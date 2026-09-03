"""L4: dispatch a repair session for one routed work order.

Order of operations is the point: the durable row is written first (`reserve_session`), then the
API is called, then the row is bound to Devin's id. A crash between the two leaves a reserved row
that the poller can reconcile, never a session nobody knows about.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from swe_loop.config import TargetConfig
from swe_loop.devin import DevinClient, SessionSpec
from swe_loop.store import Store

ROOT = Path(__file__).resolve().parents[1]
RESULT_SCHEMA_PATH = ROOT / "schemas" / "repair_result.schema.json"


def load_result_schema() -> dict[str, Any]:
    return json.loads(RESULT_SCHEMA_PATH.read_text())


def _sites_for(files: list[str], verdict: dict[str, Any] | None) -> list[dict[str, Any]]:
    fs = set(files)
    return [s for s in (verdict or {}).get("sites", []) if s.get("file") in fs]


def build_repair_prompt(
    wo: dict[str, Any], ticket: dict[str, Any], cfg: TargetConfig, review: str
) -> str:
    """What / How / Result. Everything target-specific comes from the seam and the ticket."""
    verdict = (
        json.loads(ticket["triage_verdict_json"]) if ticket.get("triage_verdict_json") else None
    )
    sites = _sites_for(wo["files"], verdict)
    ext = ticket.get("external_ref") or ticket["id"]
    rng = cfg.session.get("version_range", "")
    what = (
        f"In `{cfg.repo}`, starting from branch `{cfg.base_branch}`, fix every call site listed below in "
        f"{', '.join(f'`{f}`' for f in wo['files'])} so the code runs correctly on both versions of the "
        f"library named in the ticket ({cfg.name}). Ticket: {ext}, shard {wo['shard_id']}. "
        f"Open one pull request against the fork."
    )
    site_lines = []
    for s in sites:
        lines = ",".join(str(x) for x in (s.get("lines") or [s.get("line")]) if x)
        msg = s.get("r3") or s.get("r2") or s.get("prescribed_fix") or ""
        site_lines.append(
            f"- `{s['file']}:{lines}` [{', '.join(s.get('classes') or [s.get('class', '')])}] {msg[:200]}"
        )
    if not site_lines:
        site_lines = [
            f"- every site in `{f}` that the acceptance commands expose" for f in wo["files"]
        ]
    dos = [
        "Do:",
        "- Read the library's own message for each site before changing it; it usually names the replacement.",
        f"- Keep every change compatible with the version range `{rng}`; the lower bound does not move."
        if rng
        else "- Keep every change compatible with both library versions.",
        "- Run `ruff format` and `ruff check` on the files you changed. The repository uses ruff, not black.",
        f"- Title the PR `{cfg.session.get('pr_title_prefix', 'fix')}: <summary>` and fill the pull request template.",
        "- Keep the diff to the files listed above.",
    ]
    if review == "required":
        dos.append(
            "- These sites warned on the current version but did not fail on the new one: the behaviour "
            "changes silently. State in the PR what the old and new behaviour are and why the fix preserves the intent."
        )
    donts = [
        "Don't:",
        f"- Modify anything under {', '.join(cfg.forbidden_paths)}. If a test looks wrong, report it in `needs_human` and stop.",
        "- Change dependency pins or the version range.",
        "- Run the full test suite; run the acceptance commands only.",
        "- Rewrite chained-assignment or copy-on-write sites whose meaning depends on surrounding data; report them.",
    ]
    acc = [f"- `{k}`: `{v}`" for k, v in wo["acceptance"].items()]
    result = (
        "All acceptance commands exit 0 on your branch:\n" + "\n".join(acc) + "\n"
        "A pull request exists against the fork with a conventional-commit title. "
        "Provide structured output matching the repair result schema and call provide_structured_output "
        "with is_final=true: shard, self_reported_done, files_changed, call_sites_fixed (file, line, change), "
        "tests_run, tests_passed, acceptance (exit codes), pr_url, branch, needs_human (site, reason)."
    )
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
) -> SessionSpec:
    verdict = (
        json.loads(ticket["triage_verdict_json"]) if ticket.get("triage_verdict_json") else None
    )
    classes = sorted(
        {
            c
            for s in _sites_for(wo["files"], verdict)
            for c in (s.get("classes") or [s.get("class", "")])
            if c
        }
    )
    tags = (
        cfg.session.get("tags_prefix", "swe-loop"),
        "repair",
        ticket["id"],
        f"shard:{wo['shard_id']}",
        *classes[:3],
    )
    return SessionSpec(
        prompt=build_repair_prompt(wo, ticket, cfg, review),
        tags=tags,
        repos=(cfg.repo,),
        max_acu_limit=cfg.max_acu_limit,
        structured_output_schema=load_result_schema(),
        playbook_id=playbook_id,
        title=f"repair {ticket.get('external_ref') or ticket['id']} shard {wo['shard_id']}",
    )


def active_session_for(store: Store, work_order_id: str) -> dict[str, Any] | None:
    """Idempotency at the store level: one live session per work order."""
    for s in store.sessions_for(work_order_id):
        if s["terminal_at"] is None and s["status"] not in ("exit", "error", "suspended"):
            return s
    return None


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
    """Reserve the row, call the API, bind the id. Returns our session id."""
    existing = active_session_for(store, wo["id"])
    if existing:
        return existing["id"]
    ticket = store.get_ticket(wo["ticket_id"])
    if not ticket:
        raise KeyError(wo["ticket_id"])
    spec = build_repair_spec(wo, ticket, cfg, review=review, playbook_id=playbook_id)
    sid = store.reserve_session(
        work_order_id=wo["id"], playbook_id=playbook_id, tags=list(spec.tags), attempt=attempt
    )
    state = client.start(spec)
    store.bind_devin_session(
        sid, devin_session_id=state.session_id, url=state.url, status=state.status or "new"
    )
    store.update_session(sid, status_detail=state.status_detail)
    store.conn.execute("UPDATE work_orders SET status=? WHERE id=?", ("dispatched", wo["id"]))
    store.set_ticket_status(wo["ticket_id"], "dispatched")
    return sid
