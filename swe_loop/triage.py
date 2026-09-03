"""L1: the triage session. Builds the session spec from a ticket and validates the verdict.

The session reads and reasons; it does not patch. Its output is a verdict that satisfies
schemas/triage_verdict.schema.json, which encodes the three questions Devin's own guidance says
to answer before assigning a task: can success be described, is there enough context, would
breaking it down help.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft7Validator

from swe_loop.config import TargetConfig
from swe_loop.devin import SessionSpec
from swe_loop.store import Store, now

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "schemas" / "triage_verdict.schema.json"
PLAYBOOK_PATH = ROOT / "playbooks" / "triage-pandas3.md"
TRIAGE_ACU_CAP = 3


def load_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text())


def validate_verdict(verdict: Any) -> list[str]:
    """Return a list of problems. Empty means the verdict is acceptable."""
    v = Draft7Validator(load_schema())
    errors = sorted(v.iter_errors(verdict), key=lambda e: list(e.path))
    out = [f"{'/'.join(str(p) for p in e.path) or '$'}: {e.message}" for e in errors]
    if (
        isinstance(verdict, dict)
        and verdict.get("split") == "parallel"
        and not verdict.get("shards")
    ):
        out.append("shards: split is parallel but no shards were given")
    return out


def build_prompt(ticket: dict[str, Any], cfg: TargetConfig, inventory_path: str | None) -> str:
    """What / How / Result, in the shape Devin's prompting guide uses."""
    ext = ticket.get("external_ref") or ticket["id"]
    what = (
        f"In `{cfg.repo}` on branch `{cfg.base_branch}`, read ticket {ext}: {ticket['title']}.\n"
        f"Decide how the {cfg.name} migration work in this ticket should be carried out. "
        f"Do not change any code."
    )
    inv = (
        f"The inventory is at `{inventory_path}`."
        if inventory_path
        else "There is no inventory file; derive sites from the ticket."
    )
    how = "\n".join(
        [
            "Do:",
            f"- {inv}",
            "- Group call sites by module and by class of change. One file belongs to one shard.",
            "- For each group decide whether one session can finish it in under three hours; otherwise split.",
            (
                "- Name an acceptance command per group: pytest over the impacted tests with warnings as errors, "
                "and the same tests on the new library version."
            ),
            (
                "- Flag as needs_human: semantics the upstream notes say need manual review, anything under "
                f"{', '.join(cfg.forbidden_paths)}, and anything under {', '.join(cfg.router.get('coverage_100_paths', []))} without a covering test."
            ),
            "Don't:",
            "- Modify any file. Open or comment on any PR or issue. Install anything.",
            "- Run the full suite; run only the tests named for the sites you are scoping.",
        ]
    )
    result = (
        "Provide structured output matching the verdict schema and call provide_structured_output with "
        "is_final=true. Fields: ticket_id, summary, sites (file, line, class, kind, prescribed_fix, tests), "
        "acceptance_cmd (named commands), context_sufficient and missing, split (one|parallel) with shards, "
        "est_size, needs_human (site, reason). The session is done when that call has been made."
    )
    return f"## What\n{what}\n\n## How\n{how}\n\n## Result\n{result}\n"


def build_triage_spec(
    ticket: dict[str, Any],
    cfg: TargetConfig,
    *,
    inventory_path: str | None,
    playbook_id: str | None,
) -> SessionSpec:
    return SessionSpec(
        prompt=build_prompt(ticket, cfg, inventory_path),
        tags=(cfg.session.get("tags_prefix", "swe-loop"), "triage", ticket["id"]),
        repos=(cfg.repo,),
        max_acu_limit=min(TRIAGE_ACU_CAP, cfg.max_acu_limit),
        structured_output_schema=load_schema(),
        playbook_id=playbook_id,
        title=f"triage {ticket.get('external_ref') or ticket['id']}",
    )


def apply_verdict(store: Store, ticket_id: str, verdict: dict[str, Any]) -> list[str]:
    """Record the verdict and create work orders. Returns the work order ids.

    Routing is the router's job (L2); this only turns shards into rows. If the verdict says the
    whole ticket needs a person, no work order is created and the router will see needs_human.
    """
    problems = validate_verdict(verdict)
    if problems:
        raise ValueError("verdict rejected: " + "; ".join(problems))
    t = store.get_ticket(ticket_id)
    if not t:
        raise KeyError(ticket_id)
    store.upsert_ticket(
        id=ticket_id,
        source=t["source"],
        title=t["title"],
        status="triaged",
        cls=",".join(verdict.get("classes", [])) or t.get("class"),
        triage_verdict=verdict,
    )
    ids: list[str] = []
    if verdict["split"] == "parallel":
        for sh in verdict["shards"]:
            ids.append(
                store.insert_work_order(
                    ticket_id=ticket_id,
                    shard_id=sh["id"],
                    files=sh["files"],
                    tests=sh["tests"],
                    acceptance=sh.get("acceptance_cmd") or verdict["acceptance_cmd"],
                    est_size=sh["est_size"],
                )
            )
    else:
        files = sorted({s["file"] for s in verdict["sites"]})
        tests = sorted({t_ for s in verdict["sites"] for t_ in s.get("tests", [])})
        human_only = {h["site"] for h in verdict["needs_human"]}
        if files and not all(f"{s['file']}:{s['line']}" in human_only for s in verdict["sites"]):
            ids.append(
                store.insert_work_order(
                    ticket_id=ticket_id,
                    shard_id=ticket_id.removeprefix("tkt_"),
                    files=files,
                    tests=tests,
                    acceptance=verdict["acceptance_cmd"],
                    est_size=verdict["est_size"],
                )
            )
    return ids


# ---------------------------------------------------------------------------- the runner
def run_triage(
    store: Store,
    client: Any,
    ticket_id: str,
    cfg: TargetConfig,
    *,
    inventory_path: str | None = None,
    playbook_id: str | None = None,
    sleep: Any = None,
    wall_clock: float = 3600.0,
    min_wait: float = 5.0,
    max_wait: float = 30.0,
    answer: str | None = None,
) -> dict[str, Any]:
    """One triage session for one ticket, start to verdict. The session proposes; this code
    validates the verdict against the schema and only then writes work orders. A terminal session
    without structured output is a failure and is escalated, never treated as a pass."""
    import time as _time

    sleep = sleep or (
        (lambda s_: _time.sleep(min(s_, 0.05)))
        if getattr(client, "is_fake", False)
        else _time.sleep
    )
    t = store.get_ticket(ticket_id)
    if not t:
        raise KeyError(ticket_id)
    if t["status"] not in ("new", "escalated"):
        return {
            "ticket_id": ticket_id,
            "kind": "skipped",
            "detail": f"status is {t['status']}, not new",
        }
    spec = build_triage_spec(t, cfg, inventory_path=inventory_path, playbook_id=playbook_id)
    live = client.find_live(list(spec.tags))
    state = live if live else client.start(spec)
    prior = store.triage_session_by_devin_id(state.session_id)
    if prior:
        tid = prior["id"]
        store.update_triage_session(tid, terminal_at=None, outcome=None)
    else:
        tid = store.insert_triage_session(
            ticket_id=ticket_id,
            devin_session_id=state.session_id,
            url=state.url,
            status=state.status or "new",
            status_detail=state.status_detail,
            playbook_id=playbook_id,
            tags=list(spec.tags),
        )
    if answer:
        client.message(state.session_id, answer)
        store.log("L1 triage", "answered by a person", ticket_id=ticket_id, detail=answer[:200])
    store.log(
        "L1 triage",
        "adopted live session" if live else "POST /sessions",
        ticket_id=ticket_id,
        detail=f"{state.session_id} cap {spec.max_acu_limit} ACU",
    )
    start = _time.monotonic()
    delay = min_wait
    while True:
        state = client.status(state.session_id)
        store.update_triage_session(
            tid,
            status=state.status,
            status_detail=state.status_detail,
            acus_consumed=state.acus_consumed,
        )
        store.log(
            "L1 triage",
            f"{state.status}/{state.status_detail or '-'}",
            ticket_id=ticket_id,
            detail=f"acus={state.acus_consumed}",
        )
        if state.delivered or state.terminal:
            break
        if _time.monotonic() - start > wall_clock:
            try:
                client.terminate(state.session_id)
            except Exception as ex:  # noqa: BLE001 - best effort; the row is closed regardless
                store.log(
                    "L1 triage", "terminate call failed", ticket_id=ticket_id, detail=str(ex)[:120]
                )
            store.update_triage_session(
                tid, terminal_at=now(), status="exit", status_detail="terminated", outcome="timeout"
            )
            store.insert_escalation(
                ticket_id,
                None,
                "review_blocked",
                f"triage session exceeded {wall_clock:.0f}s; terminated",
            )
            store.set_ticket_status(ticket_id, "escalated")
            return {"ticket_id": ticket_id, "kind": "timeout", "session": tid}
        sleep(delay)
        delay = min(delay * 2, max_wait)

    if state.too_large:
        store.update_triage_session(tid, terminal_at=now(), outcome="too_large")
        store.insert_escalation(
            ticket_id,
            None,
            "usage_limit",
            f"triage hit its {spec.max_acu_limit} ACU cap: the ticket is too large to scope in one session",
        )
        store.set_ticket_status(ticket_id, "escalated")
        return {"ticket_id": ticket_id, "kind": "too_large", "session": tid}
    out = state.structured_output
    if state.status_detail == "waiting_for_user" and not out:
        store.update_triage_session(tid, outcome="waiting")
        store.insert_escalation(
            ticket_id,
            None,
            "waiting_for_user",
            "the triage session asked a question; answer it with triage --answer, or terminate it",
        )
        store.set_ticket_status(ticket_id, "escalated")
        return {"ticket_id": ticket_id, "kind": "waiting", "session": tid}
    if not state.delivered:
        store.update_triage_session(tid, terminal_at=now(), outcome="no_output")
        store.insert_escalation(
            ticket_id,
            None,
            "review_blocked",
            f"triage session ended {state.status}/{state.status_detail} without a verdict",
        )
        store.set_ticket_status(ticket_id, "escalated")
        return {"ticket_id": ticket_id, "kind": "no_output", "session": tid}
    if out.get("ticket_id") != ticket_id:
        out = {**out, "ticket_id": ticket_id}
    try:
        ids = apply_verdict(store, ticket_id, out)
    except ValueError as ex:
        store.update_triage_session(tid, terminal_at=now(), outcome="invalid", verdict=out)
        store.insert_escalation(ticket_id, None, "review_blocked", str(ex)[:300])
        store.set_ticket_status(ticket_id, "escalated")
        return {"ticket_id": ticket_id, "kind": "invalid", "session": tid, "detail": str(ex)[:300]}
    store.update_triage_session(tid, terminal_at=now(), outcome="triaged", verdict=out)
    store.log(
        "L1 triage",
        "verdict accepted",
        ticket_id=ticket_id,
        detail=f"split {out['split']} · {len(out.get('sites', []))} site(s) · {len(ids)} work order(s) · needs_human {len(out.get('needs_human', []))}",
    )
    return {"ticket_id": ticket_id, "kind": "triaged", "session": tid, "work_orders": ids}


def triage_all(
    store: Store, client: Any, cfg: TargetConfig, *, ticket_id: str | None = None, **kw: Any
) -> list[dict[str, Any]]:
    tickets = [store.get_ticket(ticket_id)] if ticket_id else store.list_tickets("new")
    return [run_triage(store, client, t["id"], cfg, **kw) for t in tickets if t]
