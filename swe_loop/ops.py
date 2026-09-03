"""The operational view: every session, where each one is in the pipeline, and the feed of what
just happened. Reads the ticket store and the timeline; nothing else."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from swe_loop.store import Store

STEPS = [
    ("intake", "code"),
    ("triage", "devin"),
    ("route", "code"),
    ("dispatch", "code"),
    ("session", "devin"),
    ("gate", "gate"),
    ("review", "devin"),
    ("merge", "human"),
]

STATUS_PILL = {
    "running": "p-run",
    "working": "p-run",
    "new": "p-wait",
    "claimed": "p-wait",
    "reserved": "p-na",
    "exit": "p-ok",
    "finished": "p-ok",
    "error": "p-bad",
    "terminated": "p-bad",
    "usage_limit_exceeded": "p-bad",
    "waiting_for_user": "p-wait",
    "waiting_for_approval": "p-wait",
    "orphaned": "p-na",
}
TICKET_PILL = {
    "new": "p-na",
    "triaged": "p-na",
    "routed": "p-wait",
    "dispatched": "p-run",
    "running": "p-run",
    "gated": "p-run",
    "reviewed": "p-ok",
    "merged": "p-ok",
    "escalated": "p-bad",
    "refused": "p-bad",
}


def _elapsed(start: str | None, end: str | None) -> str:
    if not start:
        return ""
    try:
        a = datetime.fromisoformat(start)
        b = datetime.fromisoformat(end) if end else datetime.now(UTC)
    except ValueError:
        return ""
    a = a if a.tzinfo else a.replace(tzinfo=UTC)
    b = b if b.tzinfo else b.replace(tzinfo=UTC)
    secs = int((b - a).total_seconds())
    if secs < 90:
        return f"{secs}s"
    if secs < 5400:
        return f"{secs // 60}m {secs % 60:02d}s"
    return f"{secs // 3600}h {(secs % 3600) // 60:02d}m"


def _steps_for(
    session: dict[str, Any], ticket: dict[str, Any], verdict: dict[str, Any] | None, merged: bool
) -> tuple[list[dict], str]:
    """Which pipeline steps this session has passed, is at, or failed at."""
    done: set[str] = {"intake", "route"}
    if ticket.get("triage_verdict_json"):
        done.add("triage")
    if session.get("devin_session_id"):
        done.add("dispatch")
    now = None
    bad = None
    if (
        session.get("terminal_at")
        and session.get("status") == "exit"
        and session.get("status_detail") == "finished"
    ):
        done.add("session")
    elif session.get("terminal_at"):
        bad = "session"
    elif session.get("devin_session_id"):
        now = "session"
    if verdict:
        if verdict["gate_result"] == "pass":
            done.add("gate")
            if (verdict.get("review_severity") or "").startswith(("requested", "completed")):
                done.add("review")
        else:
            bad = "gate"
    elif "session" in done and not bad:
        now = "gate"
    if merged:
        done.add("merge")
    elif "review" in done and not now:
        now = "merge"
    out = []
    for name, kind in STEPS:
        cls = kind
        if name in done:
            cls += " done"
        elif name == bad:
            cls += " bad"
        elif name == now:
            cls += " now"
        out.append({"name": name, "cls": cls})
    label = " · ".join(n for n, _ in STEPS)
    return out, label


def build(store: Store) -> dict[str, Any]:
    tickets = {t["id"]: t for t in store.list_tickets()}
    merged_tickets = {
        r["ticket_id"]
        for r in store._all("SELECT DISTINCT ticket_id FROM human_actions WHERE kind='merge'")
    }
    sessions = []
    counts = {
        "running": 0,
        "needs_human": 0,
        "gated": 0,
        "passed": 0,
        "failed": 0,
        "merged": len(merged_tickets),
        "acus": 0.0,
    }
    for s in store._all("SELECT * FROM sessions ORDER BY created_at DESC, rowid DESC"):
        wo = store.get_work_order(s["work_order_id"])
        t = tickets.get(wo["ticket_id"], {}) if wo else {}
        v = store.latest_verdict(s["id"])
        ev = store.evidence_for(s["id"])
        latest_tree = ev[-1]["tree_hash"] if ev else None
        t1 = [e for e in ev if e["tier"] == "T1" and e["tree_hash"] == latest_tree]
        last = store.timeline(session_id=s["id"], limit=1)
        merged = wo["ticket_id"] in merged_tickets if wo else False
        steps, label = _steps_for(s, t, v, merged)
        detail = s.get("status_detail") or ""
        pill = STATUS_PILL.get(detail, STATUS_PILL.get(s.get("status") or "", "p-na"))
        live = s["devin_session_id"] and not s["terminal_at"]
        if live and detail in ("waiting_for_user", "waiting_for_approval"):
            counts["needs_human"] += 1
        elif live:
            counts["running"] += 1
        if v and v["gate_result"] == "pass":
            counts["passed"] += 1
        elif v:
            counts["failed"] += 1
        elif s["terminal_at"] and t.get("status") == "gated":
            counts["gated"] += 1
        elif s["terminal_at"] and s.get("status_detail") != "finished":
            counts["failed"] += 1
        counts["acus"] += s["acus_consumed"] or 0
        sessions.append(
            {
                "id": s["id"],
                "devin_id": s["devin_session_id"],
                "url": s["url"],
                "ticket": wo["ticket_id"] if wo else "",
                "shard": wo["shard_id"] if wo else "",
                "external_ref": t.get("external_ref"),
                "files": wo["files"] if wo else [],
                "status": s["status"] or "reserved",
                "status_detail": detail,
                "pill": pill,
                "steps": steps,
                "step_label": label,
                "pr_url": s["pull_request_url"],
                "gate": v["gate_result"] if v else None,
                "t1": f"T1 {sum(1 for e in t1 if e['passed'])}/{len(t1)}" if t1 else "",
                "acus": s["acus_consumed"],
                "size": s["session_size"],
                "retries": s["retries"],
                "last_event": f"{last[0]['layer']}: {last[0]['event']}" if last else "",
                "elapsed": _elapsed(s["created_at"], s["terminal_at"]),
            }
        )
    for tr in store.list_triage_sessions():
        counts["acus"] += tr["acus_consumed"] or 0
        if tr["devin_session_id"] and not tr["terminal_at"]:
            counts["running"] += 1
    counts["acus"] = round(counts["acus"], 2)
    counts["cap"] = store.budget_state().get("cap")
    escalations = store.list_escalations()
    counts["escalations"] = len(escalations)

    trows = []
    for t in tickets.values():
        n = sum(len(store.sessions_for(w["id"])) for w in store.work_orders_for(t["id"]))
        issue = (
            t["external_ref"].rsplit("#", 1)[-1]
            if t.get("external_ref") and "#" in t["external_ref"]
            else None
        )
        repo = t["external_ref"].rsplit("#", 1)[0] if issue else None
        trows.append(
            {
                "id": t["id"],
                "issue": issue,
                "issue_url": f"https://github.com/{repo}/issues/{issue}"
                if issue and repo and "/" in repo
                else None,
                "classes": (t["class"] or "").replace(",", ", "),
                "route": t["router_decision"],
                "status": t["status"],
                "pill": TICKET_PILL.get(t["status"], "p-na"),
                "sessions": n,
                "reason": (t["router_reason"] or "")[:140],
            }
        )
    feed = list(reversed(store.timeline(limit=60)))
    return {
        "sessions": sessions,
        "tickets": trows,
        "feed": feed,
        "escalations": escalations,
        "counts": counts,
    }


def session_detail(store: Store, sid: str) -> dict[str, Any] | None:
    s = store.get_session(sid)
    if not s:
        return None
    wo = store.get_work_order(s["work_order_id"])
    return {
        "session": s,
        "work_order": wo,
        "ticket": store.get_ticket(wo["ticket_id"]) if wo else None,
        "structured_output": json.loads(s["structured_output_json"])
        if s["structured_output_json"]
        else None,
        "evidence": store.evidence_for(sid),
        "verdicts": store._all(
            "SELECT * FROM verdicts WHERE session_id=? ORDER BY created_at, rowid", sid
        ),
        "timeline": store.timeline(session_id=sid, limit=500),
    }
