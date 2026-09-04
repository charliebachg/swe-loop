"""Cost the org will not report: self-serve Devin plans are billed in dollar credits, and the API's
`acus_consumed` stays 0.0 for every session (verified 2026-09-04 on the org, the service user and
each session through /consumption/daily). What we can measure ourselves is active time: the gaps
between our own polls while the session reported `working`. A person reads the credits figure
from the console (Settings > Plans) once, and that calibrates active minutes into dollars."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime
from typing import Any

from swe_loop.store import Store

GAP_CAP_S = 60.0  # our polls are 5 to 30 s apart; a longer gap means we were not watching


def _ts(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(iso)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _active_from_events(events: list[dict[str, Any]], start: str | None, end: str | None) -> float:
    """Seconds the session was `working` between consecutive observations, each gap capped."""
    pts: list[tuple[datetime, bool]] = []
    t0 = _ts(start)
    if t0:
        pts.append((t0, True))
    for e in events:
        t = _ts(e.get("at"))
        if t is None:
            continue
        working = "working" in (e.get("event") or "") or "/-" in (e.get("event") or "")
        pts.append((t, working))
    t1 = _ts(end)
    if t1:
        pts.append((t1, False))
    pts.sort(key=lambda p: p[0])
    total = 0.0
    for (ta, working), (tb, _) in itertools.pairwise(pts):
        if working:
            total += min((tb - ta).total_seconds(), GAP_CAP_S)
    return total


def repair_active_seconds(store: Store, session: dict[str, Any]) -> float:
    events = [
        e for e in store.timeline(session_id=session["id"], limit=5000) if e["layer"] == "poll"
    ]
    return _active_from_events(events, session.get("created_at"), session.get("terminal_at"))


def triage_active_seconds(store: Store, tri: dict[str, Any]) -> float:
    lo, hi = _ts(tri.get("created_at")), _ts(tri.get("terminal_at"))
    events = []
    for e in store.timeline(ticket_id=tri["ticket_id"], limit=5000):
        if e["layer"] != "triage":
            continue
        t = _ts(e.get("at"))
        if t is None or (lo and t < lo) or (hi and t > hi):
            continue
        events.append(e)
    return _active_from_events(events, tri.get("created_at"), tri.get("terminal_at"))


def scan_active_seconds(store: Store, sc: dict[str, Any]) -> float:
    """A scan session's working time, from the same polls, on its own step name."""
    lo, hi = _ts(sc.get("created_at")), _ts(sc.get("terminal_at"))
    events = []
    for e in store.timeline(session_id=sc["id"], limit=5000):
        if e["layer"] != "scan":
            continue
        t = _ts(e.get("at"))
        if t is None or (lo and t < lo) or (hi and t > hi):
            continue
        events.append(e)
    return _active_from_events(events, sc.get("created_at"), sc.get("terminal_at"))


def calibration(store: Store) -> dict[str, Any]:
    """Dollars per active minute, from the credits figure a person entered and the minutes we
    had measured at that moment. None until a person enters the figure."""
    credits = store.get_setting("cost.credits_usd")
    minutes_then = store.get_setting("cost.active_min_at_entry")
    at = store.get_setting("cost.credits_at")
    try:
        c = float(credits) if credits else None
        m = float(minutes_then) if minutes_then else None
    except ValueError:
        c, m = None, None
    rate = (c / m) if (c is not None and m) else None
    return {"credits_usd": c, "active_min_at_entry": m, "at": at, "usd_per_active_min": rate}


# Dollars per active minute by session kind. Seeded from the 2026-09-03 run's console figures
# (repair $6.98 over 27.0 min, triage $7.80 over 13.5 min: a triage minute costs about twice a
# repair minute). Every console figure a person enters refines the rate for its kind.
DEFAULT_RATES = {"rep": 0.26, "tri": 0.58, "scn": 0.58}


def rates(store: Store) -> dict[str, float]:
    """Dollars per active minute for repair and triage sessions: from the console figures a person
    entered where there are any, the seeded defaults otherwise. The formula is ours; the pages
    show its result as the cost, and a person refreshes the console figures periodically."""
    out: dict[str, float] = {}
    for kind, table, fn in (
        ("rep", "sessions", repair_active_seconds),
        ("tri", "triage_sessions", triage_active_seconds),
        ("scn", "scan_sessions", scan_active_seconds),
    ):
        usd, secs = 0.0, 0.0
        for r in store._all(f"SELECT * FROM {table} WHERE cost_usd IS NOT NULL"):
            usd += float(r["cost_usd"])
            secs += fn(store, r)
        out[kind] = (usd / (secs / 60.0)) if secs > 0 and usd > 0 else DEFAULT_RATES[kind]
    return out


def session_usd(
    store: Store, row: dict[str, Any], kind: str, rate: Any = None
) -> tuple[float, str]:
    """(dollars, source): 'console' when a person entered the figure, 'rate' when computed from
    active minutes at the kind's rate. There is always a number."""
    if row.get("cost_usd") is not None:
        return float(row["cost_usd"]), "console"
    r = rate if isinstance(rate, dict) else rates(store)
    secs = {
        "rep": repair_active_seconds,
        "tri": triage_active_seconds,
        "scn": scan_active_seconds,
    }[kind](store, row)
    return secs / 60.0 * r[kind], "rate"


def observed_rate(store: Store) -> float | None:
    """One blended figure for labels: total dollars over total active minutes."""
    rs = rates(store)
    secs = {"rep": 0.0, "tri": 0.0}
    for r in store._all("SELECT * FROM sessions"):
        secs["rep"] += repair_active_seconds(store, r)
    for r in store.list_triage_sessions():
        secs["tri"] += triage_active_seconds(store, r)
    total_min = (secs["rep"] + secs["tri"]) / 60.0
    if total_min <= 0:
        return None
    return (secs["rep"] / 60.0 * rs["rep"] + secs["tri"] / 60.0 * rs["tri"]) / total_min


def spend(store: Store) -> dict[str, Any]:
    """The one cost picture: reported ACU (0 on self-serve plans), measured active minutes, and
    dollars: the console's figure per session where a person entered it, an estimate at the
    observed rate for the rest."""
    sessions = store._all("SELECT * FROM sessions")
    tri = store.list_triage_sessions()
    scn = store.list_scan_sessions()
    acu = (
        sum((s["acus_consumed"] or 0) for s in sessions)
        + sum((t["acus_consumed"] or 0) for t in tri)
        + sum((c["acus_consumed"] or 0) for c in scn)
    )
    active_s = (
        sum(repair_active_seconds(store, s) for s in sessions)
        + sum(triage_active_seconds(store, t) for t in tri)
        + sum(scan_active_seconds(store, c) for c in scn)
    )
    rate = observed_rate(store)
    kind_rates = rates(store)
    priced = sessions + tri + scn
    usd_console = sum(float(r["cost_usd"]) for r in priced if r.get("cost_usd") is not None)
    n_console = sum(1 for r in priced if r.get("cost_usd") is not None)
    usd_total = 0.0
    any_usd = False
    for kind, rows in (("rep", sessions), ("tri", tri), ("scn", scn)):
        for r in rows:
            u, _src = session_usd(store, r, kind, kind_rates)
            if u is not None:
                usd_total += u
                any_usd = True
    minutes = active_s / 60.0
    cal = calibration(store)
    return {
        "acu": round(acu, 2),
        "metered": acu > 0,
        "active_min": round(minutes, 1),
        "active_min_raw": minutes,
        "usd": round(usd_total, 2) if any_usd else None,
        "usd_console": round(usd_console, 2),
        "n_console": n_console,
        "n_sessions": len(sessions) + len(tri) + len(scn),
        "rate": rate,
        "rates": kind_rates,
        "calibrated_at": cal["at"],
        "credits_usd": cal["credits_usd"],
        "source": (
            "console"
            if n_console == len(sessions) + len(tri) and n_console
            else ("mixed" if n_console else ("estimate" if rate else "none"))
        ),
    }


def record_credits(store: Store, credits_usd: float) -> dict[str, Any]:
    """A person read the console; remember the figure and the minutes measured at that moment."""
    sp = spend(store)
    store.set_setting("cost.credits_usd", f"{credits_usd:.2f}")
    store.set_setting("cost.active_min_at_entry", f"{sp['active_min_raw']:.4f}")
    store.set_setting("cost.credits_at", datetime.now(UTC).isoformat(timespec="seconds"))
    store.log(
        "budget",
        f"credits used entered from the console: ${credits_usd:.2f} over {sp['active_min']:.1f} active minutes",
    )
    return spend(store)


def fmt_cost(minutes: float, rate: float | None) -> str:
    return f"{minutes:.1f} min" + (f" · ${minutes * rate:.2f}" if rate else "")
