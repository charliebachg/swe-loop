"""Intake. Any event becomes one NormalizedEvent, or nothing.

One adapter per source. The pipeline downstream never sees a raw payload. Adding a source
(Slack, a scheduler, a scan) means adding an adapter here and nothing else.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

import yaml

from swe_loop.config import TargetConfig

WORK_ORDER_BLOCK = re.compile(r"```yaml\s*\n#\s*swe-loop work order\s*\n(.*?)```", re.DOTALL)


@dataclass(frozen=True)
class NormalizedEvent:
    source: str  # github | manual
    kind: str  # pull_request | issue | check_run
    repo: str
    number: int | None
    title: str
    ref: str | None = None  # head branch for PRs and check runs
    base_ref: str | None = None
    author: str | None = None
    action: str | None = None
    labels: tuple[str, ...] = ()
    body: str = ""
    work_order: dict[str, Any] | None = None  # parsed from the issue body, when present
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def external_ref(self) -> str:
        return f"{self.repo}#{self.number}" if self.number is not None else self.repo


class Adapter(Protocol):
    kind: str

    def matches(self, payload: dict[str, Any]) -> bool: ...
    def normalize(self, payload: dict[str, Any], cfg: TargetConfig) -> NormalizedEvent | None: ...


if TYPE_CHECKING:
    from swe_loop.store import Store


def parse_work_order(body: str) -> dict[str, Any] | None:
    """The machine-readable block a ticket carries. Absent means the triage session must scope it."""
    m = WORK_ORDER_BLOCK.search(body or "")
    if not m:
        return None
    try:
        data = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None


class GitHubPullRequestAdapter:
    """A dependency bot opened or updated a PR. The v0 trigger for the running lane."""

    kind = "pull_request"

    def matches(self, payload: dict[str, Any]) -> bool:
        return "pull_request" in payload and "issue" not in payload

    def normalize(self, payload: dict[str, Any], cfg: TargetConfig) -> NormalizedEvent | None:
        pr = payload["pull_request"]
        match = cfg.trigger.get("match", {})
        actions = set(cfg.trigger.get("actions", ["opened", "synchronize", "reopened"]))
        action = payload.get("action")
        author = (pr.get("user") or {}).get("login")
        head = (pr.get("head") or {}).get("ref") or ""
        repo = (payload.get("repository") or {}).get("full_name") or cfg.repo
        if action not in actions:
            return None
        if match.get("author") and author != match["author"]:
            return None
        if match.get("head_ref_prefix") and not head.startswith(match["head_ref_prefix"]):
            return None
        if repo != cfg.repo:
            return None
        return NormalizedEvent(
            source="github",
            kind=self.kind,
            repo=repo,
            number=pr.get("number"),
            title=pr.get("title", ""),
            ref=head,
            base_ref=(pr.get("base") or {}).get("ref"),
            author=author,
            action=action,
            labels=tuple(lbl.get("name", "") for lbl in pr.get("labels", [])),
            body=pr.get("body") or "",
            extra={"draft": pr.get("draft"), "html_url": pr.get("html_url")},
        )


class GitHubIssuesAdapter:
    """A ticket on the fork carrying the swe-loop label. The v0 path into the store."""

    kind = "issue"

    def matches(self, payload: dict[str, Any]) -> bool:
        return "issue" in payload and "pull_request" not in payload.get("issue", {})

    def normalize(self, payload: dict[str, Any], cfg: TargetConfig) -> NormalizedEvent | None:
        issue = payload["issue"]
        repo = (payload.get("repository") or {}).get("full_name") or cfg.repo
        labels = tuple(lbl.get("name", "") for lbl in issue.get("labels", []))
        required = cfg.trigger.get("issue_label", "swe-loop")
        if repo != cfg.repo or required not in labels:
            return None
        if payload.get("action") not in {"opened", "labeled", "edited", "reopened"}:
            return None
        body = issue.get("body") or ""
        return NormalizedEvent(
            source="github",
            kind=self.kind,
            repo=repo,
            number=issue.get("number"),
            title=issue.get("title", ""),
            author=(issue.get("user") or {}).get("login"),
            action=payload.get("action"),
            labels=labels,
            body=body,
            work_order=parse_work_order(body),
            extra={"html_url": issue.get("html_url")},
        )


class GitHubCheckRunAdapter:
    """A check failed on a branch the seam cares about."""

    kind = "check_run"

    def matches(self, payload: dict[str, Any]) -> bool:
        return "check_run" in payload

    def normalize(self, payload: dict[str, Any], cfg: TargetConfig) -> NormalizedEvent | None:
        cr = payload["check_run"]
        repo = (payload.get("repository") or {}).get("full_name") or cfg.repo
        if repo != cfg.repo or payload.get("action") != "completed":
            return None
        if cr.get("conclusion") not in {"failure", "timed_out"}:
            return None
        head = (cr.get("check_suite") or {}).get("head_branch") or ""
        prefix = cfg.trigger.get("match", {}).get("head_ref_prefix")
        if prefix and not head.startswith(prefix):
            return None
        prs = cr.get("pull_requests") or []
        return NormalizedEvent(
            source="github",
            kind=self.kind,
            repo=repo,
            number=prs[0].get("number") if prs else None,
            title=f"{cr.get('name')} failed on {head}",
            ref=head,
            action=cr.get("conclusion"),
            extra={"check_name": cr.get("name"), "details_url": cr.get("details_url")},
        )


class ManualAdapter:
    """Replay and simulate: a NormalizedEvent posted directly, as a dict."""

    kind = "manual"

    def matches(self, payload: dict[str, Any]) -> bool:
        return "kind" in payload and "repo" in payload

    def normalize(self, payload: dict[str, Any], cfg: TargetConfig) -> NormalizedEvent | None:
        known = set(NormalizedEvent.__dataclass_fields__)
        data = {k: v for k, v in payload.items() if k in known}
        data.setdefault("source", "manual")
        if isinstance(data.get("labels"), list):
            data["labels"] = tuple(data["labels"])
        if data.get("work_order") is None and data.get("body"):
            data["work_order"] = parse_work_order(data["body"])
        return NormalizedEvent(**data)


ADAPTERS: dict[str, list[Adapter]] = {
    "github": [GitHubPullRequestAdapter(), GitHubIssuesAdapter(), GitHubCheckRunAdapter()],
    "manual": [ManualAdapter()],
}


def normalize(source: str, payload: dict[str, Any], cfg: TargetConfig) -> NormalizedEvent | None:
    for adapter in ADAPTERS.get(source, []):
        if adapter.matches(payload):
            return adapter.normalize(payload, cfg)
    return None


def verify_github_signature(secret: str, body: bytes, header: str | None) -> bool:
    """GitHub signs webhooks with HMAC-SHA256 in X-Hub-Signature-256. Fail closed when a secret is set."""
    if not secret:
        return True  # no secret configured: local, replay, tests
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header[len("sha256=") :], expected)


def ticket_id_for(ev: NormalizedEvent) -> str:
    """Stable ticket id: the shard letter from the work order when present, else the GitHub number."""
    if ev.work_order and ev.work_order.get("shard"):
        return f"tkt_{ev.work_order['shard']}"
    return f"tkt_{ev.kind[:2]}{ev.number}" if ev.number is not None else f"tkt_{ev.kind}"


def ingest(
    store: Store, ev: NormalizedEvent, *, ticket_id: str | None = None, as_new: bool = False
) -> str:
    """One normalised event becomes one ticket (created or updated). Work orders only when the
    event already carries an acceptance command; otherwise the triage session scopes it.
    as_new=True ignores any carried work order: a triage session scopes the ticket regardless.
    An existing ticket is left alone (its status is the loop's, not the event's)."""
    tid = ticket_id or ticket_id_for(ev)
    if store.get_ticket(tid):
        return tid
    wo = {} if as_new else (ev.work_order or {})
    route = wo.get("route")
    verdict = None
    if wo.get("acceptance"):
        verdict = {
            "acceptance_cmd": wo["acceptance"],
            "context_sufficient": True,
            "split": "one",
            "est_size": "XS" if len(wo.get("files", [])) <= 1 else "S",
            "needs_human": route == "human",
            "review": wo.get("review"),
        }
    store.upsert_ticket(
        id=tid,
        source=ev.source,
        title=ev.title,
        status="triaged" if verdict else "new",
        external_ref=ev.external_ref,
        cls=",".join(wo.get("classes", [])) or None,
        triage_verdict=verdict,
    )
    if verdict and route == "devin" and not store.work_orders_for(tid):
        store.insert_work_order(
            ticket_id=tid,
            shard_id=str(wo.get("shard", tid)),
            files=list(wo.get("files", [])),
            tests=list(wo.get("tests", [])),
            acceptance=dict(wo["acceptance"]),
            est_size=verdict["est_size"],
        )
    return tid
