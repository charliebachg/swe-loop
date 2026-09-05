"""The Devin API client (v3), behind a transport so the whole pipeline runs without a key.

Two transports:
- HttpTransport talks to https://api.devin.ai/v3/organizations/{org_id}. Bearer auth with an
  org-scoped service user key. 429, 5xx and transport errors back off with jitter. Polling
  only; there is no outbound webhook from Devin.
- FakeTransport replays fixtures from data/replay/sessions/*.json, or synthesises a plausible
  session when no fixture exists. It records every call, so tests can assert what would have
  been sent. It is the default unless mode is live AND a key is present.

Verified against the org on 2026-09-03: list endpoints paginate with `first` (default 100,
max 200) and `after` (cursor; an invalid value is a 400, an unknown parameter name is silently
ignored). `tags` and `session_ids` are array parameters. Responses carry `items`, `end_cursor`,
`has_next_page`, `total`.

Two distinct predicates on a session, because they answer different questions:
- `terminal`: the poller stops waiting. True for exit/error/suspended, and for any
  `status_detail` other than "working" (waiting_for_user and waiting_for_approval included).
- `alive`: the session still exists at Devin and can be resumed or adopted. True unless the
  status is exit/error/suspended. A session parked on a question is alive.
Success is exit + finished. A terminal session with no structured output is a failure.
"""

from __future__ import annotations

import json
import random
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx

from swe_loop.config import Settings

API_BASE = "https://api.devin.ai/v3"
TERMINAL_STATUSES = {"exit", "error", "suspended"}
ATTENTION_DETAILS = {"waiting_for_user", "waiting_for_approval"}
PAGE_SIZE = 100
MAX_PAGES = 50


class DevinError(RuntimeError):
    def __init__(self, status: int, detail: str):
        super().__init__(f"Devin API {status}: {detail}")
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class SessionSpec:
    prompt: str
    tags: tuple[str, ...]
    repos: tuple[str, ...]
    max_acu_limit: int
    structured_output_schema: dict[str, Any]
    playbook_id: str | None = None
    title: str | None = None
    structured_output_required: bool = True
    devin_mode: str = "normal"

    def to_payload(self) -> dict[str, Any]:
        p: dict[str, Any] = {
            "prompt": self.prompt,
            "tags": list(self.tags),
            "repos": list(self.repos),
            "max_acu_limit": self.max_acu_limit,
            "structured_output_schema": self.structured_output_schema,
            "structured_output_required": self.structured_output_required,
            "devin_mode": self.devin_mode,
        }
        if self.playbook_id:
            p["playbook_id"] = self.playbook_id
        if self.title:
            p["title"] = self.title
        return p


@dataclass(frozen=True)
class SessionState:
    session_id: str
    url: str
    status: str
    status_detail: str | None
    acus_consumed: float | None = None
    structured_output: dict[str, Any] | None = None
    pull_requests: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES or self.status_detail not in (None, "", "working")

    @property
    def alive(self) -> bool:
        return self.status not in TERMINAL_STATUSES

    @property
    def succeeded(self) -> bool:
        return self.status == "exit" and self.status_detail == "finished"

    @property
    def delivered(self) -> bool:
        """The contract is met: structured output is present and the session's turn is over.
        Verified live 2026-09-03: a session that calls provide_structured_output(is_final=true) and
        has nothing more to do ends its turn as running/waiting_for_user, not exit/finished."""
        return bool(self.structured_output) and (
            self.succeeded or self.status_detail == "waiting_for_user"
        )

    @property
    def needs_attention(self) -> bool:
        return self.status_detail in ATTENTION_DETAILS

    @property
    def too_large(self) -> bool:
        return self.status_detail == "usage_limit_exceeded"

    @classmethod
    def from_raw(cls, d: dict[str, Any]) -> SessionState:
        prs = d.get("pull_requests") or []
        return cls(
            session_id=d.get("session_id") or d.get("id") or "",
            url=d.get("url") or d.get("session_url") or "",
            status=d.get("status") or "",
            status_detail=d.get("status_detail"),
            acus_consumed=d.get("acus_consumed"),
            structured_output=d.get("structured_output"),
            pull_requests=tuple(p.get("url") if isinstance(p, dict) else str(p) for p in prs),
            tags=tuple(d.get("tags") or ()),
            raw=d,
        )


class Transport(Protocol):
    def create_session(self, payload: dict[str, Any]) -> dict[str, Any]: ...
    def get_session(self, session_id: str) -> dict[str, Any]: ...
    def list_sessions(self, tags: list[str] | None = None) -> list[dict[str, Any]]: ...
    def send_message(self, session_id: str, text: str) -> dict[str, Any]: ...
    def terminate(self, session_id: str, archive: bool = True) -> dict[str, Any]: ...
    def list_insights(self, session_ids: list[str] | None = None) -> list[dict[str, Any]]: ...
    def list_automations(self) -> list[dict[str, Any]]: ...
    def list_playbooks(self) -> list[dict[str, Any]]: ...
    def list_knowledge_notes(self) -> list[dict[str, Any]]: ...
    def list_secrets(self) -> list[dict[str, Any]]: ...
    def create_pr_review(self, pr_url: str) -> dict[str, Any]: ...
    def get_pr_review(self, pr_url: str) -> dict[str, Any]: ...
    def create_playbook(self, payload: dict[str, Any]) -> dict[str, Any]: ...
    def get_playbook(self, playbook_id: str) -> dict[str, Any]: ...
    def update_playbook(self, playbook_id: str, payload: dict[str, Any]) -> dict[str, Any]: ...
    def create_knowledge_note(self, payload: dict[str, Any]) -> dict[str, Any]: ...
    def start_code_scan(self, payload: dict[str, Any]) -> dict[str, Any]: ...
    def list_code_scans(self) -> list[dict[str, Any]]: ...
    def list_code_scan_findings(
        self, scan_id: str | None = None, repo_name: str | None = None
    ) -> list[dict[str, Any]]: ...
    def list_code_scan_profiles(self) -> list[dict[str, Any]]: ...
    def remediate_finding(self, scan_id: str, finding_id: str) -> dict[str, Any]: ...
    def create_auto_scan(self, scan_id: str, payload: dict[str, Any]) -> dict[str, Any]: ...
    def delete_auto_scan(self, scan_id: str) -> dict[str, Any]: ...
    def update_automation(self, automation_id: str, patch: dict[str, Any]) -> dict[str, Any]: ...


# ---------------------------------------------------------------------------- HTTP
class HttpTransport:
    def __init__(
        self,
        api_key: str,
        org_id: str,
        *,
        client: httpx.Client | None = None,
        max_retries: int = 5,
        sleep=time.sleep,
    ):
        if not api_key or not org_id:
            raise ValueError("HttpTransport needs an api key and an org id")
        self.base = f"{API_BASE}/organizations/{org_id}"
        self.client = client or httpx.Client(timeout=30)
        self.headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        self.max_retries = max_retries
        self.sleep = sleep

    def _req(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        url = f"{self.base}{path}"
        delay = 1.0
        last = "no attempt made"
        for attempt in range(self.max_retries + 1):
            try:
                r = self.client.request(method, url, headers=self.headers, **kw)
            except httpx.HTTPError as ex:  # timeouts and connection errors: retry, then DevinError
                last = f"{type(ex).__name__}: {ex}"
                if attempt == self.max_retries:
                    raise DevinError(0, last) from ex
                self.sleep(min(delay + random.uniform(0, delay), 60))
                delay = min(delay * 2, 30)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                last = f"{r.status_code}: {r.text[:200]}"
                if attempt == self.max_retries:
                    raise DevinError(r.status_code, r.text[:200])
                retry_after = r.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else delay + random.uniform(0, delay)
                self.sleep(min(wait, 60))
                delay = min(delay * 2, 30)
                continue
            if r.status_code >= 400:
                raise DevinError(r.status_code, r.text[:300])
            if not r.content:
                return {}
            return r.json()
        raise DevinError(0, last)

    def _paged(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Cursor pagination with `first`/`after`. A cursor that stops advancing ends the loop."""
        items: list[dict[str, Any]] = []
        after: str | None = None
        seen: set[str] = set()
        for _ in range(MAX_PAGES):
            q: dict[str, Any] = {"first": PAGE_SIZE, **params}
            if after:
                q["after"] = after
            page = self._req("GET", path, params=q)
            items += page.get("items", [])
            nxt = page.get("end_cursor")
            if not page.get("has_next_page") or not nxt or nxt in seen:
                break
            seen.add(nxt)
            after = nxt
        return items

    def create_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._req("POST", "/sessions", json=payload)

    def get_session(self, session_id: str) -> dict[str, Any]:
        return self._req("GET", f"/sessions/{session_id}")

    def list_sessions(self, tags: list[str] | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"tags": list(tags)} if tags else {}
        items = self._paged("/sessions", params)
        if tags:  # confirm client-side regardless of the server's any/all semantics
            want = set(tags)
            items = [i for i in items if want <= set(i.get("tags") or [])]
        return items

    def send_message(self, session_id: str, text: str) -> dict[str, Any]:
        return self._req("POST", f"/sessions/{session_id}/messages", json={"message": text})

    def terminate(self, session_id: str, archive: bool = True) -> dict[str, Any]:
        return self._req(
            "DELETE", f"/sessions/{session_id}", params={"archive": str(archive).lower()}
        )

    def list_insights(self, session_ids: list[str] | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"session_ids": list(session_ids)} if session_ids else {}
        return self._paged("/sessions/insights", params)

    def generate_insights(self, session_id: str) -> dict[str, Any]:
        """Ask Devin to write its analysis of one finished session. Free; about a minute; the
        answer says `already_exists` when it was written before."""
        return self._req("POST", f"/sessions/{session_id}/insights/generate")

    def list_automations(self) -> list[dict[str, Any]]:
        return self._paged("/automations", {})

    def list_playbooks(self) -> list[dict[str, Any]]:
        return self._paged("/playbooks", {})

    def list_knowledge_notes(self) -> list[dict[str, Any]]:
        return self._paged("/knowledge/notes", {})

    def list_secrets(self) -> list[dict[str, Any]]:
        return self._paged("/secrets", {})

    def create_pr_review(self, pr_url: str) -> dict[str, Any]:
        return self._req("POST", "/pr-reviews", json={"pr_url": pr_url})

    # ---- Devin's own code scans. A scan is not a session: it is started, it runs its own
    # orchestrator session, and its findings are read back from the organisation.
    def start_code_scan(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._req("POST", "/code-scans", json=payload)

    def list_code_scans(self) -> list[dict[str, Any]]:
        return self._paged("/code-scans/scans", {})

    def list_code_scan_findings(
        self, scan_id: str | None = None, repo_name: str | None = None
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if scan_id:
            params["scan_id"] = scan_id
        if repo_name:
            params["repo_name"] = repo_name
        return self._paged("/code-scans/findings", params)

    def list_code_scan_profiles(self) -> list[dict[str, Any]]:
        return self._paged("/code-scans/profiles", {})

    def remediate_finding(self, scan_id: str, finding_id: str) -> dict[str, Any]:
        return self._req("POST", f"/code-scans/{scan_id}/findings/{finding_id}/remediate")

    def create_auto_scan(self, scan_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Ask Devin to keep scanning this repository on a recurrence of its own. Devin backs the
        schedule with an Automation, so the answer carries the id of the one it made."""
        return self._req("POST", f"/code-scans/{scan_id}/auto-scan", json=payload)

    def delete_auto_scan(self, scan_id: str) -> dict[str, Any]:
        return self._req("DELETE", f"/code-scans/{scan_id}/auto-scan")

    def update_automation(self, automation_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        """Change one automation on the organisation. The spec calls this a merge patch: what is
        not sent is kept, so sending {"enabled": ...} touches nothing else."""
        return self._req("PATCH", f"/automations/{automation_id}", json=patch)

    def get_pr_review(self, pr_url: str) -> dict[str, Any]:
        """Verified live 2026-09-03: status (running | completed), repo_path, pr_number,
        commit_sha, created_at. The findings themselves land on the pull request."""
        return self._req("GET", "/pr-reviews", params={"pr_url": pr_url})

    def create_playbook(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._req("POST", "/playbooks", json=payload)

    def get_playbook(self, playbook_id: str) -> dict[str, Any]:
        return self._req("GET", f"/playbooks/{playbook_id}")

    def update_playbook(self, playbook_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """The same body as create; the organisation's copy takes the file's text."""
        return self._req("PUT", f"/playbooks/{playbook_id}", json=payload)

    def create_knowledge_note(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._req("POST", "/knowledge/notes", json=payload)


# ---------------------------------------------------------------------------- Fake
class FakeTransport:
    """Replays fixtures; synthesises when none exist. Records every call in `calls`."""

    def __init__(self, replay_dir: Path | str | None = None, *, synthesize: bool = True):
        self._playbooks: dict[str, dict[str, Any]] = {}
        self.replay_dir = Path(replay_dir) if replay_dir else None
        self.synthesize = synthesize
        self.calls: list[tuple[str, Any]] = []
        self._fixtures: list[dict[str, Any]] = []
        self._sessions: dict[str, dict[str, Any]] = {}
        self._counter = 0
        self._prefix = f"fake-{uuid.uuid4().hex[:4]}"  # distinct per instance: ids never collide across seeds and runs
        self.fail_terminate: set[str] = set()  # tests: session ids whose terminate raises
        if self.replay_dir and (self.replay_dir / "sessions").is_dir():
            for f in sorted((self.replay_dir / "sessions").glob("*.json")):
                self._fixtures.append(json.loads(f.read_text()))

    def _pick_fixture(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        tags = set(payload.get("tags", []))
        for fx in self._fixtures:
            if fx.get("used"):
                continue
            if not fx.get("match_tags") or set(fx["match_tags"]) <= tags:
                fx["used"] = True
                return fx
        return None

    def _synth(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._counter += 1
        sid = f"{self._prefix}-{self._counter:03d}"
        repo = (payload.get("repos") or ["owner/repo"])[0]
        tags = payload.get("tags", [])
        shard = next((t.split(":", 1)[1] for t in tags if t.startswith("shard:")), "X")
        if "scan" in tags:
            out = {
                "searched": "synthesised by FakeTransport; no repository was read",
                "findings": [
                    {
                        "title": "pandas 3: synthesised finding in superset/synthesised.py",
                        "file": "superset/synthesised.py",
                        "line": 1,
                        "class": "synthesised",
                        "why": "synthesised by FakeTransport; replace with a recorded fixture",
                        "tests": ["tests/unit_tests/synthesised_test.py"],
                        "confidence": "unsure",
                    }
                ],
            }
            return self._timeline(sid, out, pr_url=None)
        if "triage" in tags:
            ticket_id = next((t for t in tags if t.startswith("tkt_")), "tkt_X")
            out: dict[str, Any] = {
                "ticket_id": ticket_id,
                "summary": "synthesised by FakeTransport; replace with a recorded fixture",
                "classes": ["synthesised"],
                "sites": [
                    {
                        "file": "superset/synthesised.py",
                        "line": 1,
                        "class": "synthesised",
                        "kind": "mechanical",
                        "tests": ["tests/unit_tests/synthesised_test.py"],
                    }
                ],
                "acceptance_cmd": {"p2": "true", "p3": "true"},
                "context_sufficient": True,
                "missing": [],
                "split": "one",
                "shards": [],
                "est_size": "S",
                "needs_human": [],
            }
            return self._timeline(sid, out, pr_url=None)
        out = {
            "shard": shard,
            "self_reported_done": True,
            "files_changed": [],
            "call_sites_fixed": [],
            "tests_run": 0,
            "tests_passed": 0,
            "pr_url": f"https://github.com/{repo}/pull/{900 + self._counter}",
            "needs_human": [],
            "notes": "synthesised by FakeTransport; replace with a recorded fixture",
        }
        return self._timeline(sid, out, pr_url=out["pr_url"])

    @staticmethod
    def _timeline(sid: str, out: dict[str, Any], *, pr_url: str | None) -> dict[str, Any]:
        last: dict[str, Any] = {
            "status": "exit",
            "status_detail": "finished",
            "acus_consumed": 2.1,
            "structured_output": out,
        }
        if pr_url:
            last["pull_requests"] = [{"url": pr_url}]
        return {
            "session_id": sid,
            "url": f"https://app.devin.ai/sessions/{sid}",
            "timeline": [
                {"status": "new", "status_detail": None, "acus_consumed": 0.0},
                {"status": "running", "status_detail": "working", "acus_consumed": 0.6},
                {"status": "running", "status_detail": "working", "acus_consumed": 1.4},
                last,
            ],
            "insights": {"session_size": "S", "num_user_messages": 1, "num_devin_messages": 6},
        }

    def _state(self, sid: str, advance: bool) -> dict[str, Any]:
        s = self._sessions[sid]
        tl = s["fixture"]["timeline"]
        idx = min(s["i"], len(tl) - 1) if advance else min(max(s["i"] - 1, 0), len(tl) - 1)
        if advance:
            s["i"] += 1
        state = dict(tl[idx])
        if s["terminated"]:
            state.update(status="exit", status_detail="terminated")
        state.update(
            session_id=sid, url=s["fixture"]["url"], tags=sorted(s["created"].get("tags", []))
        )
        return state

    # transport API
    def create_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("create_session", payload))
        fx = self._pick_fixture(payload)
        if fx is None:
            if not self.synthesize:
                raise DevinError(0, "no fixture for this session and synthesis is off")
            fx = self._synth(payload)
        sid = fx["session_id"]
        self._sessions[sid] = {
            "fixture": fx,
            "i": 1,
            "created": payload,
            "messages": [],
            "terminated": False,
        }
        first = fx["timeline"][0]
        return {
            "session_id": sid,
            "url": fx["url"],
            "tags": sorted(payload.get("tags", [])),
            **first,
        }

    def get_session(self, session_id: str) -> dict[str, Any]:
        self.calls.append(("get_session", session_id))
        return self._state(session_id, advance=True)

    def list_sessions(self, tags: list[str] | None = None) -> list[dict[str, Any]]:
        self.calls.append(("list_sessions", tags))
        want = set(tags or [])
        ours = [
            self._state(sid, advance=False)
            for sid, s in self._sessions.items()
            if want <= set(s["created"].get("tags", []))
        ]
        # sessions Devin made on its own, the way a schedule does; a test sets these directly
        theirs = [] if tags else list(getattr(self, "foreign_sessions", []))
        return ours + theirs

    def send_message(self, session_id: str, text: str) -> dict[str, Any]:
        self.calls.append(("send_message", (session_id, text)))
        if session_id not in self._sessions:
            raise DevinError(404, f"session {session_id} not found")
        self._sessions[session_id]["messages"].append(text)
        return {"ok": True}

    def terminate(self, session_id: str, archive: bool = True) -> dict[str, Any]:
        self.calls.append(("terminate", (session_id, archive)))
        if session_id in self.fail_terminate:
            raise DevinError(404, "session not found")
        self._sessions[session_id]["terminated"] = True
        return {"ok": True, "archived": archive}

    def generate_insights(self, session_id: str) -> dict[str, Any]:
        self.calls.append(("generate_insights", session_id))
        s = self._sessions.get(session_id)
        if s is None:
            raise DevinError(404, "no such session")
        ins = s["fixture"].setdefault("insights", {})
        if (ins.get("analysis") or {}).get("issues"):
            return {"session_id": session_id, "status": "already_exists"}
        # the fixture may carry the analysis Devin would write; else a small one stands in
        ins["analysis"] = s["fixture"].get("insights_analysis") or {
            **(ins.get("analysis") or {}),
            "issues": [
                {
                    "id": "iss-1",
                    "label": "environment",
                    "impact": "low",
                    "title": "Acceptance commands reference environments the clone lacks",
                    "issue": "The commands name .venv-p2 and .venv-p3, which the session could not run.",
                }
            ],
            "action_items": [
                {
                    "issue_id": "iss-1",
                    "type": "knowledge",
                    "action_item": "Say in the knowledge note that the acceptance commands are run by the loop, not by the session.",
                }
            ],
            "timeline": [
                {"title": "Knowledge notes fetched", "description": "Three notes.", "color": "blue"}
            ],
            "suggested_prompt": None,
            "note_usage": None,
        }
        return {"session_id": session_id, "status": "started"}

    def list_insights(self, session_ids: list[str] | None = None) -> list[dict[str, Any]]:
        self.calls.append(("list_insights", session_ids))
        out = []
        for sid, s in self._sessions.items():
            if session_ids and sid not in session_ids:
                continue
            last = s["fixture"]["timeline"][-1]
            out.append(
                {
                    "session_id": sid,
                    "acus_consumed": last.get("acus_consumed"),
                    **s["fixture"].get("insights", {}),
                }
            )
        return out

    def list_automations(self) -> list[dict[str, Any]]:
        self.calls.append(("list_automations", None))
        return list(getattr(self, "automations", []))

    def list_playbooks(self) -> list[dict[str, Any]]:
        self.calls.append(("list_playbooks", None))
        return list(getattr(self, "_playbooks", {}).values())

    def list_knowledge_notes(self) -> list[dict[str, Any]]:
        self.calls.append(("list_knowledge_notes", None))
        return []

    def list_secrets(self) -> list[dict[str, Any]]:
        self.calls.append(("list_secrets", None))
        return []

    def create_pr_review(self, pr_url: str) -> dict[str, Any]:
        self.calls.append(("create_pr_review", pr_url))
        return {"review_id": f"rev-{len(self.calls)}", "pr_url": pr_url, "status": "queued"}

    def get_pr_review(self, pr_url: str) -> dict[str, Any]:
        self.calls.append(("get_pr_review", pr_url))
        return {
            "status": "completed",
            "pr_number": int(pr_url.rsplit("/", 1)[-1]),
            "commit_sha": "0" * 40,
        }

    def create_playbook(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("create_playbook", payload))
        pid = f"pb-{len(self.calls)}"
        self._playbooks[pid] = {"playbook_id": pid, **payload}
        return self._playbooks[pid]

    def get_playbook(self, playbook_id: str) -> dict[str, Any]:
        self.calls.append(("get_playbook", playbook_id))
        return dict(self._playbooks.get(playbook_id) or {})

    def update_playbook(self, playbook_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("update_playbook", playbook_id))
        self._playbooks[playbook_id] = {"playbook_id": playbook_id, **payload}
        return self._playbooks[playbook_id]

    def create_knowledge_note(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("create_knowledge_note", payload))
        return {"note_id": f"kn-{len(self.calls)}", **payload}

    # ---- code scans. The fake answers one scan that completes with the findings in the
    # fixture, so the whole chain can be exercised with no key and no money.
    def start_code_scan(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("start_code_scan", payload))
        self._scan = {
            "scan_id": "fake-scan-001",
            "org_id": "fake-org",
            "repo_name": payload.get("repo_name", ""),
            "host": "github.com",
            "status": "running",
            "profile": None,
            "scan_type": payload.get("scan_type") or "security",
            "created_at": 0,
        }
        self._scan_polls = 0
        return dict(self._scan)

    def list_code_scans(self) -> list[dict[str, Any]]:
        self.calls.append(("list_code_scans", None))
        sc = getattr(self, "_scan", None)
        if not sc:
            return []
        self._scan_polls = getattr(self, "_scan_polls", 0) + 1
        if self._scan_polls >= 2:
            sc["status"] = "completed"
        return [dict(sc)]

    def list_code_scan_findings(
        self, scan_id: str | None = None, repo_name: str | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append(("list_code_scan_findings", scan_id))
        sc = getattr(self, "_scan", None)
        if not sc or sc["status"] != "completed":
            return []
        # a test can say what a later look at the same scan turns up, the way new commits do
        if getattr(self, "_findings", None) is not None:
            return [dict(f, scan_id=sc["scan_id"]) for f in self._findings]
        return [
            {
                "finding_id": "fake-finding-001",
                "scan_id": sc["scan_id"],
                "repo_name": sc["repo_name"],
                "title": "Unvalidated redirect in the login flow",
                "description": "The next parameter is used without checking the host.",
                "recommendation": "Reject an absolute URL whose host is not the application's.",
                "note": None,
                "code_owners": [],
                "reference_snippets": [
                    {"file_path": "superset/views/base.py", "start_line": 120, "end_line": 124}
                ],
                "severity": "high",
                "category": "open-redirect",
                "pr_url": getattr(self, "_fix_pr", None),
                "session_id": (
                    f"fake-remediation-{getattr(self, '_remediated', 0):03d}"
                    if getattr(self, "_remediated", 0)
                    else None
                ),
                "orchestrator_session_id": "fake-orchestrator-001",
                "status": "open",
                "created_at": 0,
            }
        ]

    def list_code_scan_profiles(self) -> list[dict[str, Any]]:
        self.calls.append(("list_code_scan_profiles", None))
        return []

    def create_auto_scan(self, scan_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("create_auto_scan", (scan_id, payload)))
        made = {
            "scan_id": scan_id,
            "automation_id": f"auto-fake-{scan_id[-6:]}",
            "rrule": payload.get("rrule", ""),
            "enabled": bool(payload.get("enabled", True)),
        }
        self._auto_scans = getattr(self, "_auto_scans", {})
        self._auto_scans[scan_id] = made
        return made

    def delete_auto_scan(self, scan_id: str) -> dict[str, Any]:
        self.calls.append(("delete_auto_scan", scan_id))
        getattr(self, "_auto_scans", {}).pop(scan_id, None)
        return {}

    def update_automation(self, automation_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        """Set `lag_automation_reads` to model the real thing: the answer to a change carries the
        new state while a list read straight afterwards can still carry the old one."""
        self.calls.append(("update_automation", (automation_id, patch)))
        for a in getattr(self, "automations", []):
            if a.get("automation_id") == automation_id:
                if getattr(self, "lag_automation_reads", False):
                    return {**a, **patch}
                a.update(patch)
                return dict(a)
        return {"automation_id": automation_id, **patch}

    def remediate_finding(self, scan_id: str, finding_id: str) -> dict[str, Any]:
        self.calls.append(("remediate_finding", (scan_id, finding_id)))
        self._remediated = getattr(self, "_remediated", 0) + 1
        sid = f"fake-remediation-{self._remediated:03d}"
        self._fix_pr = f"https://github.com/o/r/pull/{90 + self._remediated}"
        return {"finding_id": finding_id, "session_id": sid}


# ---------------------------------------------------------------------------- Client
class DevinClient:
    def __init__(self, transport: Transport):
        self.t = transport

    @classmethod
    def from_settings(cls, settings: Settings) -> DevinClient:
        if settings.live and settings.devin_api_key and settings.devin_org_id:
            return cls(HttpTransport(settings.devin_api_key, settings.devin_org_id))
        return cls(FakeTransport(settings.replay_dir))

    @property
    def is_fake(self) -> bool:
        return isinstance(self.t, FakeTransport)

    def start(self, spec: SessionSpec) -> SessionState:
        return SessionState.from_raw(self.t.create_session(spec.to_payload()))

    def status(self, session_id: str) -> SessionState:
        return SessionState.from_raw(self.t.get_session(session_id))

    def find(
        self, tags: list[str], *, exclude: set[str] | None = None, alive_only: bool = True
    ) -> SessionState | None:
        """Sessions on the org carrying every tag, minus `exclude`. An alive one wins; with
        alive_only=False the last-listed terminal one is returned when no live one exists."""
        exclude = exclude or set()
        cands = [SessionState.from_raw(r) for r in self.t.list_sessions(tags)]
        cands = [c for c in cands if c.session_id and c.session_id not in exclude]
        for c in cands:
            if c.alive:
                return c
        if alive_only or not cands:
            return None
        return cands[-1]

    def find_live(self, tags: list[str], *, exclude: set[str] | None = None) -> SessionState | None:
        return self.find(tags, exclude=exclude, alive_only=True)

    def message(self, session_id: str, text: str) -> None:
        self.t.send_message(session_id, text)

    def terminate(self, session_id: str) -> None:
        self.t.terminate(session_id, archive=True)

    def insights(self, session_ids: list[str]) -> dict[str, dict[str, Any]]:
        return {i["session_id"]: i for i in self.t.list_insights(session_ids)}

    def generate_insights(self, session_id: str) -> dict[str, Any]:
        return self.t.generate_insights(session_id)

    def review_pr(self, pr_url: str) -> dict[str, Any]:
        return self.t.create_pr_review(pr_url)

    def start_code_scan(self, **payload: Any) -> dict[str, Any]:
        return self.t.start_code_scan({k: v for k, v in payload.items() if v is not None})

    def code_scan(self, scan_id: str) -> dict[str, Any] | None:
        return next((s for s in self.t.list_code_scans() if s["scan_id"] == scan_id), None)

    def code_scan_findings(self, scan_id: str) -> list[dict[str, Any]]:
        return self.t.list_code_scan_findings(scan_id=scan_id)

    def remediate(self, scan_id: str, finding_id: str) -> dict[str, Any]:
        return self.t.remediate_finding(scan_id, finding_id)

    def auto_scan(self, scan_id: str, rrule: str, *, enabled: bool = False) -> dict[str, Any]:
        """Hand the recurrence to Devin instead of running one ourselves. Devin backs it with an
        Automation of its own; created switched off, because a schedule that fires the morning of
        a walk-through is not something to turn on by surprise."""
        return self.t.create_auto_scan(scan_id, {"rrule": rrule, "enabled": enabled})

    def stop_auto_scan(self, scan_id: str) -> dict[str, Any]:
        return self.t.delete_auto_scan(scan_id)

    def set_automation_enabled(self, automation_id: str, enabled: bool) -> dict[str, Any]:
        """Switch a Devin-held schedule on or off, so the button in this app moves the real one."""
        return self.t.update_automation(automation_id, {"enabled": bool(enabled)})

    def automation(self, automation_id: str) -> dict[str, Any] | None:
        """One automation as Devin holds it, so a page can show the real state and not ours."""
        return next(
            (a for a in self.t.list_automations() if a.get("automation_id") == automation_id), None
        )
