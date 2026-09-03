"""The three numbers an engineering leader watches, and the liveness beside them.

Each one is a count over a stated denominator, computed here so the page cannot narrate
something the database does not hold. Nothing on this page is a percentage without its
denominator, and nothing is a percentile: the run is small and says so."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from swe_loop.store import Store

# What the acceptance commands are called tells us what kind of check ran.
LINT_WORDS = ("lint", "ruff", "format", "oxlint", "prettier", "mypy", "type")


def _kind_of(command: str) -> str:
    name = (command or "").split(":", 1)[0].lower()
    return "lint" if any(w in name for w in LINT_WORDS) else "tests"


def _latest_verdicts(store: Store) -> list[dict[str, Any]]:
    """One row per session that reached a decision: its most recent verdict."""
    return store._all(
        "SELECT v.* FROM verdicts v WHERE v.created_at = ("
        "  SELECT MAX(v2.created_at) FROM verdicts v2 WHERE v2.session_id = v.session_id)"
        " GROUP BY v.session_id"
    )


def verification(store: Store) -> dict[str, Any]:
    """Of the changes the AI produced, how many passed checks we ran ourselves.

    Three kinds of check are counted separately because they answer different doubts: the tests
    the repository already had, the linter it already had, and whether the session's own report
    was even the right shape."""
    latest = _latest_verdicts(store)
    passed = [v for v in latest if v["gate_result"] == "pass"]
    ev = store._all("SELECT tier, command, passed FROM evidence")
    tests = [e for e in ev if e["tier"] == "T1" and _kind_of(e["command"]) == "tests"]
    lint = [e for e in ev if e["tier"] == "T1" and _kind_of(e["command"]) == "lint"]
    scope = [e for e in ev if e["tier"] == "T0"]
    tri = store.list_triage_sessions()
    shaped = [t for t in tri if t["outcome"] in ("triaged", "invalid", "no_output")]
    return {
        "changes": len(latest),
        "changes_passed": len(passed),
        "kinds": [
            {
                "label": "the project's own tests",
                "n": sum(1 for e in tests if e["passed"]),
                "of": len(tests),
            },
            {"label": "the linter", "n": sum(1 for e in lint if e["passed"]), "of": len(lint)},
            {
                "label": "nothing edited that should not be",
                "n": sum(1 for e in scope if e["passed"]),
                "of": len(scope),
            },
            {
                "label": "the AI's report had the agreed shape",
                "n": sum(1 for t in shaped if t["outcome"] == "triaged"),
                "of": len(shaped),
            },
        ],
        "failed": [
            {
                "session": v["session_id"],
                "result": v["gate_result"],
                "reason": (v["reason"] or "")[:160],
            }
            for v in latest
            if v["gate_result"] != "pass"
        ],
    }


def intervention(store: Store) -> dict[str, Any]:
    """How often a person had to step in. Merging is not stepping in: it is the design.

    The two that count are a question the AI could not answer for itself, and work it handed
    back. Both are recorded when they happen, so neither can be quietly dropped."""
    tickets = store.list_tickets()
    answered = {
        e["ticket_id"]
        for e in store.timeline(limit=5000)
        if e["event"] == "answered by a person" and e["ticket_id"]
    }
    handed_back = {
        t["id"]
        for t in tickets
        if (t["router_decision"] and t["router_decision"] != "devin") or t["status"] == "escalated"
    }
    merges = store._all("SELECT ticket_id FROM human_actions WHERE kind='merge'")
    touched = answered | handed_back
    return {
        "tickets": len(tickets),
        "untouched": len([t for t in tickets if t["id"] not in touched]),
        "asked": len(answered),
        "handed_back": len(handed_back),
        "merges": len({m["ticket_id"] for m in merges}),
        "rows": [
            {"label": "ran without you, up to the merge", "n": len(tickets) - len(touched)},
            {"label": "asked you a question", "n": len(answered)},
            {"label": "handed back to your team", "n": len(handed_back)},
        ],
    }


def acceptance(store: Store) -> dict[str, Any]:
    """Of the changes offered to your team, how many were merged and how many were turned down.

    Offered means the checks passed and the change was put in front of a person. Turned down is
    read from the pull request itself, so a change closed on GitHub shows up here without anyone
    telling the app."""
    rows = store._all(
        "SELECT s.id, s.pull_request_url AS pr, s.pr_state, w.ticket_id, t.status "
        "FROM sessions s JOIN work_orders w ON w.id = s.work_order_id "
        "JOIN tickets t ON t.id = w.ticket_id WHERE s.pull_request_url IS NOT NULL"
    )
    passed = {v["session_id"] for v in _latest_verdicts(store) if v["gate_result"] == "pass"}
    offered = [r for r in rows if r["id"] in passed]
    merged = [r for r in offered if r["status"] == "merged" or r["pr_state"] == "merged"]
    closed = [r for r in offered if r["pr_state"] == "closed"]
    waiting = [r for r in offered if r not in merged and r not in closed]
    actions = store._all("SELECT DISTINCT actor_hash, at FROM human_actions WHERE kind='merge'")
    return {
        "mergers": len({a["actor_hash"] for a in actions}),
        "merge_events": len({a["at"] for a in actions}),
        "offered": len(offered),
        "merged": len(merged),
        "closed": len(closed),
        "waiting": len(waiting),
        "rows": [
            {"label": "merged by your team", "n": len(merged)},
            {"label": "turned down", "n": len(closed)},
            {"label": "waiting for a decision", "n": len(waiting)},
        ],
    }


def liveness(store: Store) -> dict[str, Any]:
    """Is it running right now, when did it last run, and what will start it next."""
    live = store.live_sessions()
    runs = store._all("SELECT * FROM automation_runs ORDER BY started_at DESC LIMIT 1") or [{}]
    last = runs[0]
    result = json.loads(last.get("result_json") or "{}") if last else {}
    autos = [a for a in store.list_automations() if a["enabled"] and a["availability"] == "live"]
    fail = store._all(
        "SELECT at, event, detail FROM timeline WHERE event LIKE '%failed%' "
        "OR event LIKE '%cannot run%' ORDER BY at DESC LIMIT 1"
    )
    return {
        "running": len(live),
        "last_run_at": (last or {}).get("finished_at") or (last or {}).get("started_at"),
        "last_run_status": (last or {}).get("status") or "never run",
        "last_run_result": result,
        "watching": [
            f"{a['name']}: {(a['trigger'] or {}).get('event', 'on click')}" for a in autos
        ],
        "last_failure": (fail[0] if fail else None),
    }


def claim_vs_check(store: Store) -> list[dict[str, Any]]:
    """One row per command: what the session said, and what happened when we ran it ourselves.

    The tree hash ties the result to the exact code it ran on. Evidence recorded against any
    other tree is invisible to the loop, so a session cannot bring its own receipts."""
    out = []
    for s in store._all(
        "SELECT s.*, w.ticket_id, w.shard_id FROM sessions s "
        "JOIN work_orders w ON w.id = s.work_order_id WHERE s.devin_session_id IS NOT NULL "
        "ORDER BY s.created_at"
    ):
        claim = json.loads(s["structured_output_json"] or "{}") or {}
        said = claim.get("acceptance") or {}
        for e in store.evidence_for(s["id"]):
            name = (e["command"] or "").split(":", 1)[0]
            out.append(
                {
                    "ticket_id": s["ticket_id"],
                    "session": (s["devin_session_id"] or "")[:12],
                    "url": s["url"],
                    "tier": e["tier"],
                    "name": "nothing edited outside the job" if e["tier"] == "T0" else name,
                    "said": ("passed" if said.get(name) in (0, "0", True) else "")
                    if e["tier"] == "T1"
                    else "",
                    "exit": e["exit_code"],
                    "passed": bool(e["passed"]),
                    "tree": (e["tree_hash"] or "")[:12],
                    "digest": (e["output_digest"] or "")[:12],
                    "log": f"/evidence/{e['id']}" if e.get("output_path") else "",
                }
            )
    return out


def window(store: Store) -> str:
    """The period every number on the page covers, said plainly."""
    row = store._one("SELECT MIN(at) AS lo, MAX(at) AS hi FROM timeline")
    if not row or not row["lo"]:
        return "no run recorded yet"

    def fmt(s: str) -> str:
        return datetime.fromisoformat(s).astimezone(UTC).strftime("%d %b %H:%M")

    return f"{fmt(row['lo'])} to {fmt(row['hi'])} UTC"
