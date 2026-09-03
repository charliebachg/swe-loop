"""View models for the designed pages: the mock's field contract, filled from the real store.

The mock (`swe-loop v2`) was built as a clickable component with its own state; here every
interaction is a URL the server renders, so nothing lives only in the browser. Colours and labels
are computed the way the mock computed them, from the same constants."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from swe_loop import cost, ops, pages
from swe_loop import reduce as reduce_mod
from swe_loop import report as report_mod
from swe_loop.config import Settings, TargetConfig
from swe_loop.store import Store
from swe_loop.triage import TRIAGE_ACU_CAP

ACT = {
    "code": ("#5b6f8a", "#eaeef4", "code"),
    "devin": ("#7a4fb5", "#f1eafa", "Devin"),
    "gate": ("#1f8a80", "#e0f3f1", "the gate"),
    "person": ("#b8862a", "#fff3d6", "a person"),
    "next": ("#8f97a3", "#e9e7e1", "next"),
}
PL = {
    "ok": ("#2e7d4f", "#e3f2e8"),
    "bad": ("#b4452e", "#f9e4df"),
    "run": ("#b8862a", "#fff3d6"),
    "na": ("#8f97a3", "#e9e7e1"),
    "devin": ("#7a4fb5", "#f1eafa"),
    "person": ("#b8862a", "#fff3d6"),
    "gate": ("#1f8a80", "#e0f3f1"),
}
TK = {"A": "#2c5ba6", "B": "#4f9a6b", "C": "#c48a3a", "D": "#8a5fb5", "E": "#b4452e"}
_TK_MORE = ["#2c5ba6", "#4f9a6b", "#c48a3a", "#8a5fb5", "#b4452e", "#1f8a80", "#7a4fb5", "#5b6f8a"]
INK, FAINT, MUTED = "#14181f", "#8f97a3", "#626b78"
PURPLE, TEAL = "#7a4fb5", "#1f8a80"
STG = [
    ("intake", "code"),
    ("triage", "devin"),
    ("route", "code"),
    ("dispatch", "code"),
    ("session", "devin"),
    ("gate", "gate"),
    ("review", "devin"),
    ("merge", "person"),
]
PAGES = {
    "home": ("Home", "/"),
    "automations": ("Automations", "/automations"),
    "tickets": ("Tickets", "/tickets-page"),
    "tracker": ("Tracker", "/tracker"),
    "report": ("Report", "/report"),
    "sessions": ("Sessions", "/devin/sessions"),
    "playbooks": ("Playbooks", "/devin/playbooks"),
    "knowledge": ("Knowledge", "/devin/knowledge"),
    "insights": ("Insights", "/devin/insights"),
    "review": ("Review", "/devin/review"),
    "integrations": ("Integrations", "/devin/integrations"),
    "next": ("Next", "/devin/next"),
    "settings": ("Settings", "/settings"),
}
JOURNEY = ["home", "automations", "tickets", "tracker", "report"]

# Plain words for a reader who has never used an AI engineer. The internal name stays in tooltips.
ACTOR_PLAIN = {
    "code": "automatic",
    "devin": "AI engineer",
    "gate": "independent checks",
    "person": "your team",
    "next": "later",
}
STAGE_PLAIN = [
    ("issues received", "from the repository", "intake"),
    ("scoped by the AI", "read, understood, a plan written", "triage"),
    ("who fixes it", "decided automatically from the plan", "route"),
    ("fix started", "an AI work session opened", "dispatch"),
    ("fix written", "code changed, tests run by the AI", "session"),
    ("re-tested independently", "the same tests, on a clean copy", "gate"),
    ("reviewed by the AI reviewer", "a second reading of the change", "review"),
    ("shipped by your team", "merged by a person, never automatic", "merge"),
]
KIND_PLAIN = {
    "human_only": "needs your team",
    "refuse": "not for the AI",
    "waiting_for_user": "the AI has a question",
    "review_blocked": "did not finish",
    "usage_limit": "too big for one run",
    "oracle_touched": "tests were edited",
    "ready to merge": "ready to ship",
}
LAYER_PLAIN = {
    "L0 intake": "received",
    "L1 triage": "AI scoping",
    "L2 route": "decision",
    "L3 shard": "split",
    "L4 dispatch": "AI started",
    "L4 poll": "AI working",
    "L4 manage": "AI steered",
    "L5 gate": "checks",
    "L6 review": "AI review",
    "L7 reduce": "shipping",
    "ticket": "status",
    "escalate": "for your team",
    "automation": "trigger",
    "budget": "budget",
    "playbook": "procedure",
}
ACU_HELP = "ACU, Agent Compute Unit: Devin's unit of work, about 15 minutes of an AI session"
DEVIN_NAV = [
    ("sessions", 1, ""),
    ("playbooks", 1, ""),
    ("knowledge", 1, ""),
    ("insights", 1, ""),
    ("review", 1, ""),
    ("integrations", 1, ""),
    ("next", 0, "next"),
]


# ---------------------------------------------------------------------------- helpers
def url(path: str, **q: Any) -> str:
    q = {k: v for k, v in q.items() if v not in (None, "", False)}
    return path + ("?" + urlencode(q) if q else "")


def letter(ticket_id: str) -> str:
    return ticket_id.removeprefix("tkt_")[:2]


def tk_color(ticket_id: str) -> str:
    L = letter(ticket_id)
    if L in TK:
        return TK[L]
    return _TK_MORE[sum(map(ord, L)) % len(_TK_MORE)]


def dot(ticket_id: str, done: bool) -> dict[str, Any]:
    c = tk_color(ticket_id)
    return {
        "L": letter(ticket_id),
        "color": c,
        "dotBg": c if done else "#fff",
        "dotFg": "#fff" if done else c,
    }


def pill(kind: str) -> dict[str, str]:
    fg, bg = PL[kind]
    return {"bg": bg, "fg": fg}


def _pill_kind(css: str) -> str:
    return {
        "p-run": "run",
        "p-ok": "ok",
        "p-bad": "bad",
        "p-wait": "run",
        "p-na": "na",
        "p-devin": "devin",
        "p-gate": "gate",
        "p-person": "person",
    }.get(css, "na")


def _actor_for_layer(layer: str, event: str = "") -> str:
    lay = layer.lower()
    if lay.startswith(("l1", "l6")) or "review" in lay:
        return "devin"
    if lay.startswith("l5") or "gate" in lay:
        return "gate"
    if lay.startswith("l7") or "human" in lay or "merge" in lay or lay == "escalate":
        return "person"
    if lay == "ticket":
        e = event.lower()
        if e in ("running", "dispatched", "gated"):
            return "devin" if e != "gated" else "gate"
        if e in ("merged",):
            return "person"
        if e in ("reviewed",):
            return "devin"
    return "code"


def _bad_event(event: str, detail: str = "") -> bool:
    e = (event + " " + detail).lower()
    return any(
        w in e
        for w in (
            "fail",
            "escalat",
            "error",
            "terminated",
            "refused",
            "too_large",
            "usage_limit",
            "no_output",
            "rejected",
        )
    )


def _hhmmss(iso: str | None) -> str:
    return (iso or "")[11:19]


def ev(e: dict[str, Any]) -> dict[str, Any]:
    actor = _actor_for_layer(e.get("layer", ""), e.get("event", ""))
    detail = e.get("detail") or ""
    return {
        "time": _hhmmss(e.get("at")),
        "layer": e.get("layer", ""),
        "event": e.get("event", ""),
        "detail": detail,
        "ref": e.get("ticket_id") or "",
        "fg": ACT[actor][0],
        "bg": ACT[actor][1],
        "evColor": PL["bad"][0] if _bad_event(e.get("event", ""), detail) else INK,
        "hasDetail": bool(detail),
        "link": url("/tracker", open=e.get("ticket_id")) if e.get("ticket_id") else "/tracker",
        "session_id": e.get("session_id"),
    }


def _median(xs: list[float]) -> float:
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else 0.0


def _p95(xs: list[float]) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(0.95 * (len(xs) - 1)))] if xs else 0.0


def _fmt_acu(x: Any) -> str:
    try:
        return f"{float(x):.1f}"
    except (TypeError, ValueError):
        return "0.0"


def _pct(x: Any, cap: Any) -> str:
    try:
        return f"{min(100.0, 100.0 * float(x) / float(cap)):.1f}" if cap else "0"
    except (TypeError, ValueError, ZeroDivisionError):
        return "0"


def _short_status(status: str | None, detail: str | None, delivered: bool) -> str:
    """One word for the pill: what the session is doing, not the raw pair."""
    d = detail or ""
    if status == "exit" and d == "finished":
        return "finished"
    if d == "waiting_for_user":
        return "delivered" if delivered else "asking"
    if d == "waiting_for_approval":
        return "needs approval"
    if d == "usage_limit_exceeded":
        return "too large"
    if d in ("terminated", "error", "inactivity", "out_of_credits"):
        return d
    if status in ("running", "claimed"):
        return "working"
    return status or "reserved"


def _status_kind(status: str | None, detail: str | None, terminal: bool) -> str:
    d = detail or ""
    if status == "exit" and d == "finished":
        return "ok"
    if d in ("waiting_for_user", "waiting_for_approval"):
        return "run" if not terminal else "ok"
    if d in ("usage_limit_exceeded", "terminated", "error") or status == "error":
        return "bad"
    if status in ("running", "new", "claimed"):
        return "run"
    return "na"


def cost_rows(store: Store) -> list[dict[str, Any]]:
    """Every session with its Devin id, minutes, and the console figure if entered: the Settings form."""
    rate = cost.observed_rate(store)
    out = []
    for r in store._all("SELECT * FROM sessions ORDER BY created_at"):
        wo = store.get_work_order(r["work_order_id"]) or {}
        out.append(
            {
                "devin_id": r["devin_session_id"] or "",
                "label": f"repair {wo.get('ticket_id', '')} shard {wo.get('shard_id', '')}",
                "minutes": f"{cost.repair_active_seconds(store, r) / 60.0:.1f}",
                "usd": r.get("cost_usd"),
                "est": (
                    f"{cost.repair_active_seconds(store, r) / 60.0 * rate:.2f}"
                    if rate and r.get("cost_usd") is None
                    else ""
                ),
            }
        )
    for r in store.list_triage_sessions():
        out.append(
            {
                "devin_id": r["devin_session_id"] or "",
                "label": f"triage {r['ticket_id']}",
                "minutes": f"{cost.triage_active_seconds(store, r) / 60.0:.1f}",
                "usd": r.get("cost_usd"),
                "est": (
                    f"{cost.triage_active_seconds(store, r) / 60.0 * rate:.2f}"
                    if rate and r.get("cost_usd") is None
                    else ""
                ),
            }
        )
    return [x for x in out if x["devin_id"]]


def _cost_help(sp: dict[str, Any]) -> str:
    base = "this plan is billed in dollar credits, not ACU; the API reports 0 ACU for every session. Dollars are the console's own figures per session"
    if sp["source"] == "console":
        detail = " (every session entered)"
    elif sp["source"] == "mixed":
        detail = f" ({sp['n_console']} of {sp['n_sessions']} entered; the rest estimated at ${sp['rate']:.2f} per active minute)"
    elif sp["rate"]:
        detail = f"; estimated at ${sp['rate']:.2f} per active minute"
    else:
        detail = "; enter them in Settings"
    return (
        base
        + detail
        + f". Active minutes are measured from our own polls: {sp['active_min']:.1f} min"
    )


def usd_label(sp: dict[str, Any]) -> str:
    if sp["usd"] is None:
        return ""
    return f"${sp['usd']:.2f}" + ("" if sp["source"] == "console" else " est.")


# ---------------------------------------------------------------------------- frame
def frame(settings: Settings, cfg: TargetConfig, store: Store, active: str) -> dict[str, Any]:
    b = store.budget_state()
    sp = cost.spend(store)
    live = settings.live
    nav = []
    for i, key in enumerate(JOURNEY):
        on = key == active
        label, href = PAGES[key]
        nav.append(
            {
                "label": label,
                "href": href,
                "on": on,
                "color": "#fff" if on else "#c8ccd3",
                "bg": "#1f2530" if on else "transparent",
                "shadow": "inset 3px 0 0 #2457a8" if on else "none",
                "num": str(i + 1),
                "numBg": "#2457a8" if on else "transparent",
                "numFg": "#fff" if on else "#8b93a0",
                "numBorder": "#2457a8" if on else "#3a4352",
            }
        )
    nav_devin = []
    for key, built, tag in DEVIN_NAV:
        on = key == active
        label, href = PAGES[key]
        nav_devin.append(
            {
                "label": label,
                "href": href,
                "title": label,
                "color": "#6c7380" if tag else ("#fff" if on else "#c8ccd3"),
                "bg": "#1f2530" if on else "transparent",
                "shadow": "inset 3px 0 0 #2457a8" if on else "none",
                "dotBg": "transparent" if tag else "#2e9a6a",
                "dotBorder": "1.5px solid #6c7380" if tag else "0",
                "tag": tag,
            }
        )
    step = (
        f"step {JOURNEY.index(active) + 1} of 5"
        if active in JOURNEY
        else ("our side" if active == "settings" else "Devin")
    )
    return {
        "nav": nav,
        "navDevin": nav_devin,
        "pageTitle": PAGES.get(active, (active.title(), ""))[0],
        "pageStep": step,
        "modeLabel": "LIVE" if live else "RECORDED RUN",
        "modeHelp": "connected to the Devin organisation; sessions are real"
        if live
        else "showing a recorded run of the real system; no AI session is started from this page",
        "acuHelp": ACU_HELP if sp["metered"] else _cost_help(sp),
        "modeBg": PL["ok"][1] if live else PL["run"][1],
        "modeFg": PL["ok"][0] if live else PL["run"][0],
        "modeSmall": "live" if live else "replay",
        "modeSideFg": "#5fc08a" if live else "#e0b45a",
        "modeSentence": "Live mode:" if live else "Replay mode:",
        "modeNote": "every number below comes from sessions on the org"
        if live
        else "rendered from the recorded run, no Devin key",
        "repo": cfg.repo,
        "branch": cfg.base_branch,
        "acuSpent": (
            _fmt_acu(b.get("spent"))
            if sp["metered"]
            else (usd_label(sp) or f"{sp['active_min']:.0f} min")
        ),
        "acuCap": (
            (f"{b['cap']:.0f}" if b.get("cap") else "no cap")
            if sp["metered"]
            else (
                f"{sp['active_min']:.0f} min of AI work"
                if sp["usd"] is not None
                else "dollars: enter in Settings"
            )
        ),
        "acuPct": _pct(b.get("spent"), b.get("cap")) if sp["metered"] else "0",
        "costUnit": "ACU" if sp["metered"] else "",
        "perSession": f"{b['per_session_cap']:.0f}"
        if b.get("per_session_cap")
        else str(cfg.max_acu_limit),
        # legacy pages still read these
        "mode": settings.mode,
        "target": cfg.repo,
        "active": active,
        "goTracker": "/tracker",
        "goSessions": "/devin/sessions",
        "goTickets": "/tickets-page",
    }


def _now(counts: dict[str, Any]) -> dict[str, Any]:
    def col(n: Any, good: str | None = None) -> str:
        if not n:
            return FAINT
        return good or INK

    return {
        "running": counts["running"],
        "runningColor": col(counts["running"], PL["run"][0]),
        "waiting": counts["needs_human"],
        "waitingColor": col(counts["needs_human"], PL["run"][0]),
        "gated": counts["gated"],
        "gatedColor": col(counts["gated"], PL["gate"][0]),
        "passed": counts["passed"],
        "passedColor": col(counts["passed"], PL["gate"][0]),
        "failed": counts["failed"],
        "failedColor": col(counts["failed"], PL["bad"][0]),
        "merged": counts["merged"],
        "mergedColor": col(counts["merged"], PL["person"][0]),
    }


# ---------------------------------------------------------------------------- stage position per ticket
def _pattern(store: Store, t: dict[str, Any]) -> str:
    """Eight characters, one per stage: d done, n now, b refused or blocked, - not reached."""
    status = t["status"]
    verdict = bool(t.get("triage_verdict_json"))
    routed = bool(t.get("router_decision"))
    human = t.get("router_decision") in ("human_only", "refuse")
    wos = store.work_orders_for(t["id"])
    sess = [s for w in wos for s in store.sessions_for(w["id"])]
    merged = status == "merged"
    tri = store.list_triage_sessions(t["id"])
    tri_waiting = any(
        x["outcome"] in ("waiting", "no_output", "invalid", "too_large") for x in tri
    ) or (status == "escalated" and not verdict)
    p = ["d", "-", "-", "-", "-", "-", "-", "-"]
    p[1] = "b" if tri_waiting else ("d" if verdict else "n")
    if not verdict and not tri_waiting:
        return "".join(p)
    p[2] = "d" if routed else "n"
    if not routed:
        return "".join(p)
    if human:
        p[3] = "b"
        return "".join(p)
    p[3] = "d" if sess else "n"
    if not sess:
        return "".join(p)
    finished = [
        s
        for s in sess
        if s["terminal_at"] and s["status"] == "exit" and s["status_detail"] == "finished"
    ]
    delivered = [s for s in sess if s["terminal_at"]]
    p[4] = "d" if delivered else "n"
    if status == "escalated" and not delivered:
        p[4] = "b"
        return "".join(p)
    if not delivered:
        return "".join(p)
    passed = any(
        store.latest_verdict(s["id"]) and store.latest_verdict(s["id"])["gate_result"] == "pass"
        for s in sess
    )
    failed = any(
        store.latest_verdict(s["id"]) and store.latest_verdict(s["id"])["gate_result"] != "pass"
        for s in sess
    )
    p[5] = "d" if passed else ("b" if failed or status == "escalated" else "n")
    if not passed:
        return "".join(p)
    reviewed = any((store.latest_verdict(s["id"]) or {}).get("review_severity") for s in sess)
    p[6] = "d" if reviewed else "n"
    if not reviewed:
        return "".join(p)
    p[7] = "d" if merged else "n"
    _ = finished
    return "".join(p)


STATE_NAME = {"d": "done", "n": "now", "b": "refused or blocked", "-": "not reached"}


def _pos(pat: str) -> int:
    for i, ch in enumerate(pat):
        if ch in "nb":
            return i
    return len(pat) - 1


# ---------------------------------------------------------------------------- home
def home(store: Store, cfg: TargetConfig, q: dict[str, str]) -> dict[str, Any]:
    h = pages.home(store)
    counts = h["counts"]
    tickets = store.list_tickets()
    pats = {t["id"]: _pattern(store, t) for t in tickets}
    verdicts = [t for t in tickets if t.get("triage_verdict_json")]
    decided = [t for t in tickets if t.get("router_decision")]
    to_devin = [t for t in decided if t["router_decision"] == "devin"]
    to_person = [t for t in decided if t["router_decision"] != "devin"]
    wos = [w for t in tickets for w in store.work_orders_for(t["id"])]
    sess = store._all("SELECT * FROM sessions")
    sum((x["acus_consumed"] or 0) for x in store.list_triage_sessions())
    rep_acu = sum((s["acus_consumed"] or 0) for s in sess)
    passed = [s for s in sess if (store.latest_verdict(s["id"]) or {}).get("gate_result") == "pass"]
    [s for s in sess if s["retries"]]
    reviewed = [s for s in sess if (store.latest_verdict(s["id"]) or {}).get("review_severity")]
    merged = [t for t in tickets if t["status"] == "merged"]
    ready = h["summary"]["ready"]
    loop_counts = [
        (len(tickets), f"{len(tickets)} issue{'s' if len(tickets) != 1 else ''} filed"),
        (len(verdicts), f"{len(verdicts)} plan{'s' if len(verdicts) != 1 else ''} written"),
        (len(decided), f"{len(to_devin)} to the AI · {len(to_person)} to your team"),
        (
            len([w for w in wos if w["status"] in ("dispatched", "devin")]),
            f"{len(to_person)} held for your team" if to_person else "all started",
        ),
        (len(sess), f"{len(sess)} fix{'es' if len(sess) != 1 else ''} · {rep_acu:.1f} ACU used"),
        (
            len(passed),
            f"{len(passed)} passed · {sum(1 for x in sess if store.latest_verdict(x['id']) and store.latest_verdict(x['id'])['gate_result'] != 'pass')} failed",
        ),
        (len(reviewed), f"{len(reviewed)} reviewed"),
        (len(merged), f"{len(merged)} shipped · {len(ready)} waiting for you"),
    ]
    loop = []
    for i, (name, actor) in enumerate(STG):
        dots = []
        for t in tickets:
            pat = pats[t["id"]]
            if _pos(pat) != i:
                continue
            st = pat[i]
            d = dot(t["id"], st == "d")
            issue = (
                (t.get("external_ref") or "").rsplit("#", 1)[-1]
                if t.get("external_ref") and "#" in t["external_ref"]
                else letter(t["id"])
            )
            state = (
                "done here"
                if st == "d"
                else ("held for your team" if st == "b" else "waiting here")
            )
            dots.append(
                {
                    **d,
                    "L": f"#{issue}" if issue.isdigit() else issue,
                    "bg": d["dotBg"],
                    "fg": d["dotFg"],
                    "ring": d["color"],
                    "title": f"#{issue} {t.get('title', '')[:70]} · {state}",
                    "go": url("/tracker", open=t["id"]),
                }
            )
        plain, meaning, internal = STAGE_PLAIN[i]
        loop.append(
            {
                "name": plain,
                "meaning": meaning,
                "internal": internal,
                "actor": ACTOR_PLAIN[actor],
                "color": ACT[actor][0],
                "count": str(loop_counts[i][0]),
                "context": loop_counts[i][1],
                "numColor": PL["person"][0] if i == 7 else INK,
                "dots": dots,
            }
        )
    needs = []
    for n in h["needs"]:
        kind = "ok" if n["kind"] == "ready to merge" else "bad"
        needs.append(
            {
                **dot(n["ticket_id"], False),
                "kind": n["kind"],
                **pill(kind),
                "reason": n["reason"],
                "action": n["action"],
                "go": url("/tracker", open=n["ticket_id"]),
            }
        )
    raw_mode = q.get("tl") == "raw"
    open_groups = {x for x in (q.get("open") or "").split(",") if x}
    events = [ev(e) for e in reversed(store.timeline(limit=40))]
    groups = _group_events(events, open_groups, raw_mode)
    on, off = ("#e2e9f6", "#2457a8"), ("transparent", "#8f97a3")
    b = store.budget_state()
    sp0 = cost.spend(store)
    conc = next((r["concurrency"] for r in store.list_automations() if r["kind"] == "repair"), 4)
    gate_n = len(passed)
    gate_total = sum(1 for x in sess if store.latest_verdict(x["id"]))
    verified = len(merged)
    blocking = [x for x in needs if x["kind"] != "ready to merge"]
    five = [
        {
            "n": str(counts["running"]),
            "of": f"of {conc} at once",
            "label": "AI sessions working now",
            "color": PL["run"][0] if counts["running"] else FAINT,
            "pct": None,
        },
        {
            "n": str(counts["needs_human"] + len(blocking)),
            "of": "",
            "label": "waiting for your team",
            "color": PL["bad"][0] if (counts["needs_human"] or blocking) else FAINT,
            "pct": None,
        },
        (
            {
                "n": _fmt_acu(b.get("spent")),
                "of": f"of {b['cap']:.0f} ACU" if b.get("cap") else "no cap",
                "label": "compute used",
                "color": INK,
                "help": ACU_HELP,
                "pct": _pct(b.get("spent"), b.get("cap")),
            }
            if sp0["metered"]
            else {
                "n": (usd_label(sp0) or f"{sp0['active_min']:.0f} min"),
                "of": (
                    f"{sp0['active_min']:.0f} min of AI work"
                    if sp0["usd"] is not None
                    else "not yet in dollars"
                ),
                "label": "AI cost" if sp0["usd"] is not None else "AI working time",
                "color": INK,
                "help": _cost_help(sp0),
                "pct": None,
            }
        ),
        {
            "n": str(verified),
            "of": f"of {len(decided)} planned",
            "label": "fixed, re-tested and shipped",
            "color": PL["ok"][0] if verified else FAINT,
            "pct": None,
        },
        {
            "n": f"{gate_n}/{gate_total}" if gate_total else "0/0",
            "of": "pass rate",
            "label": "gate",
            "color": PL["gate"][0] if gate_n else FAINT,
            "pct": None,
        },
    ]
    short_needs = []
    for n in needs:
        tid = n["L"] if n["L"].startswith("tkt_") else f"tkt_{n['L']}"
        t = store.get_ticket(tid) or {}
        what = (t.get("title") or "")[:64]
        if n["kind"] == "ready to merge":
            mn = reduce_mod.merge_notes(store, tid)
            what = " · ".join(mn["reviews"]) + (
                f" · {len(mn['notes'])} note(s)" if mn["notes"] else ""
            )
        short_needs.append({**n, "what": what or n["reason"][:64], "hover": n["reason"]})
    spark = _sparklines(store)
    issue_no = {
        t["id"]: ("#" + t["external_ref"].rsplit("#", 1)[-1])
        for t in tickets
        if t.get("external_ref") and "#" in t["external_ref"]
    }
    # sessions that finished without a person typing anything into them
    tri = store.list_triage_sessions()
    tl = store.timeline(limit=2000)
    answered_triage = {e["ticket_id"] for e in tl if e["event"] == "answered by a person"}
    answered_repair = {
        e["session_id"] for e in tl if e["event"] == "answered waiting_for_user from the work order"
    }
    all_sessions = [("tri", x) for x in tri] + [("rep", x) for x in sess]
    quiet = 0
    for kind, x in all_sessions:
        asked = x["ticket_id"] in answered_triage if kind == "tri" else x["id"] in answered_repair
        if not asked:
            quiet += 1
    oldest_wait = None
    for e in store.list_escalations():
        oldest_wait = (
            e["created_at"] if oldest_wait is None or e["created_at"] < oldest_wait else oldest_wait
        )
    tiles = [
        {**five[0], "svg": spark[0]["svg"], "note": spark[0]["span"]},
        {
            **five[1],
            "of": (f"oldest {_age(oldest_wait)}" if oldest_wait else ""),
            "svg": "",
            "note": "",
        },
        {**five[2], "svg": spark[1]["svg"], "note": spark[1]["span"]},
        {**five[3], "svg": spark[2]["svg"], "note": spark[2]["last"]},
        {
            "n": f"{quiet} of {len(all_sessions)}" if all_sessions else "0 of 0",
            "of": "sessions",
            "label": "fixes that needed no help",
            "color": PL["ok"][0] if quiet else FAINT,
            "pct": None,
            "svg": "",
            "note": f"{len(all_sessions) - quiet} asked your team a question"
            if all_sessions
            else "",
        },
    ]
    for tile in tiles:
        tile.setdefault("help", "")
    inbox = []
    for e in store.list_escalations():
        t = store.get_ticket(e["ticket_id"]) or {}
        tri_for = store.list_triage_sessions(e["ticket_id"])
        can_answer = (
            e["kind"] in ("waiting_for_user", "human_only", "review_blocked")
            and bool(tri_for)
            and t.get("status") in ("escalated", "new")
        )
        inbox.append(
            {
                **dot(e["ticket_id"], False),
                "kind": KIND_PLAIN.get(e["kind"], e["kind"]),
                **pill("bad"),
                "what": (t.get("title") or e["reason"])[:70],
                "hover": e["reason"],
                "age": _age(e["created_at"]),
                "go": url("/tracker", open=e["ticket_id"]),
                "answerUrl": f"/tickets/{e['ticket_id']}/answer" if can_answer else "",
                "mergeUrl": "",
                "dismissUrl": f"/escalations/{e['id']}/resolve",
            }
        )
    for tid in h["summary"]["ready"]:
        mn = reduce_mod.merge_notes(store, tid)
        inbox.append(
            {
                **dot(tid, False),
                "kind": KIND_PLAIN["ready to merge"],
                **pill("ok"),
                "what": (
                    " · ".join(mn["reviews"])
                    .replace("comment(s)", "reviewer remarks")
                    .replace("no issues", "reviewer found nothing")
                    + (f" · {len(mn['notes'])} note(s)" if mn["notes"] else "")
                )
                or "gate passed, reviewed",
                "hover": next(
                    (
                        x["reason"]
                        for x in h["needs"]
                        if x["ticket_id"] == tid and x["kind"] == "ready to merge"
                    ),
                    "every shard passed the gate and was reviewed; merge on GitHub first, then record it here",
                ),
                "age": _age(
                    next(
                        (
                            v["created_at"]
                            for v in store._all(
                                "SELECT v.created_at FROM verdicts v JOIN sessions s ON s.id=v.session_id JOIN work_orders w ON w.id=s.work_order_id WHERE w.ticket_id=? ORDER BY v.created_at DESC LIMIT 1",
                                tid,
                            )
                        ),
                        None,
                    )
                ),
                "go": url("/tracker", open=tid),
                "answerUrl": "",
                "mergeUrl": f"/tickets/{tid}/merge-form",
                "dismissUrl": "",
            }
        )
    inflight = []
    for x in tri:
        if x["devin_session_id"] and not x["terminal_at"]:
            inflight.append(
                {
                    **dot(x["ticket_id"], False),
                    "ticket": x["ticket_id"],
                    "stage": "scoping",
                    "elapsed": ops._elapsed(x["created_at"], None),
                    "acu": _fmt_acu(x["acus_consumed"]),
                    "cap": f"{TRIAGE_ACU_CAP}",
                    "pct": _pct(x["acus_consumed"], TRIAGE_ACU_CAP),
                    "last": (store.timeline(ticket_id=x["ticket_id"], limit=1) or [{}])[0].get(
                        "event", ""
                    ),
                    "needsInput": x["status_detail"] == "waiting_for_user",
                    "go": url("/tracker", open=x["ticket_id"]),
                }
            )
    for x in store.live_sessions():
        wo = store.get_work_order(x["work_order_id"]) or {}
        tid = wo.get("ticket_id", "")
        st_row = store.get_ticket(tid) or {}
        pat = _pattern(store, st_row) if st_row else "--------"
        stage = STAGE_PLAIN[_pos(pat)][0]
        inflight.append(
            {
                **dot(tid, False),
                "ticket": tid,
                "stage": stage,
                "elapsed": ops._elapsed(x["created_at"], None),
                "acu": _fmt_acu(x["acus_consumed"]),
                "cap": f"{b['per_session_cap']:.0f}" if b.get("per_session_cap") else "·",
                "pct": _pct(x["acus_consumed"], b.get("per_session_cap")),
                "last": (store.timeline(session_id=x["id"], limit=1) or [{}])[0].get("event", ""),
                "needsInput": x["status_detail"] in ("waiting_for_user", "waiting_for_approval")
                and not x.get("pull_request_url"),
                "go": url("/tracker", open=tid),
            }
        )
    enabled = [r for r in store.list_automations() if r["enabled"] and r["availability"] == "live"]
    next_trigger = (
        f"a pull request on {enabled[0]['target']}"
        if enabled and enabled[0]["trigger"].get("event") == "pull_request"
        else (
            f"{enabled[0]['trigger'].get('event', '')} on {enabled[0]['target']}"
            if enabled
            else "no automation is switched on"
        )
    )
    return {
        "tiles": tiles,
        "inbox": inbox,
        "inflight": inflight,
        "nextTrigger": next_trigger,
        "five": five,
        "shortNeeds": short_needs,
        "spark": spark,
        "recent8": [
            {
                **e,
                "plain": LAYER_PLAIN.get(e["layer"], e["layer"]),
                "ref": issue_no.get(e["ref"], e["ref"]),
            }
            for e in events[:8]
        ],
        "now": _now(counts),
        "ticketWord": f"{len(tickets)} issues, left to right from received to shipped"
        if len(tickets) != 1
        else "one issue, left to right from received to shipped",
        "loop": loop,
        "needs": needs,
        "events": events,
        "groups": groups,
        "raw": raw_mode,
        "grouped": not raw_mode,
        "countLabel": f"last {min(8, len(events))} of {len(events)} events",
        "setGrouped": url("/", tl="grouped"),
        "setRaw": url("/", tl="raw"),
        "gBg": off[0] if raw_mode else on[0],
        "gFg": off[1] if raw_mode else on[1],
        "rBg": on[0] if raw_mode else off[0],
        "rFg": on[1] if raw_mode else off[1],
    }


def _age(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        t = datetime.fromisoformat(iso)
    except ValueError:
        return ""
    t = t if t.tzinfo else t.replace(tzinfo=UTC)
    secs = int((datetime.now(UTC) - t).total_seconds())
    if secs < 3600:
        return f"{max(secs // 60, 0)}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def _sparklines(store: Store) -> list[dict[str, Any]]:
    """Three small series over the run's own span in 24 equal bins: sessions started, ACU,
    gate passes (fails in the title). The span is the run's own, so replay draws what live drew."""
    from datetime import timedelta

    from swe_loop import charts

    starts: list[tuple[datetime, float]] = []
    metered = any(
        (r["acus_consumed"] or 0) > 0 for r in store._all("SELECT acus_consumed FROM sessions")
    )
    for r in store._all("SELECT * FROM sessions"):
        try:
            val = (
                float(r["acus_consumed"] or 0)
                if metered
                else cost.repair_active_seconds(store, r) / 60.0
            )
            starts.append((datetime.fromisoformat(r["created_at"]), val))
        except (TypeError, ValueError):
            pass
    for r in store.list_triage_sessions():
        try:
            val = (
                float(r["acus_consumed"] or 0)
                if metered
                else cost.triage_active_seconds(store, r) / 60.0
            )
            starts.append((datetime.fromisoformat(r["created_at"]), val))
        except (TypeError, ValueError):
            pass
    verdicts: list[tuple[datetime, str]] = []
    for v in store._all("SELECT created_at, gate_result FROM verdicts"):
        try:
            verdicts.append((datetime.fromisoformat(v["created_at"]), v["gate_result"]))
        except (TypeError, ValueError):
            pass
    times = [t for t, _ in starts] + [t for t, _ in verdicts]
    if not times:
        empty = charts.sparkline([], PURPLE)
        return [
            {"label": k, "svg": empty, "span": "no sessions yet", "last": "0"}
            for k in ("sessions started", "ACU", "gate passes")
        ]
    lo, hi = min(times), max(times)
    if (hi - lo).total_seconds() < 3600:
        hi = lo + timedelta(hours=1)
    bins = 24
    width = (hi - lo).total_seconds() / bins

    def bucket(t: datetime) -> int:
        return min(bins - 1, int((t - lo).total_seconds() // width))

    s_bins, a_bins, g_bins = [0.0] * bins, [0.0] * bins, [0.0] * bins
    fails = 0
    for t, acu in starts:
        s_bins[bucket(t)] += 1
        a_bins[bucket(t)] += acu
    for t, g in verdicts:
        if g == "pass":
            g_bins[bucket(t)] += 1
        else:
            fails += 1
    span = f"{lo.strftime('%H:%M')} to {hi.strftime('%H:%M')}"
    return [
        {
            "label": "sessions started",
            "svg": charts.sparkline(s_bins, PURPLE, title="sessions started per bin"),
            "span": span,
            "last": str(int(sum(s_bins))),
        },
        {
            "label": "ACU" if metered else "active minutes",
            "svg": charts.sparkline(
                a_bins, INK, title="ACU per bin" if metered else "active minutes per bin"
            ),
            "span": span,
            "last": f"{sum(a_bins):.1f}",
        },
        {
            "label": "gate passes",
            "svg": charts.sparkline(g_bins, TEAL, title=f"gate passes per bin; {fails} fail(s)"),
            "span": span,
            "last": f"{int(sum(g_bins))} pass · {fails} fail",
        },
    ]


def _group_events(
    events: list[dict[str, Any]], open_groups: set[str], raw_mode: bool
) -> list[dict[str, Any]]:
    """Consecutive gate events of one session fold into one row with receipt chips."""
    groups: list[dict[str, Any]] = []
    i = 0
    while i < len(events):
        e = events[i]
        run = [e]
        if e["layer"].startswith("L5"):
            j = i + 1
            while (
                j < len(events)
                and events[j]["layer"].startswith("L5")
                and events[j].get("session_id") == e.get("session_id")
            ):
                run.append(events[j])
                j += 1
        head = next((x for x in run if "->" in x["event"]), run[0])
        checks = []
        for x in run:
            evn = x["event"]
            if evn.startswith(("T0", "T1", "T2")):
                bad = "FAIL" in evn or "fail" in evn
                checks.append(
                    {
                        "label": evn.split(" exit")[0][:8],
                        **(
                            {"bg": PL["bad"][1], "fg": PL["bad"][0]}
                            if bad
                            else {"bg": PL["ok"][1], "fg": PL["ok"][0]}
                        ),
                    }
                )
        gid = str(len(groups))
        is_open = gid in open_groups
        note = head["detail"] if len(run) > 1 else ""
        others = open_groups - {gid} if is_open else open_groups | {gid}
        groups.append(
            {
                **head,
                "ref": head["ref"],
                "note": note,
                "hasNote": bool(note),
                "checks": checks,
                "nLabel": (("hide " if is_open else "") + f"{len(run)} events")
                if len(run) > 1
                else "",
                "cursor": "pointer" if len(run) > 1 else "default",
                "open": is_open,
                "raw": run,
                "toggle": url("/", tl="raw" if raw_mode else None, open=",".join(sorted(others)))
                if len(run) > 1
                else "/",
            }
        )
        i += len(run)
    return groups


# ---------------------------------------------------------------------------- tickets
FILTERS = [
    ("all", "all"),
    ("devin", "to Devin"),
    ("human", "to a person"),
    ("active", "active"),
    ("merged", "merged"),
]
EMPTY = {
    "active": "No active tickets. Active means dispatched, running, or at the gate.",
    "merged": "No merged tickets yet.",
    "devin": "No tickets routed to Devin.",
    "human": "No tickets routed to a person.",
}


def _passes(row: dict[str, Any], f: str) -> bool:
    route = row["route"]
    status = row["status"]
    return (
        f == "all"
        or (f == "devin" and route == "devin")
        or (f == "human" and route and route != "devin")
        or (f == "merged" and status == "merged")
        or (f == "active" and status in ("dispatched", "running", "gated"))
    )


def tickets(store: Store, cfg: TargetConfig, q: dict[str, str]) -> dict[str, Any]:
    tk_page = pages.tickets(store)
    sm = tk_page["summary"]
    rows = [r for g in tk_page["groups"] for r in g["rows"]]
    f = q.get("f", "all")
    sel = q.get("sel") or (rows[0]["id"] if rows else None)
    shown = [r for r in rows if _passes(r, f)]

    def col(n: Any, good: str) -> str:
        return good if n else FAINT

    summary = {
        **sm,
        "devinColor": col(sm["devin"], PL["devin"][0]),
        "humanColor": col(sm["human"], PL["person"][0]),
        "activeColor": col(sm["active"], PL["run"][0]),
        "mergedColor": col(sm["merged"], PL["ok"][0]),
        "pendingColor": FAINT,
    }
    chips = [
        {
            "label": label,
            "set": url("/tickets-page", f=key if key != "all" else None, sel=sel),
            "border": "#2457a8" if f == key else "#d6d2c9",
            "bg": "#2457a8" if f == key else "#fff",
            "fg": "#fff" if f == key else INK,
        }
        for key, label in FILTERS
    ]
    out_rows = []
    for r in shown:
        route = r["route"]
        st_kind = _pill_kind(r["pill"])
        out_rows.append(
            {
                **dot(r["id"], r["status"] == "merged"),
                "id": r["id"],
                "issue": f"#{r['issue']}" if r.get("issue") else "",
                "issueUrl": r.get("issue_url") or "#",
                "title": r["title"],
                "count": (
                    f"{len(r['files'])} file{'s' if len(r['files']) != 1 else ''} · {r['sites']} site{'s' if r['sites'] != 1 else ''}"
                    if r.get("files")
                    else f"{r['sites']} site(s)"
                ),
                "classes": ", ".join(r["classes"][:4])
                + (f" +{len(r['classes']) - 4}" if len(r["classes"]) > 4 else ""),
                "sessions": f"{r['sessions']} session{'s' if r['sessions'] != 1 else ''}",
                "why": r.get("reason") or "",
                "route": "devin"
                if route == "devin"
                else ("a person" if route else "awaiting triage"),
                "routeBg": PL["devin"][1]
                if route == "devin"
                else (PL["person"][1] if route else PL["na"][1]),
                "routeFg": PL["devin"][0]
                if route == "devin"
                else (PL["person"][0] if route else PL["na"][0]),
                "status": r["status"],
                "stBg": PL[st_kind][1],
                "stFg": PL[st_kind][0],
                "bg": "#eef2f9" if r["id"] == sel else "#fff",
                "select": url("/tickets-page", f=f if f != "all" else None, sel=r["id"]),
                "track": url("/tracker", open=r["id"]),
            }
        )
    tk = _ticket_panel(store, sel, f, q) if sel else _empty_panel()
    return {
        "sm": summary,
        "chips": chips,
        "ticketCount": f"{len(shown)}" + ("" if len(shown) == len(rows) else f" of {len(rows)}"),
        "tickets": out_rows,
        "noTickets": not shown,
        "emptyText": EMPTY.get(f, "No tickets."),
        "tk": tk,
    }


def _empty_panel() -> dict[str, Any]:
    return {
        "id": "",
        "issue": "",
        "issueUrl": "#",
        "title": "No ticket selected",
        "color": FAINT,
        "why": "",
        "classes": "",
        "files": "",
        "pills": [],
        "hasSites": False,
        "noSites": True,
        "siteNote": "Select a ticket.",
        "sites": [],
        "hasAcceptance": False,
        "acceptance": [],
        "hasTimeline": False,
        "tlOpen": False,
        "tlLabel": "",
        "toggleTl": "/tickets-page",
        "timeline": [],
        "track": "/tracker",
    }


def _ticket_panel(store: Store, tid: str, f: str, q: dict[str, str]) -> dict[str, Any]:
    d = pages.ticket_detail(store, tid)
    if not d:
        return _empty_panel()
    route = d["route"]
    st_kind = _pill_kind(d["pill"])
    pills = [
        {"label": "routed to Devin", **pill("devin")}
        if route == "devin"
        else (
            {"label": "routed to a person", **pill("person")}
            if route
            else {"label": "awaiting triage", **pill("na")}
        ),
        {"label": d["status"], **pill(st_kind)},
        {"label": f"source: {d.get('source', '')}", **pill("na")},
    ]
    if d.get("review") == "required":
        pills.append({"label": "review required", **pill("gate")})
    sites = []
    for s in d.get("sites") or []:
        lines = s.get("lines") or ([s.get("line")] if s.get("line") else [])
        loc = (
            f"{(s.get('file') or '').replace('superset/', '', 1)}:{','.join(str(x) for x in lines)}"
        )
        classes = s.get("classes") or ([s.get("class")] if s.get("class") else [])
        sites.append(
            {
                "loc": loc,
                "cls": ", ".join(c for c in classes if c),
                "msg": (s.get("r3") or s.get("r2") or s.get("prescribed_fix") or "")[:220],
                "warned": bool(s.get("warned")) or bool(s.get("r2")),
                "broke": bool(s.get("broke")) or bool(s.get("r3")),
                "kind": s.get("kind") or "",
            }
        )
    tl_open = q.get("tl") == "1"
    timeline = [ev(e) for e in d.get("timeline") or []]
    return {
        "id": d["id"],
        "issue": f"#{d['issue']}" if d.get("issue") else "",
        "issueUrl": d.get("issue_url") or "#",
        "title": d["title"],
        "color": tk_color(d["id"]),
        "why": d.get("reason") or "",
        "classes": ", ".join(d.get("classes") or []),
        "files": ", ".join(d.get("files") or [])
        or ("under tests/ only; a session never edits them" if route and route != "devin" else ""),
        "pills": pills,
        "hasSites": bool(sites),
        "noSites": not sites,
        "siteNote": "No sites recorded yet: the triage session has not delivered a verdict for this ticket."
        if not d.get("triage_verdict_json") and not sites
        else "",
        "sites": sites,
        "hasAcceptance": bool(d.get("acceptance")),
        "acceptance": [{"name": k, "cmd": v} for k, v in (d.get("acceptance") or {}).items()],
        "hasTimeline": bool(timeline),
        "tlOpen": tl_open,
        "tlLabel": ("hide timeline" if tl_open else "timeline") + f" ({len(timeline)})",
        "toggleTl": url(
            "/tickets-page", f=f if f != "all" else None, sel=d["id"], tl=None if tl_open else "1"
        ),
        "timeline": timeline,
        "track": url("/tracker", open=d["id"]),
    }


# ---------------------------------------------------------------------------- tracker
def tracker(store: Store, cfg: TargetConfig, q: dict[str, str]) -> dict[str, Any]:
    tr = pages.tracker(store)
    open_ids = {x for x in (q.get("open") or "").split(",") if x}
    store.budget_state().get("per_session_cap") or cfg.max_acu_limit
    rows = []
    for r in tr["rows"]:
        t = store.get_ticket(r["id"]) or {}
        pat = _pattern(store, t) if t else "--------"
        sd = (r.get("sessions_detail") or [None])[-1]
        verdict = sd["verdict"] if sd and sd.get("verdict") else None
        claim = sd["claim"] if sd and isinstance(sd.get("claim"), dict) else {}
        passed_t1 = [e for e in (sd["evidence"] if sd else []) if e["tier"] == "T1"]
        labels = [
            "wo",
            "verdict" if t.get("triage_verdict_json") else ("waiting" if pat[1] == "b" else ""),
            ("devin" if r["route"] == "devin" else ("person" if r["route"] else "")),
            ("refused" if pat[3] == "b" else ("reserved" if sd else "")),
            (f"{_fmt_acu(sd['acus'])} ACU" if sd else ""),
            (
                f"{sum(1 for e in passed_t1 if e['passed'])}/{len(passed_t1)}"
                + (f" r{sd['retries']}" if sd and sd["retries"] else "")
                if passed_t1
                else ""
            ),
            (
                _review_short(verdict["review_severity"])
                if verdict and verdict.get("review_severity")
                else ""
            ),
            ("person" if r["merged"] else ("waiting" if r["ready"] else "")),
        ]
        cells = []
        for i, (name, actor) in enumerate(STG):
            st = pat[i]
            col = ACT[actor][0]
            cells.append(
                {
                    "title": f"{name} · {ACT[actor][2]} · {STATE_NAME[st]}"
                    + (f" · {labels[i]}" if labels[i] else ""),
                    "h": "22px",
                    "label": labels[i],
                    "bg": col
                    if st == "d"
                    else (PL["bad"][0] if st == "b" else ("#fff" if st == "n" else "#e9e7e1")),
                    "fg": col if st == "n" else "#fff",
                    "shadow": f"inset 0 0 0 2px {col}" if st == "n" else "none",
                }
            )
        is_open = r["id"] in open_ids
        others = (open_ids - {r["id"]}) if is_open else (open_ids | {r["id"]})
        st_kind = _pill_kind(r["pill"])
        evidence = [
            {
                "tier": e["tier"],
                "cmd": (e["command"] or "").split(": ", 1)[0]
                if e["tier"] == "T1"
                else e["command"],
                "exit": str(e["exit_code"]),
                **pill("ok" if e["passed"] else "bad"),
            }
            for e in (sd["evidence"] if sd else [])
        ]
        state_kind = _status_kind(sd["status"], sd["status_detail"], True) if sd else "na"
        rows.append(
            {
                **dot(r["id"], r["merged"]),
                "id": r["id"],
                "issue": f"#{r['issue']}" if r.get("issue") else "",
                "issueUrl": r.get("issue_url") or "#",
                "idColor": tk_color(r["id"]),
                "bg": "#faf9f6" if is_open else "#fff",
                "pad": "10px",
                "cells": cells,
                "classes": ", ".join(r.get("classes") or []),
                "status": r["status"],
                "stBg": PL[st_kind][1],
                "stFg": PL[st_kind][0],
                "note": r.get("last_event") or "",
                "open": is_open,
                "chev": "▲" if is_open else "▼",
                "toggle": url("/tracker", open=",".join(sorted(others))),
                "hasShard": bool(sd),
                "files": ", ".join(sd["files"]) if sd else "",
                "session": (sd["devin_id"] or sd["id"])[:12] if sd else "",
                "sessionUrl": (sd["url"] or "#") if sd else "#",
                "pr": (sd["pr_url"] or "").rsplit("/", 1)[-1]
                if sd and sd.get("pr_url")
                else "none",
                "prUrl": (sd["pr_url"] or "#") if sd else "#",
                "acuLine": f"{_fmt_acu(sd['acus'])} ACU · {(sd['size'] or '?').upper()} · retries {sd['retries']} · {sd['elapsed']}"
                if sd
                else "",
                "state": f"{sd['status']}/{sd['status_detail']}" if sd else "",
                "stateBg": PL[state_kind][1],
                "stateFg": PL[state_kind][0],
                "timeline": [ev(e) for e in (sd["timeline"] if sd else [])][-40:],
                "evidence": evidence,
                "said": (
                    f"{'done' if claim.get('self_reported_done') else 'not done'} · tests {claim.get('tests_passed', 0)}/{claim.get('tests_run', 0)}"
                    + (
                        f" · {len(claim.get('needs_human') or [])} note(s) for a person"
                        if claim.get("needs_human")
                        else ""
                    )
                )
                if claim
                else "no claim yet",
                "gate": verdict["gate_result"] if verdict else "pending",
                "decision": verdict["decision"] if verdict else "",
                "review": verdict.get("review_severity") or "not requested" if verdict else "",
                "gateNote": (verdict.get("reason") or "") if verdict else "",
                "isMerged": bool(r["merged"]),
                "isReady": bool(r["ready"]) and not r["merged"],
                "mergeUrl": f"/tickets/{r['id']}/merge-form",
                "readyNote": f"{r['readiness']['verified']}/{r['readiness']['shards']} shards verified · reviewed · {'no conflicts' if not r['readiness']['conflicts'] else str(len(r['readiness']['conflicts'])) + ' conflict(s)'}. Merge on GitHub first; this records it.",
                "isEscalated": r["status"] == "escalated"
                or bool(r["route"] and r["route"] != "devin"),
                "routeReason": r.get("reason") or "",
                "escalations": [
                    {
                        "kind": e["kind"],
                        **pill("bad" if not e.get("resolved_at") else "na"),
                        "reason": e["reason"],
                        "time": _hhmmss(e["created_at"]),
                    }
                    for e in r.get("escalations") or []
                ],
                "placeholder": False,
            }
        )
    return {
        "fifty": False,
        "stageHead": [{"name": n, "actor": ACT[a][2], "color": ACT[a][0]} for n, a in STG],
        "trackerCount": str(len(rows)),
        "trackerRows": rows,
    }


def _review_short(sev: str) -> str:
    if sev.startswith("completed:"):
        rest = sev.split(":", 1)[1]
        if "no issues" in rest:
            return "no issues"
        n = rest.split(" ")[0]
        return f"{n} found" if n.isdigit() else "done"
    return "requested"


# ---------------------------------------------------------------------------- sessions
def sessions(store: Store, cfg: TargetConfig, q: dict[str, str]) -> dict[str, Any]:
    ss = pages.sessions(store, cfg)
    b = store.budget_state()
    cap = b.get("per_session_cap") or cfg.max_acu_limit
    drawer_id = q.get("drawer")
    metered = cost.spend(store)["metered"]
    mins: dict[str, float] = {}
    usd_rows: dict[str, tuple[float | None, str]] = {}
    if not metered:
        rate = cost.observed_rate(store)
        for r in store._all("SELECT * FROM sessions"):
            mins[r["id"]] = cost.repair_active_seconds(store, r) / 60.0
            usd_rows[r["id"]] = cost.session_usd(store, r, "rep", rate)
        for r in store.list_triage_sessions():
            mins[r["id"]] = cost.triage_active_seconds(store, r) / 60.0
            usd_rows[r["id"]] = cost.session_usd(store, r, "tri", rate)
    rows = []
    for s in ss["sessions"]:
        is_triage = s.get("kind") == "triage"
        st_kind = _pill_kind(s["pill"])
        size = (s.get("size") or "").upper()
        gate = s.get("gate")
        rows.append(
            {
                **dot(s["ticket"], False),
                "id": (s.get("devin_id") or s["id"])[:12],
                "url": s.get("url") or "#",
                "tk": s["ticket"],
                "shard": "triage" if is_triage else s["shard"],
                "track": url("/tracker", open=s["ticket"]),
                "source": s.get("source") or "",
                "status": _short_status(
                    s["status"],
                    s.get("status_detail"),
                    bool(s.get("pr_url")) or bool(s.get("outcome")),
                ),
                "stBg": PL[st_kind][1],
                "stFg": PL[st_kind][0],
                "acu": (
                    _fmt_acu(s.get("acus"))
                    if metered
                    else (
                        f"${usd_rows[s['id']][0]:.2f}"
                        if usd_rows.get(s["id"], (None, ""))[0] is not None
                        else f"{mins.get(s['id'], 0.0):.1f} min"
                    )
                ),
                "cap": (
                    f"{TRIAGE_ACU_CAP if is_triage else cap:.0f}"
                    if metered
                    else f"{mins.get(s['id'], 0.0):.1f} min"
                    + (" est." if usd_rows.get(s["id"], (None, ""))[1] == "estimate" else "")
                ),
                "acuPct": _pct(s.get("acus"), TRIAGE_ACU_CAP if is_triage else cap)
                if metered
                else _pct(mins.get(s["id"], 0.0), max(list(mins.values()) or [1.0])),
                "size": size or "·",
                "sizeBg": PL["bad"][1]
                if size in ("L", "XL")
                else (PL["ok"][1] if size else PL["na"][1]),
                "sizeFg": PL["bad"][0]
                if size in ("L", "XL")
                else (PL["ok"][0] if size else PL["na"][0]),
                "parent": (
                    "child"
                    if s.get("parent")
                    else (f"{len(s['children'])} children" if s.get("children") else "single")
                ),
                "pr": (s.get("pr_url") or "").rsplit("/", 1)[-1]
                and f"#{(s.get('pr_url') or '').rsplit('/', 1)[-1]}"
                or ("verdict" if is_triage else "none yet"),
                "prUrl": s.get("pr_url") or url("/tracker", open=s["ticket"]),
                "gate": (s.get("outcome") or "scoping") if is_triage else (gate or "not run"),
                "gateBg": PL["gate"][1]
                if (gate == "pass" or s.get("outcome") == "triaged")
                else (
                    PL["bad"][1]
                    if gate or s.get("outcome") in ("invalid", "no_output", "too_large")
                    else PL["na"][1]
                ),
                "gateFg": PL["gate"][0]
                if (gate == "pass" or s.get("outcome") == "triaged")
                else (
                    PL["bad"][0]
                    if gate or s.get("outcome") in ("invalid", "no_output", "too_large")
                    else PL["na"][0]
                ),
                "started": s.get("created") or "",
                "elapsed": s.get("elapsed") or "",
                "eta": s.get("eta") or "",
                "bg": "#eef2f9" if s["id"] == drawer_id else "#fff",
                "open": url("/devin/sessions", drawer=s["id"])
                if not is_triage
                else url("/tracker", open=s["ticket"]),
            }
        )
    d = (
        _drawer(store, drawer_id, cap)
        if drawer_id
        else {"timeline": [], "evidence": [], "verdicts": []}
    )
    basis = " · ".join(
        f"{k if k != '*' else 'all'} {v}" for k, v in (ss.get("eta_basis") or {}).items()
    )
    return {
        "now": _now(ss["counts"]),
        "perSession": f"{cap:.0f}",
        "managed": "in use" if ss.get("managed") else "not exercised in this run",
        "sessionRows": rows,
        "etaFoot": "time left: estimate from finished sessions"
        + (f" ({basis})" if basis else "")
        + " · cap is the hard limit · L and XL flagged",
        "drawerOpen": bool(drawer_id) and bool(d.get("id")),
        "closeDrawer": "/devin/sessions",
        "d": d,
    }


def _drawer(store: Store, sid: str, cap: float) -> dict[str, Any]:
    det = ops.session_detail(store, sid)
    if not det:
        return {"timeline": [], "evidence": [], "verdicts": []}
    s, t = det["session"], det["ticket"] or {}
    out = det.get("structured_output") or {}
    verdicts = det.get("verdicts") or []
    last = verdicts[-1] if verdicts else None
    st_kind = _status_kind(s.get("status"), s.get("status_detail"), bool(s.get("terminal_at")))
    return {
        "id": (s.get("devin_session_id") or s["id"])[:12],
        "tk": t.get("id", ""),
        "L": (det["work_order"] or {}).get("shard_id", ""),
        "color": tk_color(t.get("id", "tkt_")),
        "status": f"{s.get('status')}/{s.get('status_detail')}"
        if s.get("status_detail")
        else (s.get("status") or ""),
        "stBg": PL[st_kind][1],
        "stFg": PL[st_kind][0],
        "acu": _fmt_acu(s.get("acus_consumed"))
        if (s.get("acus_consumed") or 0) > 0
        else f"{cost.repair_active_seconds(store, s) / 60.0:.1f} min",
        "cap": f"{cap:.0f}" if (s.get("acus_consumed") or 0) > 0 else f"cap {cap:.0f} ACU",
        "acuPct": _pct(s.get("acus_consumed"), cap) if (s.get("acus_consumed") or 0) > 0 else "0",
        "size": (s.get("session_size") or "·").upper(),
        "retries": str(s.get("retries") or 0),
        "pr": f"#{(s.get('pull_request_url') or '').rsplit('/', 1)[-1]}"
        if s.get("pull_request_url")
        else "none",
        "prUrl": s.get("pull_request_url") or "#",
        "said": (
            f"{'done' if out.get('self_reported_done') else 'not done'} · tests {out.get('tests_passed', 0)}/{out.get('tests_run', 0)}"
            + (
                f" · PR #{(out.get('pr_url') or '').rsplit('/', 1)[-1]}"
                if out.get("pr_url")
                else ""
            )
        )
        if out
        else "no structured output",
        "gate": last["gate_result"] if last else "pending",
        "decision": last["decision"] if last else "",
        "review": (last.get("review_severity") or "not requested") if last else "",
        "gateNote": (last.get("reason") or "") if last else "the gate has not run",
        "timeline": [ev(e) for e in det.get("timeline") or []][-60:],
        "evidence": [
            {
                "tier": e["tier"],
                "cmd": e["command"],
                "exit": str(e["exit_code"]),
                **pill("ok" if e["passed"] else "bad"),
                "log": (e.get("output_path") or "").rsplit("/", 1)[-1][:14] or "·",
            }
            for e in det.get("evidence") or []
        ],
        "verdicts": [
            {
                "time": _hhmmss(v["created_at"]),
                "gate": v["gate_result"],
                **pill("gate" if v["gate_result"] == "pass" else "bad"),
                "decision": v["decision"],
                "reason": v.get("reason") or "",
            }
            for v in verdicts
        ],
        "output": json.dumps(out, indent=1) if out else "none",
    }


# ---------------------------------------------------------------------------- automations
def automations(
    store: Store,
    cfg: TargetConfig,
    settings: Settings,
    client: Any,
    running: bool,
    q: dict[str, str],
    err: bool = False,
    name: str = "",
) -> dict[str, Any]:
    a = pages.automations(store, cfg, settings, client, running)
    sel = q.get("sel") or (a["rows"][0]["id"] if a["rows"] else None)
    autos = []
    for r in a["rows"]:
        is_next = r["availability"] == "next"
        kind = "na" if is_next else ("run" if r["running"] else ("ok" if r["enabled"] else "na"))
        autos.append(
            {
                "id": r["id"],
                "name": r["name"],
                "kind": r["kind"],
                "state": "next"
                if is_next
                else ("running" if r["running"] else ("enabled" if r["enabled"] else "disabled")),
                "stBg": PL[kind][1],
                "stFg": PL[kind][0],
                "trigger": r["trigger_label"],
                "triggerShort": r["trigger_detail"],
                "playbook": r["playbook"] or "none",
                "cap": f"{int(r['max_acu'])} ACU per session" if r.get("max_acu") else "no cap",
                "conc": str(r["concurrency"]),
                "lastRun": (
                    f"last run {r['last_run'][:16].replace('T', ' ')}"
                    if r.get("last_run")
                    else "never run"
                ),
                "enabled": bool(r["enabled"]) and not is_next,
                "isNext": is_next,
                "opacity": ".72" if is_next else "1",
                "bg": "#eef2f9" if r["id"] == sel else "#fff",
                "shadow": "inset 3px 0 0 #2457a8" if r["id"] == sel else "none",
                "select": url("/automations", sel=r["id"]),
                "toggleUrl": f"/automations/{r['id']}/toggle",
                "toggleLabel": "Disable" if r["enabled"] else "Enable",
                "runnable": bool(r["runnable"]),
                "canRun": bool(r["enabled"]) and not r["running"],
                "runUrl": f"/automations/{r['id']}/run",
                "runLabel": "Running…" if r["running"] else "Run now",
                "_row": r,
            }
        )
    a0 = next((x for x in autos if x["id"] == sel), autos[0] if autos else None)
    mono, sans = "'JetBrains Mono',monospace", "'Instrument Sans',system-ui,sans-serif"
    au: dict[str, Any] = {
        "name": "",
        "kind": "",
        "state": "",
        "stBg": PL["na"][1],
        "stFg": PL["na"][0],
        "enabled": False,
        "isNext": False,
        "desc": "",
        "rows": [],
        "toggleUrl": "",
        "toggleLabel": "",
        "runnable": False,
        "canRun": False,
        "runUrl": "",
        "runLabel": "",
        "removable": False,
        "removeUrl": "",
        "actionNote": "",
    }
    if a0:
        r = a0["_row"]
        native = r.get("native")
        last = r.get("last_result") or {}
        au = {
            **{
                k: a0[k]
                for k in (
                    "name",
                    "kind",
                    "state",
                    "stBg",
                    "stFg",
                    "enabled",
                    "isNext",
                    "toggleUrl",
                    "toggleLabel",
                    "runnable",
                    "canRun",
                    "runUrl",
                    "runLabel",
                )
            },
            "desc": (r.get("kind_note") or "") + (f" {r['notes']}." if r.get("notes") else ""),
            "rows": [
                {"k": "trigger", "v": r["trigger_label"], "font": mono, "size": "12px"},
                {"k": "match", "v": r["trigger_detail"] or "any", "font": sans, "size": "13px"},
                {"k": "target", "v": r["target"], "font": mono, "size": "12px"},
                {"k": "playbook", "v": r["playbook"] or "none", "font": mono, "size": "12px"},
                {
                    "k": "cap · concurrency",
                    "v": f"{a0['cap']} · {a0['conc']} at once",
                    "font": sans,
                    "size": "13px",
                },
                {
                    "k": "schedule",
                    "v": r.get("schedule") or "on the trigger",
                    "font": sans,
                    "size": "13px",
                },
                {
                    "k": "Devin Automation",
                    "v": (
                        f"native Automation {native.get('id') or native.get('automation_id')}"
                        if native
                        else a["native_note"]
                    ),
                    "font": sans,
                    "size": "13px",
                },
                {
                    "k": "last result",
                    "v": (
                        f"dispatched {last.get('dispatched', 0)} · finished {last.get('finished', 0)} · gated {last.get('gated', 0)} · escalated {last.get('escalated', 0)}"
                        if last and "dispatched" in last
                        else (a0["lastRun"])
                    ),
                    "font": sans,
                    "size": "13px",
                },
            ],
            "removable": r["kind"] == "custom",
            "removeUrl": f"/automations/{r['id']}/delete",
            "actionNote": "seeded config; remove is for user-added ones"
            if r["kind"] != "custom"
            else "user-added config",
        }
        if a0["isNext"]:
            au["actionNote"] = "next: nothing runs until the scan session exists"
    for x in autos:
        x.pop("_row", None)
    return {
        "autos": autos,
        "au": au,
        "autoErr": err,
        "autoName": name,
        "autoNameBorder": PL["bad"][0] if err else "#d6d2c9",
        "autoNameShadow": f"0 0 0 3px {PL['bad'][1]}" if err else "none",
        "autoFoot": f"{a['routed']} routed, waiting for the next pass"
        + ("" if a["live"] else " · replay: sessions faked, gate skipped"),
        "cap": a["cap"],
        "playbookNames": a["playbook_names"],
        "triggerChoices": a["trigger_choices"],
    }


# ---------------------------------------------------------------------------- playbooks
def _sections(body: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for raw in body.splitlines():
        line = raw.rstrip()
        if line.startswith("# "):
            continue
        if line.startswith("## "):
            cur = {"h": line[3:].strip(), "paras": [], "items": [], "ordered": False}
            out.append(cur)
            continue
        if cur is None or not line.strip():
            continue
        if line.lstrip()[:2] == "- ":
            cur["items"].append(line.lstrip()[2:].strip())
        elif line.lstrip()[:1].isdigit() and ". " in line.lstrip()[:4]:
            cur["items"].append(line.lstrip().split(". ", 1)[1].strip())
            cur["ordered"] = True
        else:
            if cur["paras"] and not cur["items"] and not cur["paras"][-1].endswith("."):
                cur["paras"][-1] += " " + line.strip()
            else:
                cur["paras"].append(line.strip())
    for s in out:
        s["ordered"] = bool(s["ordered"] and s["items"])
        s["unordered"] = bool(not s["ordered"] and s["items"])
    return out


def playbooks(
    store: Store,
    cfg: TargetConfig,
    client: Any,
    q: dict[str, str],
    err: bool = False,
    name: str = "",
) -> dict[str, Any]:
    p = pages.playbooks(store, cfg, client)
    sel = q.get("sel") or (p["rows"][0]["id"] if p["rows"] else None)
    rows = []
    for r in p["rows"]:
        is_next = r["availability"] == "next"
        actor = "next" if is_next else "devin"
        rows.append(
            {
                "id": r["id"],
                "title": r["title"],
                "chip": "next" if is_next else r["agent"],
                "chipBg": ACT[actor][1],
                "chipFg": ACT[actor][0],
                "meta": f"{r['name']} · {len(r['sections'])} sections · schema: {len(r['schema_fields'])} fields · cap {int(r['max_acu']) if r.get('max_acu') else '·'} ACU · {r['source']}"
                + (" · on the org" if r.get("org_id") else ""),
                "usedBy": r["used_by"],
                "usedGo": r["used_by_link"],
                "opacity": ".72" if is_next else "1",
                "bg": "#eef2f9" if r["id"] == sel else "#fff",
                "shadow": "inset 3px 0 0 #2457a8" if r["id"] == sel else "none",
                "select": url("/devin/playbooks", sel=r["id"]),
            }
        )
    d = pages.playbook_detail(store, sel) if sel else None
    pb: dict[str, Any] = {
        "slug": "",
        "title": "No playbook selected",
        "chip": "",
        "chipBg": PL["na"][1],
        "chipFg": PL["na"][0],
        "meta": "",
        "hasBody": False,
        "isNext": False,
        "nextNote": "",
        "sections": [],
        "fields": "",
        "schemaLabel": "",
        "schemaOpen": False,
        "toggleSchema": "/devin/playbooks",
        "schemaJson": "",
        "outLabel": "",
        "outOpen": False,
        "toggleOut": "/devin/playbooks",
        "outJson": "",
        "outNote": "",
    }
    if d:
        is_next = d["availability"] == "next"
        actor = "next" if is_next else "devin"
        schema_open, out_open = q.get("schema") == "1", q.get("out") == "1"
        fields = d.get("schema_fields") or []
        pb = {
            "slug": d["name"],
            "title": d.get("title") or d["name"],
            "chip": "next" if is_next else d["agent"],
            "chipBg": ACT[actor][1],
            "chipFg": ACT[actor][0],
            "meta": f"cap {int(d['max_acu']) if d.get('max_acu') else '·'} ACU · source {d['source']} · updated {d['updated_at'][:16].replace('T', ' ')}",
            "hasBody": not is_next,
            "isNext": is_next,
            "nextNote": (
                d["body"].split("## Overview", 1)[-1].split("##", 1)[0].strip()
                if "## Overview" in d["body"]
                else d["body"][:400]
            )
            + " It runs when the Scan automation is enabled; nothing runs before that.",
            "sections": _sections(d["body"]),
            "fields": " · ".join(fields),
            "schemaLabel": ("hide the schema" if schema_open else "structured output schema")
            + (f" · {len(fields)} fields" if fields else ""),
            "schemaOpen": schema_open,
            "toggleSchema": url(
                "/devin/playbooks",
                sel=sel,
                schema=None if schema_open else "1",
                out="1" if out_open else None,
            ),
            "schemaJson": json.dumps(d.get("schema"), indent=1) if d.get("schema") else "no schema",
            "outLabel": "hide the last output"
            if out_open
            else "last output received against this schema",
            "outOpen": out_open,
            "toggleOut": url(
                "/devin/playbooks",
                sel=sel,
                out=None if out_open else "1",
                schema="1" if schema_open else None,
            ),
            "outJson": json.dumps(d.get("last_output"), indent=1)
            if d.get("last_output")
            else "none yet",
            "outNote": "the last output received against this schema"
            if d.get("last_output")
            else "no session has returned output against this schema yet",
        }
    return {
        "pbRows": rows,
        "pb": pb,
        "pbErr": err,
        "pbName": name,
        "pbNameBorder": PL["bad"][0] if err else "#d6d2c9",
        "pbNameShadow": f"0 0 0 3px {PL['bad'][1]}" if err else "none",
        "cap": cfg.max_acu_limit,
    }


# ---------------------------------------------------------------------------- report
def report(
    store: Store, cfg: TargetConfig, inventory_dir: Any, q: dict[str, str]
) -> dict[str, Any]:
    rep = report_mod.build(store, inventory_dir)
    h = rep["headline"]
    sql_q = q.get("sql") or ""
    all_keys = ["verified", "acu", "claims", "budget"]
    open_keys = (
        set(all_keys) if sql_q in ("1", "all") else (set(sql_q.split(",")) if sql_q else set())
    )
    all_open = all(k in open_keys for k in all_keys)
    acu = h["acu"]
    sp = cost.spend(store)
    mins_by_sid = {
        r["id"]: cost.repair_active_seconds(store, r) / 60.0
        for r in store._all("SELECT * FROM sessions")
    }
    sid_by_devin = {
        r["devin_session_id"]: r["id"]
        for r in store._all("SELECT id, devin_session_id FROM sessions")
    }
    rate0 = cost.observed_rate(store)
    usd_by_sid = {
        r["id"]: cost.session_usd(store, r, "rep", rate0)
        for r in store._all("SELECT * FROM sessions")
    }
    pass_usd = sorted(
        usd_by_sid[x["id"]][0]
        for x in store._all("SELECT * FROM sessions")
        if (store.latest_verdict(x["id"]) or {}).get("gate_result") == "pass"
        and usd_by_sid[x["id"]][0] is not None
    )
    pass_mins = sorted(
        mins_by_sid[s["id"]]
        for s in store._all("SELECT * FROM sessions")
        if (store.latest_verdict(s["id"]) or {}).get("gate_result") == "pass"
    )

    def _receipt_cost(r: dict[str, Any]) -> str:
        if sp["metered"]:
            return _fmt_acu(r.get("acus"))
        sid = sid_by_devin.get(r.get("devin_id"))
        u, src = usd_by_sid.get(sid, (None, ""))
        if u is not None:
            return f"${u:.2f}" + (" est." if src == "estimate" else "")
        return f"{mins_by_sid.get(sid, 0.0):.1f}m"

    tiles = [
        (
            "verified",
            "Verified changes",
            str(h["verified"]["n"]),
            f"of {h['verified']['of']}",
            "gate passed and a person merged · denominator: tickets the router decided",
            h["verified"]["sql"],
        ),
        (
            (
                "acu",
                "ACU per verified change",
                str(acu["median"]) if acu["median"] is not None else "n/a",
                "median",
                f"p95 {acu['p95'] if acu['p95'] is not None else 'n/a'} · n={acu['n']}",
                acu["sql"],
            )
            if sp["metered"]
            else (
                (
                    "acu",
                    "Cost per verified change",
                    f"${_median(pass_usd):.2f}",
                    "median, console figures",
                    f"p95 ${_p95(pass_usd):.2f} · n={len(pass_usd)} · {_median(pass_mins):.1f} min of AI work at the median"
                    + (
                        ""
                        if sp["source"] == "console"
                        else " · some sessions estimated at the observed rate"
                    ),
                    "dollars per session as shown in the Devin console, entered by a person on Settings; sessions without a figure are priced at the observed dollars per active minute",
                )
                if pass_usd
                else (
                    "acu",
                    "AI working time per verified change",
                    (f"{_median(pass_mins):.1f}" if pass_mins else "n/a"),
                    "min, median",
                    f"p95 {_p95(pass_mins):.1f} min · n={len(pass_mins)} · this plan is billed in credits; enter the console's figures in Settings to see dollars",
                    "active minutes: gaps between our polls while the session reported working, each gap capped at 60 s; see swe_loop/cost.py",
                )
            )
        ),
        (
            "claims",
            "Self-reported vs verified",
            f"{h['claims']['said_done']} · {h['claims']['passed_gate']}",
            "said done · passed the gate",
            f"gap {h['claims']['gap']} · the session's claim is recorded; the gate's result counts",
            h["claims"]["sql"],
        ),
        (
            "budget",
            "Budget",
            _fmt_acu(h["budget"]["spent"]),
            f"/ {h['budget']['cap'] if h['budget'].get('cap') else 'no cap'} ACU",
            f"per-session cap {h['budget'].get('per_session_cap') or 'n/a'}",
            h["budget"]["sql"],
        ),
    ]
    answer = []
    for key, k, v, unit, dsc, sql in tiles:
        is_open = key in open_keys
        others = (open_keys - {key}) if is_open else (open_keys | {key})
        answer.append(
            {
                "k": k,
                "v": v,
                "unit": unit,
                "d": dsc,
                "open": is_open,
                "sqlLabel": "hide SQL" if is_open else "SQL",
                "toggle": url("/report", sql=",".join(sorted(others))),
                "sql": sql,
            }
        )
    board = []
    for t in rep["board"]:
        issue = t["external_ref"].rsplit("#", 1)[-1] if t.get("external_ref") else ""
        board.append(
            {
                **dot(t["id"], t["status"] == "merged"),
                "issue": f"#{issue}" if issue else "",
                "classes": (t.get("class") or "").replace(",", ", "),
                "route": t.get("router_decision") or "",
                "status": t["status"],
                "stColor": PL["ok"][0]
                if t["status"] == "merged"
                else (PL["bad"][0] if t["status"] in ("escalated", "refused") else INK),
            }
        )
    funnel = [
        {
            "label": ("↳ " if kind == "drop" else "") + name,
            "n": str(n),
            "pad": "30px" if kind == "drop" else "16px",
            "color": PL["bad"][0] if kind == "drop" else INK,
        }
        for name, n, kind in rep["funnel"]
    ]
    bd = rep["burndown"]
    prod = bd.get("product") or 1
    bd_vm = {
        **bd,
        "fixedPct": f"{100 * bd.get('fixed', 0) / prod:.1f}",
        "remainingPct": f"{100 * bd.get('remaining', 0) / prod:.1f}",
    }
    evidence_rows = []
    for r in rep["receipts"]:
        t0 = r.get("t0")
        evidence_rows.append(
            {
                "tk": r["ticket"],
                "L": r["shard"],
                "color": tk_color(r["ticket"]),
                "session": (r.get("devin_id") or "")[:12],
                "sessionUrl": r.get("session_url") or "#",
                "pr": f"#{r['pr_url'].rsplit('/', 1)[-1]}" if r.get("pr_url") else "none",
                "prUrl": r.get("pr_url") or "#",
                "t0": "not run" if t0 is None else ("clean" if t0 else "touched"),
                "t0Bg": PL["na"][1] if t0 is None else (PL["ok"][1] if t0 else PL["bad"][1]),
                "t0Fg": PL["na"][0] if t0 is None else (PL["ok"][0] if t0 else PL["bad"][0]),
                "t1": r.get("t1") or "not run",
                "gate": r.get("gate") or "pending",
                "gateBg": PL["ok"][1]
                if r.get("gate") == "pass"
                else (PL["bad"][1] if r.get("gate") else PL["na"][1]),
                "gateFg": PL["ok"][0]
                if r.get("gate") == "pass"
                else (PL["bad"][0] if r.get("gate") else PL["na"][0]),
                "review": r.get("review") or "",
                "retries": str(r.get("retries") or 0),
                "acu": _receipt_cost(r),
                "size": (r.get("size") or "·").upper(),
                "mergedBy": r.get("merged_by") or "no",
            }
        )
    size_line = "session_size " + " · ".join(
        f"{k} {n}" + (" unhealthy" if bad and n else "") for k, n, bad in rep["size_hist"]
    )
    tripwires = [
        {
            "name": w["name"],
            "value": w["value"],
            "threshold": w["threshold"],
            "status": w["status"],
            **pill("ok" if w["status"] == "PASS" else ("bad" if w["status"] == "FAIL" else "na")),
        }
        for w in rep["tripwires"]
    ]
    routing = [
        {
            "cls": c["class"],
            "att": str(c["attempted"]),
            "ver": str(c["verified"]),
            "med": str(c["median"]) if c["median"] is not None else "·",
            "p95": str(c["p95"]) if c["p95"] is not None else "·",
            "verdict": c["verdict"],
            "color": FAINT if not c["attempted"] else INK,
        }
        for c in rep["routing"]
    ]
    escalations = [
        {
            "ticket": e["ticket_id"],
            "color": tk_color(e["ticket_id"]),
            "kind": e["kind"],
            "reason": e["reason"],
            "resolved": "yes" if e.get("resolved_at") else "not yet",
        }
        for e in rep["escalations"]
    ]
    from swe_loop import charts

    fun_rows: list[tuple[str, int, int | None]] = []
    for name, n, kind in rep["funnel"]:
        if kind == "drop":
            if fun_rows:
                fun_rows[-1] = (fun_rows[-1][0], fun_rows[-1][1], n)
            continue
        fun_rows.append((name, n, None))
    cap = h["budget"].get("per_session_cap")

    def _pv(r: dict[str, Any]) -> float:
        if sp["metered"]:
            return float(r.get("acus") or 0)
        sid = sid_by_devin.get(r.get("devin_id"))
        if pass_usd:
            return usd_by_sid.get(sid, (None, ""))[0] or 0.0
        return mins_by_sid.get(sid, 0.0)

    points = [(r["shard"], _pv(r), tk_color(r["ticket"])) for r in rep["receipts"]]

    verd = store._all(
        "SELECT v.gate_result, v.decision, w.shard_id FROM verdicts v JOIN sessions s ON s.id = v.session_id "
        "JOIN work_orders w ON w.id = s.work_order_id ORDER BY v.created_at, v.rowid"
    )
    sq = [
        (
            v["shard_id"],
            f"{v['gate_result']} -> {v['decision']}",
            PL["ok"][0]
            if v["gate_result"] == "pass"
            else (PL["run"][0] if v["decision"] == "retry" else PL["bad"][0]),
        )
        for v in verd
    ]
    bd0 = rep["burndown"]
    # sites, not sessions: the inventory's site count per shard, for shards that passed and wait
    sites_by_shard: dict[str, int] = {}
    try:
        inv = json.loads((Path(inventory_dir) / "tickets.json").read_text())
        sites_by_shard = {sh["id"]: len(sh.get("sites") or []) for sh in inv.get("shards", [])}
    except (OSError, ValueError, TypeError):
        pass
    verified_unmerged = sum(
        sites_by_shard.get(r["shard"], 0)
        for r in rep["receipts"]
        if r.get("gate") == "pass" and r.get("merged_by") in (None, "no")
    )
    stack = [
        ("fixed and merged", bd0.get("fixed", 0), PL["ok"][0]),
        ("verified, unmerged", verified_unmerged, PL["gate"][0]),
        ("to a person", bd0.get("human", 0), PL["person"][0]),
        (
            "remaining",
            max(
                0,
                bd0.get("total", 0) - bd0.get("fixed", 0) - bd0.get("human", 0) - verified_unmerged,
            ),
            "#c9c5bb",
        ),
    ]
    chart_svgs = {
        "funnel": charts.funnel(fun_rows),
        "acu": charts.dot_strip(
            points,
            cap if sp["metered"] else None,
            acu["median"]
            if sp["metered"]
            else (_median(pass_usd) if pass_usd else (_median(pass_mins) if pass_mins else None)),
            unit="ACU" if sp["metered"] else ("$" if pass_usd else "min"),
        ),
        "gate": charts.squares(sq),
        "burndown": charts.stacked_bar(stack),
        "size": charts.histogram([(k, n, bad) for k, n, bad in rep["size_hist"]]),
    }
    chart_notes = {
        "funnel": f"{rep['funnel'][0][1]} decided · drops in red",
        "acu": (
            f"n={len(points)} · cap {cap:g} ACU per session"
            if (cap and sp["metered"])
            else (
                f"n={len(points)} · dollars per session from the console"
                if pass_usd
                else f"n={len(points)} · active minutes per session; the plan reports no ACU"
            )
        ),
        "gate": f"{len(sq)} verdict(s), oldest first",
        "burndown": f"{bd0.get('total', 0)} sites measured",
        "size": "L and XL unhealthy",
    }
    return {
        "charts": chart_svgs,
        "chartNotes": chart_notes,
        "answer": answer,
        "toggleAllSql": url("/report", sql=None if all_open else ",".join(all_keys)),
        "sqlAllLabel": "hide the queries" if all_open else "show the queries",
        "boardRows": board,
        "funnel": funnel,
        "bd": bd_vm,
        "evidenceRows": evidence_rows,
        "sizeLine": size_line,
        "tripwires": tripwires,
        "routing": routing,
        "escalations": escalations,
    }
