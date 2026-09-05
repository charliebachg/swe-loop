"""What Devin says about its own sessions.

Session Insights is Devin's read on each session it ran: how many turns it took, how big it
judged the work, what it classified the work as and which tools it touched, and its own analysis
of what went wrong and what the prompt should have said. None of that is ours to compute, so the
whole payload is stored as it arrives and the page reads it back. The point of the page is to
show the agent's behaviour from the agent's own record, not from our poller's guesses.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from swe_loop.store import Store, now

# Fields that were the same on every session we have run. They are facts about the setup rather
# than measurements, so the page states them once instead of giving each a column of one value.
CONSTANT = ("origin", "devin_mode", "category", "subcategory", "session_size")


def refresh(store: Store, client: Any, session_ids: list[str] | None = None) -> int:
    """Pull Session Insights from the organisation and keep them. Returns how many were stored."""
    rows = client.t.list_insights(session_ids)
    for r in rows:
        if r.get("session_id"):
            store.put_insight(r["session_id"], r)
    return len(rows)


def written(row: dict[str, Any] | None) -> bool:
    """Whether Devin's analysis of the session has been written. The classification arrives on
    its own with every session; the issues, timeline and action items only after someone asks."""
    a = (row or {}).get("analysis") or {}
    return bool(a.get("issues") or a.get("timeline") or a.get("action_items"))


def generate(
    store: Store,
    client: Any,
    session_ids: list[str],
    *,
    wait_s: float = 240.0,
    every: float = 10.0,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = lambda _m: None,
) -> dict[str, Any]:
    """Ask Devin to analyse these sessions, then wait for the analyses and keep them. Free on
    Devin's side. Returns which sessions were written, which were already, which never came."""
    asked: list[str] = []
    already: list[str] = []
    for sid in session_ids:
        r = client.generate_insights(sid)
        (already if r.get("status") == "already_exists" else asked).append(sid)
        log(f"insights: {sid[:12]} {r.get('status', 'asked')}")
    pending = set(session_ids)
    waited = 0.0
    while pending:
        refresh(store, client, sorted(pending))
        pending = {sid for sid in pending if not written(store.insight(sid))}
        if not pending or waited >= wait_s:
            break
        sleep(every)
        waited += every
    done = [sid for sid in session_ids if sid not in pending]
    for sid in pending:
        if sid in already:
            # Devin says the analysis exists and returns it empty; asking again changes nothing,
            # so the page stops offering to ask
            store.set_setting(f"insights.empty.{sid}", now())
    for sid in done:
        store.set_setting(f"insights.empty.{sid}", "")
        store.log(
            "insights",
            "Devin's analysis of the session was written",
            session_id=_row_id(store, sid),
            detail=sid,
        )
    return {"asked": asked, "already": already, "written": done, "missing": sorted(pending)}


def _row_id(store: Store, devin_id: str) -> str | None:
    for table in ("sessions", "triage_sessions", "scan_sessions"):
        rows = store._all(f"SELECT id FROM {table} WHERE devin_session_id=?", devin_id)
        if rows:
            return rows[0]["id"]
    return None


def known_ids(store: Store) -> list[str]:
    """Every Devin session this store started, in any of the three tables."""
    ids = [
        r["devin_session_id"]
        for r in store._all("SELECT devin_session_id FROM sessions")
        + store._all("SELECT devin_session_id FROM triage_sessions")
        + store._all("SELECT devin_session_id FROM scan_sessions")
        if r["devin_session_id"]
    ]
    return sorted(set(ids))


def _classification(d: dict[str, Any]) -> dict[str, Any]:
    return ((d.get("analysis") or {}).get("classification")) or {}


def turns(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """How many times anyone had to speak to a session, by Devin's own count.

    One message from us is the playbook and nothing else, which is the case worth counting: the
    session was given its instructions and got on with it."""
    one = [r for r in rows if (r.get("num_user_messages") or 0) <= 1]
    replies: dict[int, int] = {}
    for r in rows:
        n = r.get("num_devin_messages") or 0
        replies[n] = replies.get(n, 0) + 1
    return {
        "one": len(one),
        "total": len(rows),
        "replies": [{"n": k, "sessions": v} for k, v in sorted(replies.items())],
    }


def by_size(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Devin's own sizing. L or above means the piece was cut too large for one session."""
    order = ["xs", "s", "m", "l", "xl"]
    counts = {k: 0 for k in order}
    for r in rows:
        k = (r.get("session_size") or "").lower()
        if k in counts:
            counts[k] += 1
    return [
        {"label": k.upper(), "n": counts[k], "too_big": k in ("l", "xl")}
        for k in order
        if counts[k] or k in ("xs", "s")
    ]


def tools(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What Devin says the sessions actually touched, counted across them."""
    seen: dict[str, int] = {}
    for r in rows:
        for t in _classification(r).get("tools_and_frameworks") or []:
            seen[t] = seen.get(t, 0) + 1
    # most used first, then alphabetically, so the order does not wander between runs
    return [{"name": k, "n": v} for k, v in sorted(seen.items(), key=lambda x: (-x[1], x[0]))]


def advice(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Devin's own analysis of what to change. Empty is a real answer and is said as one."""
    issues, actions, prompts, notes = [], [], [], []
    for r in rows:
        a = r.get("analysis") or {}
        sid = (r.get("session_id") or "")[:8]
        for i in a.get("issues") or []:
            issues.append({"session": sid, "url": r.get("url", ""), "text": _text(i)})
        for i in a.get("action_items") or []:
            actions.append({"session": sid, "url": r.get("url", ""), "text": _text(i)})
        if a.get("suggested_prompt"):
            prompts.append(
                {"session": sid, "url": r.get("url", ""), "text": _text(a["suggested_prompt"])}
            )
        if a.get("note_usage"):
            notes.append({"session": sid, "url": r.get("url", ""), "text": _text(a["note_usage"])})
    done = sum(1 for r in rows if written(r))
    return {
        "issues": issues,
        "actions": actions,
        "prompts": prompts,
        "notes": notes,
        "analysed": done,
        "total": len(rows),
    }


def _text(x: Any) -> str:
    if isinstance(x, str):
        return x
    if isinstance(x, dict):
        for k in ("text", "description", "title", "summary", "message"):
            if x.get(k):
                return str(x[k])
    return str(x)


def constants(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Fields that were identical on every session: stated once, not given a column each."""
    out = []
    for f in CONSTANT:
        vals = {r.get(f) for r in rows if r.get(f)}
        if len(vals) == 1:
            out.append({"field": f.replace("_", " "), "value": str(next(iter(vals)))})
    return out


def no_playbook(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sessions that ran with no playbook attached. Each one is a gap in the configuration."""
    return [
        {
            "session": (r.get("session_id") or "")[:8],
            "url": r.get("url", ""),
            "title": r.get("title", ""),
        }
        for r in rows
        if not r.get("playbook_id")
    ]
