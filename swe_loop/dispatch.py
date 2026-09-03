"""Dispatch a repair session for one routed work order.

Order of operations is the point: the durable row is written first (`reserve_session`), then the
org is checked for a session already carrying this work order's tag, then the API is called, then
the row is bound to Devin's id. A crash between reserve and bind leaves a reserved row that
`reconcile` adopts or orphans; it never leaves a session nobody knows about.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from swe_loop.config import TargetConfig
from swe_loop.devin import DevinClient, SessionSpec
from swe_loop.store import Store, now

ROOT = Path(__file__).resolve().parents[1]
RESULT_SCHEMA_PATH = ROOT / "schemas" / "repair_result.schema.json"


def load_result_schema() -> dict[str, Any]:
    return json.loads(RESULT_SCHEMA_PATH.read_text())


def _sites_for(files: list[str], verdict: dict[str, Any] | None) -> list[dict[str, Any]]:
    fs = set(files)
    return [s for s in (verdict or {}).get("sites", []) if s.get("file") in fs]


def _verdict(ticket: dict[str, Any]) -> dict[str, Any] | None:
    return json.loads(ticket["triage_verdict_json"]) if ticket.get("triage_verdict_json") else None


def identity_tags(cfg: TargetConfig, wo: dict[str, Any]) -> list[str]:
    """The two tags that identify a work order's session on the org: prefix plus the work order
    id, which is unique. Shard letters are not unique across tickets or targets."""
    return [cfg.session.get("tags_prefix", "swe-loop"), f"wo:{wo['id']}"]


def gate_only(cmd: str) -> bool:
    """Acceptance commands that use this repository's own tooling run on the verification
    machine, not in the session's VM."""
    return "swe_loop" in cmd


def build_repair_prompt(
    wo: dict[str, Any], ticket: dict[str, Any], cfg: TargetConfig, review: str
) -> str:
    """What / How / Result. Everything target-specific comes from the seam and the ticket."""
    sites = _sites_for(wo["files"], _verdict(ticket))
    ext = ticket.get("external_ref") or ticket["id"]
    rng = cfg.session.get("version_range", "")
    what = (
        f"In `{cfg.repo}`, starting from branch `{cfg.base_branch}`, fix every call site listed "
        f"below in {', '.join(f'`{f}`' for f in wo['files'])} so the code runs correctly on both "
        f"versions of the library named in the ticket ({cfg.name}). Ticket: {ext}, shard "
        f"{wo['shard_id']}. Open one pull request against the fork."
    )
    site_lines = []
    for s in sites:
        lines = ",".join(str(x) for x in (s.get("lines") or [s.get("line")]) if x)
        classes = ", ".join(s.get("classes") or [s.get("class", "")])
        kind = f" ({s['kind']})" if s.get("kind") else ""
        msg = (s.get("r3") or s.get("r2") or "")[:200]
        fix = (s.get("prescribed_fix") or "")[:900]
        line = f"- `{s['file']}:{lines}` [{classes}]{kind} {msg}".rstrip()
        if fix and fix[:60] != msg[:60]:
            line += f"\n  Prescribed fix from triage: {fix}"
        elif fix:
            line = f"- `{s['file']}:{lines}` [{classes}]{kind} Prescribed fix from triage: {fix}"
        site_lines.append(line)
    verdict = _verdict(ticket) or {}
    summary = (verdict.get("summary") or "")[:900]
    if not site_lines:
        site_lines = [
            f"- every site in `{f}` that the acceptance commands expose" for f in wo["files"]
        ]
    compat = (
        f"- Keep every change compatible with the version range `{rng}`; the lower bound does not move."
        if rng
        else "- Keep every change compatible with both library versions."
    )
    dos = [
        "Do:",
        "- Read the library's own message for each site before changing it; it usually names the replacement.",
        compat,
        "- Run `ruff format` and `ruff check` on the files you changed. The repository uses ruff, not black.",
        f"- Title the PR `{cfg.session.get('pr_title_prefix', 'fix')}: <summary>` and fill the pull request template.",
        "- Keep the diff to the files listed above.",
    ]
    if review == "required":
        dos.append(
            "- Review is required for this shard: the triage verdict flags a behaviour change that "
            "tests may not catch. Preserve the current behaviour as the prescribed fix describes. "
            "State in the PR what the old and new behaviour are and why the fix preserves the intent."
        )
    donts = [
        "Don't:",
        f"- Modify anything under {', '.join(cfg.forbidden_paths)}. If a test looks wrong, report it in `needs_human` and stop.",
        "- Change dependency pins or the version range.",
        "- Run the full test suite; run the acceptance commands only.",
        "- Rewrite chained-assignment or copy-on-write sites whose meaning depends on surrounding data; report them.",
    ]
    acc = [
        f"- `{k}`: `{v}`"
        + (
            " (gate only: uses tooling on the verification machine; report exit code null)"
            if gate_only(v)
            else ""
        )
        for k, v in wo["acceptance"].items()
    ]
    result = (
        "All acceptance commands exit 0 on your branch; the gate re-runs every one of them from a clean checkout:\n"
        + "\n".join(acc)
        + "\n"
        "A pull request exists against the fork with a conventional-commit title. "
        "Provide structured output matching the repair result schema and call "
        "provide_structured_output with is_final=true: shard, self_reported_done, files_changed, "
        "call_sites_fixed (file, line, change), tests_run, tests_passed, acceptance (exit codes), "
        "pr_url, branch, needs_human (site, reason)."
    )
    if summary:
        what += f"\n\nTriage verdict: {summary}"
    return (
        f"## What\n{what}\n\nSites:\n" + "\n".join(site_lines) + "\n\n"
        "## How\n" + "\n".join(dos) + "\n" + "\n".join(donts) + "\n\n"
        f"## Result\n{result}\n"
    )


def build_repair_spec(
    wo: dict[str, Any],
    ticket: dict[str, Any],
    cfg: TargetConfig,
    *,
    review: str = "normal",
    playbook_id: str | None = None,
    per_session_cap: float | None = None,
) -> SessionSpec:
    classes = sorted(
        {
            c
            for s in _sites_for(wo["files"], _verdict(ticket))
            for c in (s.get("classes") or [s.get("class", "")])
            if c
        }
    )
    tags = (
        *identity_tags(cfg, wo),
        "repair",
        f"target:{cfg.name}",
        ticket["id"],
        f"shard:{wo['shard_id']}",
        *classes[:2],
    )
    cap = cfg.max_acu_limit
    if per_session_cap:
        cap = int(min(cap, per_session_cap))
    return SessionSpec(
        prompt=build_repair_prompt(wo, ticket, cfg, review),
        tags=tags,
        repos=(cfg.repo,),
        max_acu_limit=cap,
        structured_output_schema=load_result_schema(),
        playbook_id=playbook_id,
        title=f"repair {ticket.get('external_ref') or ticket['id']} shard {wo['shard_id']}",
    )


def active_session_for(store: Store, work_order_id: str) -> dict[str, Any] | None:
    """One live, bound session per work order. Reserved-but-unbound rows are not live; they
    belong to `reconcile`."""
    for s in store.sessions_for(work_order_id):
        if (
            s["devin_session_id"]
            and s["terminal_at"] is None
            and s["status"] not in ("exit", "error", "suspended", "orphaned")
        ):
            return s
    return None


def reconcile(store: Store, client: DevinClient, sid: str, cfg: TargetConfig) -> str:
    """A reserved row with no Devin id, left by a crash between reserve and bind. Adopt the
    session on the org carrying this work order's tag, alive or finished, unless it is already
    bound to another row; otherwise mark the row orphaned. Returns the row's new status."""
    row = store.get_session(sid)
    if not row or row["devin_session_id"]:
        return row["status"] if row else "missing"
    wo = store.get_work_order(row["work_order_id"])
    found = client.find(identity_tags(cfg, wo), exclude=store.bound_devin_ids(), alive_only=False)
    if found:
        store.bind_devin_session(
            sid, devin_session_id=found.session_id, url=found.url, status=found.status
        )
        return "bound"
    store.update_session(sid, status="orphaned", terminal_at=now())
    return "orphaned"


def dispatch(
    store: Store,
    client: DevinClient,
    wo: dict[str, Any],
    cfg: TargetConfig,
    *,
    review: str = "normal",
    playbook_id: str | None = None,
    attempt: int = 1,
) -> str:
    """Reserve the row, adopt or create the session, bind the id. Returns our session id."""
    existing = active_session_for(store, wo["id"])
    if existing:
        return existing["id"]
    for stale in store.sessions_for(wo["id"]):
        if (
            stale["devin_session_id"] is None
            and stale["status"] == "reserved"
            and reconcile(store, client, stale["id"], cfg) == "bound"
        ):
            return stale["id"]
    ticket = store.get_ticket(wo["ticket_id"])
    if not ticket:
        raise KeyError(wo["ticket_id"])
    budget = store.budget_state()
    spec = build_repair_spec(
        wo,
        ticket,
        cfg,
        review=review,
        playbook_id=playbook_id,
        per_session_cap=budget.get("per_session_cap"),
    )
    sid = store.reserve_session(
        work_order_id=wo["id"], playbook_id=playbook_id, tags=list(spec.tags), attempt=attempt
    )
    store.log(
        "dispatch",
        "reserved",
        ticket_id=wo["ticket_id"],
        session_id=sid,
        detail=f"shard {wo['shard_id']} cap {spec.max_acu_limit} ACU",
    )
    live = client.find_live(identity_tags(cfg, wo), exclude=store.bound_devin_ids())
    state = live if live else client.start(spec)
    store.log(
        "dispatch",
        "adopted live session" if live else "POST /sessions",
        ticket_id=wo["ticket_id"],
        session_id=sid,
        detail=state.session_id,
    )
    store.bind_devin_session(
        sid, devin_session_id=state.session_id, url=state.url, status=state.status or "new"
    )
    store.update_session(sid, status_detail=state.status_detail)
    store.conn.execute("UPDATE work_orders SET status=? WHERE id=?", ("dispatched", wo["id"]))
    store.set_ticket_status(wo["ticket_id"], "dispatched")
    return sid
