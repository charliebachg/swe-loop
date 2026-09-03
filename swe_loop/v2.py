"""View models for the designed pages: the mock's field contract, filled from the real store.

The mock (`swe-loop v2`) was built as a clickable component with its own state; here every
interaction is a URL the server renders, so nothing lives only in the browser. Colours and labels
are computed the way the mock computed them, from the same constants."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from swe_loop import charts, cost, ops, pages, rates
from swe_loop import reduce as reduce_mod
from swe_loop import report as report_mod
from swe_loop.config import Settings, TargetConfig
from swe_loop.store import Store
from swe_loop.triage import TRIAGE_ACU_CAP

ROOT = Path(__file__).resolve().parents[1]

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
# What a reader sees: four steps, each folding the internal ones behind it.
STG4 = [
    ("Scoped", "devin", (0, 1, 2), "read, understood, and a plan written"),
    ("Fixed", "devin", (3, 4), "code changed and a pull request opened"),
    ("Verified", "gate", (5, 6), "the same tests re-run on a clean copy, then reviewed"),
    ("Merged", "person", (7,), "merged by a person, never automatically"),
]


def fold4(pat: str) -> str:
    """The eight internal states become the four a person reads. A blocked step wins, then all
    done, then anything in progress."""
    out = []
    for _name, _actor, idx, _note in STG4:
        chars = [pat[i] for i in idx if i < len(pat)]
        if "b" in chars:
            out.append("b")
        elif chars and all(c == "d" for c in chars):
            out.append("d")
        elif "n" in chars:
            out.append("n")
        else:
            out.append("-")
    return "".join(out)


PAGES = {
    "home": ("Home", "/"),
    "automations": ("Automations", "/automations"),
    "tickets": ("Tickets", "/tickets-page"),
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
JOURNEY = ["home", "automations", "tickets", "report"]

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
# The five steps a newcomer needs, and which of the eight internal stages each one covers.
STAGE5 = [
    ("issues received", "filed on the repository", (0,)),
    ("scoped by the AI", "read, understood, a plan written; who fixes it decided", (1, 2, 3)),
    ("fix written", "code changed, tests run by the AI, a pull request opened", (4,)),
    (
        "verified",
        "the same tests re-run on a clean copy, then a second reading by the AI reviewer",
        (5, 6),
    ),
    ("shipped by your team", "merged by a person, never automatic", (7,)),
]
STAGE5_ACTOR = ["code", "devin", "devin", "gate", "person"]
BRAND = "Backstop"
SIZE_HOURS = {"XS": 0.5, "S": 1.0, "M": 3.0, "L": 8.0, "XL": 20.0}
HUMAN_HELP = (
    "Engineer time for the same change, estimated from the triage verdict's size class per fix "
    "(XS half an hour, S one hour, M three hours, L eight, XL twenty). Cognition measures AI output in "
    "productive engineering hours, the time a human would need for the same result, and found raw model "
    "estimates undercount by about 2x (h = 2.28 m^0.923); METR's time-horizon scale likewise rates tasks by "
    "the time experts need. This figure is the raw size estimate, not adjusted."
)
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
    "intake": "received",
    "triage": "AI scoping",
    "route": "decision",
    "shard": "split",
    "dispatch": "AI started",
    "poll": "AI working",
    "steer": "AI steered",
    "gate": "checks",
    "review": "AI review",
    "merge": "shipping",
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


def ref(number: Any) -> str:
    """How a ticket is named on screen: a number a person can read out."""
    try:
        return f"#{int(number):05d}"
    except (TypeError, ValueError):
        return "#-----"


def tk_color(ticket_id: str) -> str:
    """A stable colour per ticket, from the id itself, so any source works."""
    return _TK_MORE[sum(map(ord, ticket_id)) % len(_TK_MORE)]


def dot(store: Store, ticket_id: str, done: bool) -> dict[str, Any]:
    c = tk_color(ticket_id)
    t = store.get_ticket(ticket_id) or {}
    return {
        "ref": ref(t.get("number")),
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
    if lay in ("triage", "review") or "review" in lay:
        return "devin"
    if "gate" in lay:
        return "gate"
    if "human" in lay or "merge" in lay or lay == "escalate":
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
    if re.fullmatch(r"acus=0(\.0+)?", detail):
        detail = ""
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
        "link": url("/tickets-page", open=e.get("ticket_id"))
        if e.get("ticket_id")
        else "/tickets-page",
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
    cost.rates(store)
    kr = cost.rates(store)
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
                    f"{cost.repair_active_seconds(store, r) / 60.0 * kr['rep']:.2f}"
                    if r.get("cost_usd") is None
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
                    f"{cost.triage_active_seconds(store, r) / 60.0 * kr['tri']:.2f}"
                    if r.get("cost_usd") is None
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
        detail = f" ({sp['n_console']} of {sp['n_sessions']} entered; the rest at ${sp['rates']['rep']:.2f} per repair minute and ${sp['rates']['tri']:.2f} per triage minute)"
    else:
        detail = f", or minutes of AI work at ${sp['rates']['rep']:.2f} per repair minute and ${sp['rates']['tri']:.2f} per triage minute; refresh the figures in Settings whenever you like"
    return (
        base
        + detail
        + f". Active minutes are measured from our own polls: {sp['active_min']:.1f} min"
    )


def usd_label(sp: dict[str, Any]) -> str:
    if sp["usd"] is None:
        return ""
    return f"${sp['usd']:.2f}"


# ---------------------------------------------------------------------------- frame
def _usd_or_min(store: Store, row: dict[str, Any], kind: str, rates: dict[str, float]) -> str:
    """A session's cost so far, in dollars when we can price it, else its active minutes."""
    usd, _src = cost.session_usd(store, row, kind, rates)
    if usd is not None:
        return f"${usd:.2f}"
    secs = (
        cost.triage_active_seconds(store, row)
        if kind == "tri"
        else cost.repair_active_seconds(store, row)
    )
    return f"{secs / 60.0:.0f} min"


def rerun_ctx(settings: Settings, cfg: TargetConfig, store: Store) -> dict[str, Any]:
    """What the Reset button will do, and what the last reset did."""
    from swe_loop import rerun

    last_raw = store.get_setting("rerun.last")
    last = json.loads(last_raw) if last_raw else None
    root = (ROOT / cfg.gate.get("repo_root", "../superset-fork")).resolve()
    have_clone = (root / ".git").exists()
    live = settings.live
    return {
        "shards": rerun.shards(),
        "default": "D",
        "baseline": cfg.rerun.get("baseline", ""),
        "base": cfg.base_branch,
        "repo": cfg.repo,
        "live": live,
        "haveClone": have_clone,
        "clone": str(root),
        "willPush": live and have_clone,
        "last": last,
        "lastLine": (
            (
                f"shard {last['shard']}: {last.get('error')}"
                if last.get("error")
                else f"shard {last['shard']} at {last.get('at', '')[:16].replace('T', ' ')}: repository {last.get('repo')}"
                + (", pushed" if last.get("pushed") else "")
                + (", old repair branch deleted" if last.get("branch_deleted") else "")
                + f" · {last.get('store_rows', 0)} store row(s) forgotten"
                + (f" · snapshot {last.get('snapshot')}" if last.get("snapshot") else "")
            )
            if last
            else "never reset"
        ),
    }


def _usd_cap(store: Store) -> float | None:
    """The spend cap a person set on Settings, in dollars; none until set."""
    try:
        v = float(store.get_setting("usd_cap") or 0)
    except ValueError:
        return None
    return v or None


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
    per_label = (
        f"{b['per_session_cap']:.0f}" if b.get("per_session_cap") else str(cfg.max_acu_limit)
    )
    usd_cap = _usd_cap(store)
    return {
        "nav": nav,
        "navDevin": nav_devin,
        "pageTitle": PAGES.get(active, (active.title(), ""))[0],
        "pageStep": step,
        "brand": BRAND,
        "modeLabel": "LIVE" if live else "RECORDED RUN",
        "modeHelp": "connected to the Devin organisation; sessions are real"
        if live
        else "showing a recorded run of the real system; no AI session is started from this page",
        "acuHelp": ACU_HELP if sp["metered"] else _cost_help(sp),
        "modeBg": PL["ok"][1] if live else PL["run"][1],
        "modeFg": PL["ok"][0] if live else PL["run"][0],
        "modeSmall": "",
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
                f"${usd_cap:.0f} cap"
                if usd_cap
                else (
                    f"{sp['active_min']:.0f} min of AI work"
                    if sp["usd"] is not None
                    else "dollars: enter in Settings"
                )
            )
        ),
        "acuPct": _pct(b.get("spent"), b.get("cap"))
        if sp["metered"]
        else (_pct(sp["usd"], usd_cap) if usd_cap and sp["usd"] is not None else "0"),
        "costUnit": "ACU" if sp["metered"] else "",
        "spentLabel": (
            f"ACU spent · cap {per_label} per session"
            if sp["metered"]
            else f"spent · Devin's limit {per_label} ACU per session"
        ),
        "costHead": "ACU of cap" if sp["metered"] else "cost · AI minutes",
        "perSession": f"{b['per_session_cap']:.0f}"
        if b.get("per_session_cap")
        else str(cfg.max_acu_limit),
        # legacy pages still read these
        "mode": settings.mode,
        "target": cfg.repo,
        "active": active,
        "goTracker": "/tickets-page?view=pipeline",
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
    sp_home = cost.spend(store)
    kind_rates_home = cost.rates(store)
    spent_short = (
        f"{sp_home['acu']:.1f} ACU used"
        if sp_home["metered"]
        else (usd_label(sp_home) or f"{sp_home['active_min']:.0f} min") + " so far"
    )
    passed = [s for s in sess if (store.latest_verdict(s["id"]) or {}).get("gate_result") == "pass"]
    [s for s in sess if s["retries"]]
    reviewed = [s for s in sess if (store.latest_verdict(s["id"]) or {}).get("review_severity")]
    merged = [t for t in tickets if t["status"] == "merged"]
    ready = h["summary"]["ready"]
    [
        (len(tickets), f"{len(tickets)} issue{'s' if len(tickets) != 1 else ''} filed"),
        (len(verdicts), f"{len(verdicts)} plan{'s' if len(verdicts) != 1 else ''} written"),
        (len(decided), f"{len(to_devin)} to the AI · {len(to_person)} to your team"),
        (
            len([w for w in wos if w["status"] in ("dispatched", "devin")]),
            f"{len(to_person)} held for your team" if to_person else "all started",
        ),
        (len(sess), f"{len(sess)} fix{'es' if len(sess) != 1 else ''} · {spent_short}"),
        (
            len(passed),
            f"{len(passed)} passed · {sum(1 for x in sess if store.latest_verdict(x['id']) and store.latest_verdict(x['id'])['gate_result'] != 'pass')} failed",
        ),
        (len(reviewed), f"{len(reviewed)} reviewed"),
        (len(merged), f"{len(merged)} shipped · {len(ready)} waiting for you"),
    ]
    fixed_sessions = [
        x
        for x in sess
        if x["terminal_at"] and x["status_detail"] in ("finished", "waiting_for_user")
    ]
    verified_sessions = [
        x for x in passed if (store.latest_verdict(x["id"]) or {}).get("review_severity")
    ]
    counts5 = [
        (len(tickets), f"{len(tickets)} issue{'s' if len(tickets) != 1 else ''} filed"),
        (
            len(verdicts),
            f"{len(verdicts)} plan{'s' if len(verdicts) != 1 else ''} · {len(to_devin)} to the AI · {len(to_person)} to your team",
        ),
        (
            len(fixed_sessions),
            f"{len(fixed_sessions)} pull request{'s' if len(fixed_sessions) != 1 else ''} opened",
        ),
        (
            len(verified_sessions),
            f"{len(passed)} passed the tests · {len(verified_sessions)} reviewed",
        ),
        (len(merged), f"{len(merged)} shipped · {len(ready)} waiting for you"),
    ]
    loop = []
    for i, (name, meaning, covers) in enumerate(STAGE5):
        actor = STAGE5_ACTOR[i]
        dots = []
        for t in tickets:
            pat = pats[t["id"]]
            pos = _pos(pat)
            if pos not in covers:
                continue
            st = pat[pos]
            d = dot(store, t["id"], st == "d")
            issue = (
                (t.get("external_ref") or "").rsplit("#", 1)[-1]
                if t.get("external_ref") and "#" in t["external_ref"]
                else ""
            )
            state = (
                "done here"
                if st == "d"
                else ("held for your team" if st == "b" else "waiting here")
            )
            dots.append(
                {
                    **d,
                    "L": d["ref"],
                    "bg": d["dotBg"],
                    "fg": d["dotFg"],
                    "ring": d["color"],
                    "title": f"{d['ref']} {t.get('title', '')[:70]} · {state}"
                    + (f" · issue #{issue}" if issue else ""),
                    "go": url("/tickets-page", open=t["id"]),
                }
            )
        loop.append(
            {
                "name": name,
                "meaning": meaning,
                "internal": " · ".join(STAGE_PLAIN[k][2] for k in covers),
                "actor": ACTOR_PLAIN[actor],
                "color": ACT[actor][0],
                "count": str(counts5[i][0]),
                "context": counts5[i][1],
                "numColor": PL["person"][0] if i == 4 else INK,
                "dots": dots,
            }
        )
    needs = []
    for n in h["needs"]:
        kind = "ok" if n["kind"] == "ready to merge" else "bad"
        needs.append(
            {
                **dot(store, n["ticket_id"], False),
                "ticket": n["ticket_id"],
                "kind": n["kind"],
                **pill(kind),
                "reason": n["reason"],
                "action": n["action"],
                "go": url("/tickets-page", open=n["ticket_id"]),
            }
        )
    raw_mode = q.get("tl") == "raw"
    open_groups = {x for x in (q.get("open") or "").split(",") if x}
    events = [ev(e) for e in reversed(store.timeline(limit=40))]
    groups = _group_events(events, open_groups, raw_mode)
    on, off = ("#e2e9f6", "#2457a8"), ("transparent", "#8f97a3")
    b = store.budget_state()
    sp0 = cost.spend(store)
    human_hours = 0.0
    for x in passed:
        wo = store.get_work_order(x["work_order_id"]) or {}
        human_hours += SIZE_HOURS.get(str(wo.get("est_size") or "S").upper(), 1.0)
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
                "human": human_hours,
                "humanHelp": HUMAN_HELP
                + f" Here: {len(passed)} fix(es) that passed the tests, {human_hours:g} h in total; the AI worked {sp0['active_min']:.0f} minutes for them.",
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
        tid = n["ticket"]
        t = store.get_ticket(tid) or {}
        what = (t.get("title") or "")[:64]
        if n["kind"] == "ready to merge":
            mn = reduce_mod.merge_notes(store, tid)
            what = " · ".join(mn["reviews"]) + (
                f" · {len(mn['notes'])} note(s)" if mn["notes"] else ""
            )
        short_needs.append({**n, "what": what or n["reason"][:64], "hover": n["reason"]})
    rng = q.get("range", "run")
    spark = _sparklines(store, rng)
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
                **dot(store, e["ticket_id"], False),
                "kind": KIND_PLAIN.get(e["kind"], e["kind"]),
                **pill("bad"),
                "what": (t.get("title") or e["reason"])[:70],
                "hover": e["reason"],
                "age": _age(e["created_at"]),
                "go": url("/tickets-page", open=e["ticket_id"]),
                "answerUrl": f"/tickets/{e['ticket_id']}/answer" if can_answer else "",
                "mergeUrl": "",
                "dismissUrl": f"/escalations/{e['id']}/resolve",
            }
        )
    for tid in h["summary"]["ready"]:
        mn = reduce_mod.merge_notes(store, tid)
        inbox.append(
            {
                **dot(store, tid, False),
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
                "go": url("/tickets-page", open=tid),
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
                    **dot(store, x["ticket_id"], False),
                    "ticket": x["ticket_id"],
                    "stage": "scoping",
                    "elapsed": ops._elapsed(x["created_at"], None),
                    "acu": _fmt_acu(x["acus_consumed"])
                    if sp_home["metered"]
                    else _usd_or_min(store, x, "tri", kind_rates_home),
                    "cap": f"{TRIAGE_ACU_CAP}" if sp_home["metered"] else "",
                    "pct": _pct(x["acus_consumed"], TRIAGE_ACU_CAP) if sp_home["metered"] else "0",
                    "last": (store.timeline(ticket_id=x["ticket_id"], limit=1) or [{}])[0].get(
                        "event", ""
                    ),
                    "needsInput": x["status_detail"] == "waiting_for_user",
                    "go": url("/tickets-page", open=x["ticket_id"]),
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
                **dot(store, tid, False),
                "ticket": tid,
                "stage": stage,
                "elapsed": ops._elapsed(x["created_at"], None),
                "acu": _fmt_acu(x["acus_consumed"])
                if sp_home["metered"]
                else _usd_or_min(store, x, "rep", kind_rates_home),
                "cap": (f"{b['per_session_cap']:.0f}" if b.get("per_session_cap") else "·")
                if sp_home["metered"]
                else "",
                "pct": _pct(x["acus_consumed"], b.get("per_session_cap"))
                if sp_home["metered"]
                else "0",
                "last": (store.timeline(session_id=x["id"], limit=1) or [{}])[0].get("event", ""),
                "needsInput": x["status_detail"] in ("waiting_for_user", "waiting_for_approval")
                and not x.get("pull_request_url"),
                "go": url("/tickets-page", open=tid),
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
        "range": rng,
        "ranges": [
            ("run", "this run", url("/")),
            ("24h", "24h", url("/", range="24h")),
            ("7d", "7d", url("/", range="7d")),
        ],
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


def _sparklines(store: Store, rng: str = "run") -> list[dict[str, Any]]:
    """Three small series over the run's own span in 24 equal bins: sessions started, spend,
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
            for k in ("sessions started", "ACU" if metered else "AI minutes", "checks passed")
        ]
    now_ = datetime.now(UTC)
    if rng == "24h":
        lo, hi, bins = now_ - timedelta(hours=24), now_, 24
    elif rng == "7d":
        lo, hi, bins = now_ - timedelta(days=7), now_, 28
    else:
        lo, hi = min(times), max(times)
        if (hi - lo).total_seconds() < 3600:
            hi = lo + timedelta(hours=1)
        bins = 24
    width = (hi - lo).total_seconds() / bins

    def bucket(t: datetime) -> int:
        return max(0, min(bins - 1, int((t - lo).total_seconds() // width)))

    labels = [(lo + timedelta(seconds=width * i)).strftime("%d %b %H:%M") for i in range(bins)]

    s_bins, a_bins, g_bins = [0.0] * bins, [0.0] * bins, [0.0] * bins
    fails = 0
    for t, acu in starts:
        if lo <= t <= hi:
            s_bins[bucket(t)] += 1
            a_bins[bucket(t)] += acu
    for t, g in verdicts:
        if not (lo <= t <= hi):
            continue
        if g == "pass":
            g_bins[bucket(t)] += 1
        else:
            fails += 1
    span = f"{lo.strftime('%d %b %H:%M')} to {hi.strftime('%d %b %H:%M')}"
    return [
        {
            "label": "sessions started",
            "svg": charts.bars(s_bins, labels, PURPLE, unit="sessions"),
            "span": span,
            "last": str(int(sum(s_bins))),
        },
        {
            "label": "ACU" if metered else "active minutes",
            "svg": charts.bars(a_bins, labels, INK, unit="ACU" if metered else "min"),
            "span": span,
            "last": f"{sum(a_bins):.1f}",
        },
        {
            "label": "checks passed",
            "svg": charts.bars(g_bins, labels, TEAL, unit="passes"),
            "span": span,
            "last": f"{int(sum(g_bins))} passed · {fails} failed",
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
        if e["layer"] == "gate":
            j = i + 1
            while (
                j < len(events)
                and events[j]["layer"] == "gate"
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


SOURCE_GROUPS = [
    ("General", {"inventory", "fork", "github", "manual", "issues", "webhook"}, ""),
    (
        "Scan Agent",
        {"scan"},
        "Next version. A scan session reads the repository for one class of problem and files what it finds here; the loop takes it from there.",
    ),
    (
        "Others",
        {"gmail", "slack", "email", "linear", "jira"},
        "Gmail and Slack are not connected. A new source is one new adapter; everything after intake stays the same.",
    ),
]
VIEWS = [("list", "List"), ("pipeline", "Pipeline")]


PLAIN_PHRASES = [
    ("the oracle is read-only", "the tests are not ours to change"),
    ("oracle read-only to sessions", "the AI may not change tests"),
    ("sessions never edit tests or CI", "the AI never edits tests or the build"),
    ("triage: ", ""),
    ("site(s)", "places"),
    ("Devin Review left", "the AI reviewer left"),
    ("Devin Review found no issues", "the AI reviewer found nothing"),
    ("remark(s)", "comments"),
    ("comment(s)", "comments"),
    ("gate", "checks"),
]


def plain(text: str) -> str:
    """Recorded reasons carry the vocabulary of the code that wrote them. Readers do not."""
    for a, b in PLAIN_PHRASES:
        text = text.replace(a, b)
    return text


def _summary(t: dict[str, Any], row: dict[str, Any]) -> str:
    """One sentence on what the ticket is doing right now, for someone who has not read the
    design."""
    st, route = t["status"], t.get("router_decision")
    sd, verdict = row.get("sd"), row.get("verdict")
    review = (
        _review_short(verdict["review_severity"])
        if verdict and verdict.get("review_severity")
        else "not requested"
    )
    if review == "no issues":
        review_txt = "Devin Review found no issues"
    elif "found" in review:
        review_txt = f"Devin Review left {review.split()[0]} remark(s)"
    else:
        review_txt = "Devin Review " + review
    if st == "merged":
        return plain(f"Merged by your team. Every check passed on a clean copy; {review_txt}.")
    if st == "escalated" or (route and route != "devin"):
        return plain(
            "For your team to decide: " + (t.get("router_reason") or "a person decides")[:150]
        )
    if st == "new":
        return "Waiting for a triage session to read it."
    if st in ("triaged", "routed"):
        return f"Scoped by the AI: {row.get('count') or 'the sites are known'}. Waiting for a repair session."
    if st in ("dispatched", "running"):
        el = (sd or {}).get("elapsed") or ""
        since = f", {el} so far" if el else ""
        return f"The AI is working on the fix{since}. Open the session to watch."
    if st in ("gated", "reviewed"):
        if verdict and verdict.get("gate_result") != "pass":
            return plain("Checks failed: " + (verdict.get("reason") or "see the log")[:150])
        if row.get("ready"):
            return plain(
                f"Ready for you: every check passed on a clean copy, {review_txt}. "
                "Read the pull request and merge."
            )
        if review == "requested":
            return "Every check passed on a clean copy. The AI reviewer is reading it now."
        return plain(f"Every check passed on a clean copy; {review_txt}. Waiting on your decision.")
    return row.get("note") or ""


def tickets(store: Store, cfg: TargetConfig, q: dict[str, str]) -> dict[str, Any]:
    tk_page = pages.tickets(store)
    sm = tk_page["summary"]
    info = {r["id"]: r for g in tk_page["groups"] for r in g["rows"]}
    view = q.get("view") if q.get("view") in dict(VIEWS) else "list"
    f = q.get("f", "all")
    open_ids = {x for x in (q.get("open") or "").split(",") if x}
    extra = {"view": view if view != "list" else None, "f": f if f != "all" else None}
    tr = tracker(store, cfg, q, base="/tickets-page", extra=extra)

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
    keep_open = ",".join(sorted(open_ids)) or None
    chips = [
        {
            "label": label,
            "set": url(
                "/tickets-page", view=extra["view"], f=key if key != "all" else None, open=keep_open
            ),
            "border": "#2457a8" if f == key else "#d6d2c9",
            "bg": "#2457a8" if f == key else "#fff",
            "fg": "#fff" if f == key else INK,
        }
        for key, label in FILTERS
    ]
    views = [
        {
            "label": label,
            "set": url(
                "/tickets-page", view=key if key != "list" else None, f=extra["f"], open=keep_open
            ),
            "on": key == view,
            "bg": "#1f2530" if key == view else "#fff",
            "fg": "#fff" if key == view else INK,
        }
        for key, label in VIEWS
    ]
    rows = []
    for r in tr["trackerRows"]:
        t = store.get_ticket(r["id"]) or {}
        i = info.get(r["id"], {})
        if not _passes({"route": t.get("router_decision"), "status": t.get("status")}, f):
            continue
        sd = r.get("sd")
        files = i.get("files") or []
        sites = i.get("sites") or 0
        r["count"] = (
            f"{len(files)} file{'s' if len(files) != 1 else ''} · {sites} site{'s' if sites != 1 else ''}"
            if files
            else f"{sites} site{'s' if sites != 1 else ''}"
        )
        r["summary"] = _summary(t, r)
        r["source"] = t.get("source") or ""
        r["title"] = i.get("title") or t.get("title") or r["id"]
        r["age"] = _age(t.get("created_at"))
        n_s = i.get("sessions", 0)
        r["nSessions"] = f"{n_s} session{'s' if n_s != 1 else ''}"
        tri = store.list_triage_sessions(r["id"])
        live_url = (sd or {}).get("url") or (tri[-1].get("url") if tri else None)
        r["sessionUrl"] = live_url or ""
        r["sessionLabel"] = (
            "open the session" if sd else ("open the triage session" if live_url else "")
        )
        r["prLabel"] = f"PR #{r['pr']}" if r.get("pr") and r["pr"] != "none" else ""
        r["scope"] = _ticket_panel(store, r["id"], f, q) if r["open"] else None
        r["isRunning"] = t.get("status") in ("dispatched", "running")
        rows.append(r)
    groups = []
    placed: set[str] = set()
    for name, sources, note in SOURCE_GROUPS:
        g_rows = [r for r in rows if r["source"] in sources]
        placed.update(r["id"] for r in g_rows)
        groups.append(
            {"name": name, "rows": g_rows, "note": note if not g_rows else "", "empty": not g_rows}
        )
    groups[0]["rows"].extend(r for r in rows if r["id"] not in placed)
    for g in groups:
        g["count"] = str(len(g["rows"]))
    return {
        "sm": summary,
        "chips": chips,
        "views": views,
        "isList": view == "list",
        "groups": groups,
        "trackerRows": rows,
        "stageHead": tr["stageHead"],
        "legend": tr["legend"],
        "ticketCount": f"{len(rows)}"
        + ("" if len(rows) == len(tr["trackerRows"]) else f" of {len(tr['trackerRows'])}"),
        "noTickets": not rows,
        "emptyText": EMPTY.get(f, "No tickets."),
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
        "track": "/tickets-page?view=pipeline",
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
        "track": url("/tickets-page", open=d["id"]),
    }


# ---------------------------------------------------------------------------- tracker
def tracker(
    store: Store,
    cfg: TargetConfig,
    q: dict[str, str],
    *,
    base: str = "/tickets-page",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    tr = pages.tracker(store)
    extra = extra or {}
    open_ids = {x for x in (q.get("open") or "").split(",") if x}
    metered = cost.spend(store)["metered"]
    kind_rates = cost.rates(store)

    def _spent(sd: dict[str, Any] | None) -> tuple[str, str]:
        """(short label for the strip, the line under the session) for one repair session."""
        if not sd:
            return "", ""
        if metered:
            return f"{_fmt_acu(sd['acus'])} ACU", f"{_fmt_acu(sd['acus'])} ACU"
        row = store.get_session(sd["id"]) or {}
        mins = cost.repair_active_seconds(store, row) / 60.0 if row else 0.0
        usd, _src = cost.session_usd(store, row, "rep", kind_rates) if row else (None, "")
        money = f"${usd:.2f} · " if usd is not None else ""
        return f"{mins:.0f} min", f"{money}{mins:.0f} min of AI work"

    store.budget_state().get("per_session_cap") or cfg.max_acu_limit
    rows = []
    for r in tr["rows"]:
        t = store.get_ticket(r["id"]) or {}
        pat = _pattern(store, t) if t else "--------"
        sd = (r.get("sessions_detail") or [None])[-1]
        verdict = sd["verdict"] if sd and sd.get("verdict") else None
        claim = sd["claim"] if sd and isinstance(sd.get("claim"), dict) else {}
        passed_t1 = [e for e in (sd["evidence"] if sd else []) if e["tier"] == "T1"]
        checks = (
            f"{sum(1 for e in passed_t1 if e['passed'])} of {len(passed_t1)}" if passed_t1 else ""
        )
        review = (
            _review_short(verdict["review_severity"])
            if verdict and verdict.get("review_severity")
            else ""
        )
        p4 = fold4(pat)
        sites = (
            len((json.loads(t["triage_verdict_json"]) or {}).get("sites") or [])
            if t.get("triage_verdict_json")
            else 0
        )
        labels = [
            (
                "your team"
                if p4[0] == "b"
                else (f"{sites} place{'s' if sites != 1 else ''}" if sites else "")
            ),
            (
                "no fix"
                if p4[1] == "b"
                else (
                    "PR #" + (sd["pr_url"] or "").rsplit("/", 1)[-1]
                    if sd and sd.get("pr_url")
                    else ""
                )
            ),
            (checks + (f" · {review}" if checks and review else review)),
            ("you" if r["merged"] else ("ready" if r["ready"] else "")),
        ]
        cells = []
        for i, (name, actor, _idx, note) in enumerate(STG4):
            st = p4[i]
            col = ACT[actor][0]
            cells.append(
                {
                    "title": f"{name}: {note} · {STATE_NAME[st]}"
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
                **dot(store, r["id"], r["merged"]),
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
                "toggle": url(base, **extra, open=",".join(sorted(others)) or None),
                "route": r["route"],
                "ready": bool(r["ready"]),
                "sd": sd,
                "verdict": verdict,
                "hasShard": bool(sd),
                "files": ", ".join(sd["files"]) if sd else "",
                "session": (sd["devin_id"] or sd["id"])[:12] if sd else "",
                "sessionUrl": (sd["url"] or "#") if sd else "#",
                "pr": (sd["pr_url"] or "").rsplit("/", 1)[-1]
                if sd and sd.get("pr_url")
                else "none",
                "prUrl": (sd["pr_url"] or "#") if sd else "#",
                "acuLine": f"{_spent(sd)[1]} · size {(sd['size'] or '?').upper()} · retries {sd['retries']} · {sd['elapsed']}"
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
        "stageHead": [
            {"name": n, "actor": ACT[a][2], "color": ACT[a][0], "note": note}
            for n, a, _i, note in STG4
        ],
        "legend": [{"label": ACT[k][2], "color": ACT[k][0]} for k in ("devin", "gate", "person")],
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
        kind_rates = cost.rates(store)
        for r in store._all("SELECT * FROM sessions"):
            mins[r["id"]] = cost.repair_active_seconds(store, r) / 60.0
            usd_rows[r["id"]] = cost.session_usd(store, r, "rep", kind_rates)
        for r in store.list_triage_sessions():
            mins[r["id"]] = cost.triage_active_seconds(store, r) / 60.0
            usd_rows[r["id"]] = cost.session_usd(store, r, "tri", kind_rates)
    rows = []
    for s in ss["sessions"]:
        is_triage = s.get("kind") == "triage"
        st_kind = _pill_kind(s["pill"])
        size = (s.get("size") or "").upper()
        gate = s.get("gate")
        rows.append(
            {
                **dot(store, s["ticket"], False),
                "id": (s.get("devin_id") or s["id"])[:12],
                "url": s.get("url") or "#",
                "tk": s["ticket"],
                "shard": "triage" if is_triage else s["shard"],
                "track": url("/tickets-page", open=s["ticket"]),
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
                    else f"{mins.get(s['id'], 0.0):.0f} min"
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
                "prUrl": s.get("pr_url") or url("/tickets-page", open=s["ticket"]),
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
                else url("/tickets-page", open=s["ticket"]),
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
        "cap": f"{cap:.0f}"
        if (s.get("acus_consumed") or 0) > 0
        else f"Devin's limit {cap:.0f} ACU",
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
def _took(a: str | None, b: str | None) -> str:
    if not a or not b:
        return "running"
    from datetime import datetime

    secs = (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds()
    return f"{int(secs // 60)} min {int(secs % 60)} s" if secs >= 60 else f"{int(secs)} s"


def _run_line(res: dict[str, Any]) -> str:
    if not res:
        return "no result recorded"
    if res.get("error"):
        return f"failed: {res['error']}"
    parts = []
    if "issues" in res:
        n_new = len(res.get("new_tickets") or [])
        parts.append(f"{res['issues']} issue(s) found, {n_new} new ticket(s)")
    if "triaged" in res:
        parts.append(f"{res['triaged']} triage session(s)")
    if "dispatched" in res:
        parts.append(f"{res['dispatched']} repair session(s)")
    if "gated" in res:
        parts.append(f"{res['gated']} checked")
    if res.get("escalated"):
        parts.append(f"{res['escalated']} handed to your team")
    return " · ".join(parts) or "nothing to do"


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
    open_ids = {x for x in (q.get("open") or "").split(",") if x}
    add_open = q.get("add") == "1" or err
    mono = "'JetBrains Mono',monospace"
    autos = []
    for r in a["rows"]:
        is_next = r["availability"] == "next"
        state_kind = (
            "na" if is_next else ("run" if r["running"] else ("ok" if r["enabled"] else "na"))
        )
        is_open = r["id"] in open_ids
        others = (open_ids - {r["id"]}) if is_open else (open_ids | {r["id"]})
        t = r["trigger"]
        src = t.get("source", "")
        if src == "github" and t.get("event") == "issues":
            trig = f"issues on {r['target']} with label {t.get('issue_label') or 'any'}"
            how = "on click, by webhook" + (f", {r['schedule']}" if r.get("schedule") else "")
        elif src == "github":
            trig = f"{t.get('event', '')} on {r['target']}" + (
                " · " + ", ".join(f"{k}={v}" for k, v in (t.get("match") or {}).items())
                if t.get("match")
                else ""
            )
            how = "by webhook"
        elif src == "schedule":
            trig = "on a schedule"
            how = r.get("schedule") or "no schedule set"
        elif src == "manual":
            trig = "on click only"
            how = "the Run button"
        else:
            trig = f"{src}:{t.get('event', '')}"
            how = "by webhook"
        runs = store.list_automation_runs(r["id"], 8)
        produced: list[str] = []
        for run in runs:
            for tid in run["result"].get("new_tickets") or []:
                if tid not in produced:
                    produced.append(tid)
        kind_label = {
            "repair": "event-based · default",
            "scan": "scan · next version",
            "custom": "event-based",
        }.get(r["kind"], r["kind"])
        autos.append(
            {
                "id": r["id"],
                "name": r["name"],
                "kindLabel": kind_label,
                "state": "next version"
                if is_next
                else ("running" if r["running"] else ("on" if r["enabled"] else "off")),
                "stBg": PL[state_kind][1],
                "stFg": PL[state_kind][0],
                "trigger": trig,
                "how": how,
                "playbook": r["playbook"] or "none",
                "limit": f"{int(r['max_acu'])} ACU" if r.get("max_acu") else "none",
                "conc": str(r["concurrency"]),
                "lastRun": (
                    f"last run {r['last_run'][:16].replace('T', ' ')}"
                    if r.get("last_run")
                    else "never run"
                ),
                "isNext": is_next,
                "opacity": ".72" if is_next else "1",
                "open": is_open,
                "chev": "▲" if is_open else "▼",
                "bg": "#faf9f6" if is_open else "#fff",
                "toggle": url("/automations", open=",".join(sorted(others)) or None),
                "toggleUrl": f"/automations/{r['id']}/toggle",
                "toggleLabel": "Switch off" if r["enabled"] else "Switch on",
                "runnable": bool(r["runnable"]),
                "canRun": bool(r["enabled"]) and not r["running"] and not running,
                "runUrl": f"/automations/{r['id']}/run",
                "runLabel": "Running…" if r["running"] else "Run",
                "removable": r["kind"] in ("custom", "scan") and r["id"] != "auto_scan",
                "removeUrl": f"/automations/{r['id']}/delete",
                "desc": r.get("kind_note") or "",
                "notes": r.get("notes") or "",
                "rows": [
                    {"k": "what starts it", "v": trig, "mono": True},
                    {"k": "how it runs", "v": how, "mono": False},
                    {"k": "repository", "v": r["target"], "mono": True},
                    {"k": "playbook", "v": r["playbook"] or "none", "mono": True},
                    {
                        "k": "per session",
                        "v": f"Devin's limit {int(r['max_acu'])} ACU · {r['concurrency']} sessions at once"
                        if r.get("max_acu")
                        else f"{r['concurrency']} sessions at once",
                        "mono": False,
                    },
                    {
                        "k": "on the Devin org",
                        "v": (
                            f"native Automation {r['native'].get('id') or r['native'].get('automation_id')}"
                            if r.get("native")
                            else a["native_note"]
                        ),
                        "mono": False,
                    },
                ],
                "runs": [
                    {
                        "when": run["started_at"][:16].replace("T", " "),
                        "took": _took(run["started_at"], run.get("finished_at")),
                        "line": _run_line(run["result"]),
                        **pill(
                            "run"
                            if run["status"] == "running"
                            else ("bad" if run["status"] == "failed" else "ok")
                        ),
                        "status": run["status"],
                    }
                    for run in runs
                ],
                "hasRuns": bool(runs),
                "produced": [
                    {"id": tid, "go": url("/tickets-page", open=tid), "color": tk_color(tid)}
                    for tid in produced
                ],
            }
        )
    return {
        "autos": autos,
        "addOpen": add_open,
        "addUrl": url(
            "/automations", add=None if add_open else "1", open=",".join(sorted(open_ids)) or None
        ),
        "autoErr": err,
        "autoName": name,
        "autoNameBorder": PL["bad"][0] if err else "#d6d2c9",
        "autoNameShadow": f"0 0 0 3px {PL['bad'][1]}" if err else "none",
        "autoFoot": (
            f"{a['routed']} ticket(s) routed and waiting for the next run"
            if a["routed"]
            else "Nothing is waiting for a run."
        )
        + ("" if a["live"] else " · replay: sessions are simulated, the gate is skipped"),
        "cap": a["cap"],
        "playbookNames": a["playbook_names"],
        "triggerChoices": a["trigger_choices"],
        "mono": mono,
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
                "meta": f"{r['name']} · {len(r['sections'])} sections · schema: {len(r['schema_fields'])} fields · Devin's limit {int(r['max_acu']) if r.get('max_acu') else '·'} ACU · {r['source']}"
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
            "meta": f"Devin's limit {int(d['max_acu']) if d.get('max_acu') else '·'} ACU · source {d['source']} · updated {d['updated_at'][:16].replace('T', ' ')}",
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
def _bound(passed: int, total: int) -> str:
    """What a clean record does and does not allow you to say. With no failures seen, the honest
    ceiling on the failure rate is about three over the number of tries."""
    if not total:
        return "no change has been checked yet"
    base = "we re-ran the project's own tests on a clean copy of every change"
    if passed == total:
        n = "None" if total > 1 else "It"
        return f"{base}. {n} failed. {total} changes is too few to state a rate"
    return f"{base}. {total - passed} of {total} failed and never reached a person"


def _plain_funnel(rows: list[tuple[str, int, Any]]) -> list[tuple[str, int, int]]:
    """The funnel in words a newcomer reads, with each drop folded under the step it left."""
    names = {
        "tickets decided": "jobs taken on",
        "routed to Devin": "given to the AI",
        "refused or human-only": "handed to your team",
        "sessions created": "the AI started work",
        "sessions terminal": "the AI finished",
        "gate passed": "passed our checks",
        "gate failed at least once (retried or escalated)": "failed our checks",
        "human-merged": "merged by your team",
    }
    out: list[tuple[str, int, int]] = []
    for label, n, drop in rows:
        name = names.get(label, label)
        if drop and out:
            prev = out[-1]
            out[-1] = (prev[0], prev[1], n)
            continue
        if label in ("sessions created", "sessions terminal"):
            continue  # two rows saying the same thing to anyone outside this codebase
        out.append((name, n, 0))
    return out


def report(
    store: Store, cfg: TargetConfig, inventory_dir: Path, q: dict[str, Any]
) -> dict[str, Any]:
    """The page an engineering leader reads to answer: how would I know this is working.

    Three rates, each a count over a stated denominator, the checks we ran ourselves, what the
    work cost, and the log. Nothing here is a percentile and nothing is a percentage without the
    number it came from: the run is small, and the page says so."""
    rep = report_mod.build(store, inventory_dir)
    sp = cost.spend(store)
    ver = rates.verification(store)
    inter = rates.intervention(store)
    acc = rates.acceptance(store)
    live = rates.liveness(store)
    checks_open = q.get("checks") == "1"
    log_open = q.get("log") == "1"
    log_ticket = q.get("lt") or ""

    def card(
        key: str,
        title: str,
        n: int,
        of: int,
        unit: str,
        rows: list[dict[str, Any]],
        note: str,
        colour: str,
    ) -> dict[str, Any]:
        return {
            "key": key,
            "title": title,
            "n": str(n),
            "of": f"of {of}" if of else "",
            "unit": unit,
            "color": colour if of and n == of else (INK if of else FAINT),
            "rows": [
                {
                    "label": r["label"],
                    "n": str(r["n"]),
                    "color": INK if r["n"] else FAINT,
                    "pct": _pct(r["n"], r.get("of", of)) if r.get("of", of) else "0",
                }
                for r in rows
            ],
            "note": note,
        }

    cards = [
        card(
            "verified",
            "Checks passed",
            ver["changes_passed"],
            ver["changes"],
            "changes the AI wrote",
            ver["kinds"],
            _bound(ver["changes_passed"], ver["changes"]),
            PL["ok"][0],
        ),
        card(
            "hands-off",
            "Ran without you",
            inter["untouched"],
            inter["tickets"],
            "jobs taken on",
            inter["rows"],
            "merging is yours by design and is not counted as stepping in",
            PL["gate"][0],
        ),
        card(
            "accepted",
            "Merged by your team",
            acc["merged"],
            acc["offered"],
            "changes offered",
            acc["rows"],
            "read from the pull requests themselves, so one you close shows up here. "
            + (
                f"{acc['mergers']} person merged them"
                + (
                    f", in {acc['merge_events']} sittings."
                    if acc["merge_events"] > 1
                    else ", in one sitting."
                )
                if acc["merged"]
                else "nothing merged yet."
            ),
            PL["devin"][0],
        ),
    ]

    receipts = rates.claim_vs_check(store)
    by_change: dict[str, list[dict[str, Any]]] = {}
    for r in receipts:
        by_change.setdefault(r["ticket_id"], []).append(r)
    check_groups = []
    for tid, rows in by_change.items():
        t = store.get_ticket(tid) or {}
        check_groups.append(
            {
                "ref": ref(t.get("number")),
                "color": tk_color(tid),
                "session": rows[0]["session"],
                "url": rows[0]["url"],
                "go": url("/tickets-page", open=tid),
                "rows": [
                    {
                        **r,
                        "state": "passed" if r["passed"] else f"exit {r['exit']}",
                        **pill("ok" if r["passed"] else "bad"),
                    }
                    for r in rows
                ],
            }
        )

    events = [ev(e) for e in reversed(store.timeline(ticket_id=log_ticket or None, limit=400))]
    if not log_open:
        events = events[:12]
    log_tickets = [
        {"ref": ref(t.get("number")), "id": t["id"], "on": t["id"] == log_ticket}
        for t in store.list_tickets()
    ]

    bd = rep["burndown"]
    fun = _plain_funnel(rep["funnel"])
    usd = usd_label(sp) or f"{sp['active_min']:.0f} min"
    per_change = (
        f"${sp['usd'] / ver['changes_passed']:.2f}"
        if sp.get("usd") and ver["changes_passed"]
        else "n/a"
    )
    return {
        "window": rates.window(store),
        "live": {
            **live,
            "runningLabel": f"{live['running']} session{'s' if live['running'] != 1 else ''} working now"
            if live["running"]
            else "nothing running",
            "lastRun": (live["last_run_at"] or "")[:16].replace("T", " ") or "never",
            "lastLine": _run_line(live["last_run_result"]),
            "watching": " · ".join(live["watching"]) or "nothing enabled",
            "failure": (live["last_failure"] or {}).get("event") or "none recorded",
            "dot": PL["run"][0] if live["running"] else PL["ok"][0],
        },
        "cards": cards,
        "checksLine": f"{sum(1 for r in receipts if r['passed'])} of {len(receipts)} checks passed, "
        f"each re-run by this app on a clean copy of the change",
        "checksOpen": checks_open,
        "checksToggle": url(
            "/report", checks=None if checks_open else "1", log="1" if log_open else None
        ),
        "checksToggleLabel": "hide the checks" if checks_open else "show every check we ran",
        "checkGroups": check_groups,
        "checksNote": "Each command ran in a fresh copy of the repository that the AI session could "
        "not write to. The tree is the exact code it ran on; a result recorded against any other "
        "tree is ignored.",
        "funnel": charts.funnel([(n, c, d or None) for n, c, d in fun]),
        "funnelNote": f"{fun[0][1] if fun else 0} jobs · what left the line, in red",
        "burndown": charts.stacked_bar(
            [
                ("fixed and merged", bd["fixed"], PL["ok"][0]),
                ("to your team", bd["human"], PL["person"][0]),
                ("left", max(bd["total"] - bd["fixed"] - bd["human"], 0), "#c9c5bb"),
            ]
        ),
        "burndownNote": f"{bd['total']} places in the code, counted before the run",
        "cost": {
            "total": usd,
            "perChange": per_change,
            "n": f"{sp['n_console']} of {sp['n_sessions']} priced from the console, the rest at our own rate",
            "minutes": f"{sp['active_min']:.0f} min of AI work",
        },
        "log": events,
        "logOpen": log_open,
        "logToggle": url(
            "/report", log=None if log_open else "1", checks="1" if checks_open else None
        ),
        "logToggleLabel": "show less" if log_open else "show the whole log",
        "logTickets": log_tickets,
        "logAll": url(
            "/report", log="1" if log_open else None, checks="1" if checks_open else None
        ),
        "logFilter": [
            {
                "label": t["ref"],
                "on": t["on"],
                "set": url(
                    "/report",
                    lt=None if t["on"] else t["id"],
                    log="1" if log_open else None,
                    checks="1" if checks_open else None,
                ),
            }
            for t in log_tickets
        ],
        "notMeasured": [
            ("does the fix still hold after 30 days", "the window has not passed"),
            ("minutes your engineers spent reviewing", "not instrumented in this run"),
            ("security findings", "no scanner runs in this loop"),
            ("continuous integration results", "the fork runs none"),
        ],
        "refused": [
            ("lines of code", "volume is not value"),
            ("pull requests opened", "opening one is free; merging one is not"),
            ("acceptance rate of suggestions", "rubber-stamping inflates it"),
            ("share of code written by AI", "not a measure of anything working"),
            ("tokens", "an input, not a result"),
            (
                "time saved, self-reported",
                "the best trial found self-reports wrong by about 40 points",
            ),
        ],
    }
