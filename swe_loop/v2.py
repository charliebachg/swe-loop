"""View models for the designed pages: the mock's field contract, filled from the real store.

The mock (`swe-loop v2`) was built as a clickable component with its own state; here every
interaction is a URL the server renders, so nothing lives only in the browser. Colours and labels
are computed the way the mock computed them, from the same constants."""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from markupsafe import Markup, escape

from swe_loop import charts, codescan, cost, ops, pages, rates
from swe_loop import reduce as reduce_mod
from swe_loop import report as report_mod
from swe_loop.config import Settings, TargetConfig
from swe_loop.store import Store, clip, plural
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
SOURCE_PLAIN = {"github": "issues", "scan": "scan agent", "code_scan": "security scan"}


def source_tag(t: dict[str, Any] | None) -> str:
    """Where a ticket came from, in the words the Automations page uses."""
    src = (t or {}).get("source") or ""
    return SOURCE_PLAIN.get(src, src or "")


KIND_PLAIN = {
    "human_only": "needs you",
    "refuse": "waiting",
    "waiting_for_user": "the AI asked a question",
    "review_blocked": "did not finish",
    "usage_limit": "too big for one run",
    "oracle_touched": "check the test change",
    "ready to merge": "ready to merge",
    "router_refused": "waiting",
    "detector_still_fires": "the problem is still there",
    "missing_evidence": "could not be checked",
}
EVENT_PLAIN = {
    # written by an older version, which logged the internal name
    "human_only": "handed to your team",
    "router_refused": "put behind another change",
    "oracle_touched": "a test changed, so someone has to look",
    "review_blocked": "the review did not finish",
    "detector_still_fires": "the problem is still there",
    "new/-": "queued",
    "claimed/-": "picked up",
    "running/-": "started",
    "running/working": "writing code",
    "running/waiting_for_user": "finished its turn",
    "exit/finished": "finished",
    "suspended/inactivity": "went idle",
    "error/-": "errored",
}
STATUS_PLAIN = {
    "new": "not started",
    "triaged": "planned",
    "routed": "ready to start",
    "dispatched": "AI working",
    "running": "AI working",
    "gated": "checked",
    "reviewed": "ready for you",
    "merged": "merged",
    "escalated": "needs you",
    "refused": "waiting",
}
LAYER_PLAIN = {
    "intake": "received",
    "triage": "AI scoping",
    "scan": "AI looking",
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
# Four pages of their own. The review shows on every ticket, and the organisation's own settings
# and the next version live under the gear, where a person looks for them.
DEVIN_NAV = [
    ("sessions", 1, ""),
    ("playbooks", 1, ""),
    ("knowledge", 1, ""),
    ("insights", 1, ""),
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
        # a scan is not opened against a ticket, and a blank cell says that better than a
        # placeholder that looks like a number failed to load
        "ref": ref(t.get("number")) if ticket_id else "none",
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


def _said(event: str) -> str:
    """One logged event in the words the pages use, wherever it is shown."""
    return EVENT_PLAIN.get(event, plain(event or ""))


def ev(e: dict[str, Any], nums: dict[str, Any] | None = None) -> dict[str, Any]:
    """One line of the log. `nums` maps a ticket's stored id to the number it is called by on
    screen; without it the line carries no reference, because a stored id is not a name anyone
    reading the log would recognise."""
    actor = _actor_for_layer(e.get("layer", ""), e.get("event", ""))
    tid = e.get("ticket_id") or ""
    detail = e.get("detail") or ""
    if re.fullmatch(r"acus=0(\.0+)?", detail):
        detail = ""
    return {
        "time": _hhmmss(e.get("at")),
        "layer": LAYER_PLAIN.get(e.get("layer", ""), e.get("layer", "")),
        "event": EVENT_PLAIN.get(e.get("event", ""), plain(e.get("event", ""))),
        "detail": plain(detail),
        "ref": (ref(nums.get(tid)) if nums and tid in nums else ""),
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


def _session_price(usd: tuple[float | None, str] | None, minutes: float) -> str:
    """What this session cost. Devin's own scans run sub-sessions we never poll and the console
    does not itemise them, so there is nothing to show and nothing is better than a false zero."""
    if usd is None or usd[0] is None:
        return f"{minutes:.1f} min"
    if usd[1] == "rate" and minutes <= 0:
        return "not priced"
    return f"${usd[0]:.2f}"


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
        return {
            "terminated": "stopped",
            "inactivity": "idle",
            "out_of_credits": "out of credits",
        }.get(d, d)
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
    """Every session with its Devin id, minutes, and the console figure if entered: the Settings
    form. Every kind of session is here, so the table and the total count the same work."""
    kr = cost.rates(store)
    nums = {t["id"]: t.get("number") for t in store.list_tickets()}

    def row(devin_id: str, label: str, secs: float, usd: Any, rate: float) -> dict[str, Any]:
        return {
            "devin_id": devin_id or "",
            "label": label,
            "minutes": f"{secs / 60.0:.1f}",
            "usd": usd,
            "est": f"{secs / 60.0 * rate:.2f}" if usd is None else "",
        }

    out = []
    parts: dict[str, list[str]] = {}
    for w in store._all("SELECT ticket_id, id FROM work_orders ORDER BY rowid"):
        parts.setdefault(w["ticket_id"], []).append(w["id"])
    for r in store._all("SELECT * FROM sessions ORDER BY created_at"):
        wo = store.get_work_order(r["work_order_id"]) or {}
        tid = wo.get("ticket_id", "")
        shard = wo.get("shard_id", "")
        label = "wrote the fix for " + ref(nums.get(tid))
        # the part is worth naming only when the ticket was split; the internal id never is
        sibling = parts.get(tid, [])
        if len(sibling) > 1:
            if len(shard) == 1 and shard.isalpha():
                label += f", shard {shard.upper()}"
            elif wo.get("id") in sibling:
                label += f", part {sibling.index(wo['id']) + 1} of {len(sibling)}"
        out.append(
            row(
                r["devin_session_id"],
                label,
                cost.repair_active_seconds(store, r),
                r.get("cost_usd"),
                kr["rep"],
            )
        )
    for r in store.list_triage_sessions():
        out.append(
            row(
                r["devin_session_id"],
                "scoped " + ref(nums.get(r["ticket_id"])),
                cost.triage_active_seconds(store, r),
                r.get("cost_usd"),
                kr["tri"],
            )
        )
    for r in store.list_scan_sessions():
        out.append(
            row(
                r["devin_session_id"],
                "read the repository",
                cost.scan_active_seconds(store, r),
                r.get("cost_usd"),
                kr["scn"],
            )
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


def _spent_caption(sp: dict[str, Any]) -> str:
    """Under the spend figure: what the number leaves out, so nobody reads it as the whole bill.
    Two gaps can exist, a session of ours nobody priced and sessions Devin ran on its own, and each
    is named only when it is there."""
    gaps = []
    if sp.get("n_unpriced"):
        gaps.append(f"{plural(sp['n_unpriced'], 'session')} we cannot price")
    if sp.get("n_devin_own"):
        gaps.append(f"{plural(sp['n_devin_own'], 'session')} Devin ran on its own, not priced")
    return "spent · " + " · ".join(gaps) if gaps else "spent, every session counted"


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


def _analysis_detail(r: dict[str, Any]) -> dict[str, Any]:
    """Devin's written analysis of one session, shaped for the page: the problems it hit with
    their impact, what it says is worth doing and of what kind, the timeline it wrote, the prompt
    it would have preferred, and how the knowledge notes served it."""
    from swe_loop import insights as ins

    a = r.get("analysis") or {}
    impact_colour = {"high": PL["bad"][0], "medium": PL["run"][0], "low": FAINT}
    issues = [
        {
            "title": i.get("title") or i.get("label") or "issue",
            "label": (i.get("label") or "").replace("_", " "),
            "impact": (i.get("impact") or "").lower(),
            "color": impact_colour.get((i.get("impact") or "").lower(), FAINT),
            "text": i.get("issue") or ins._text(i),
        }
        for i in a.get("issues") or []
        if isinstance(i, dict)
    ]
    actions = [
        {
            "type": (i.get("type") or "other").replace("_", " "),
            "text": i.get("action_item") or ins._text(i),
        }
        for i in a.get("action_items") or []
        if isinstance(i, dict)
    ]
    timeline = [
        {"title": i.get("title") or "", "text": i.get("description") or ""}
        for i in a.get("timeline") or []
        if isinstance(i, dict)
    ]
    sp = a.get("suggested_prompt") or {}
    prompt = sp.get("suggested_prompt") if isinstance(sp, dict) else (sp or "")
    nu = a.get("note_usage") or {}
    notes = {
        "good": [ins._text(x) for x in (nu.get("good_usages") or [])]
        if isinstance(nu, dict)
        else [],
        "bad": [ins._text(x) for x in (nu.get("bad_usages") or [])] if isinstance(nu, dict) else [],
    }
    return {
        "issues": issues,
        "actions": actions,
        "timeline": timeline,
        "prompt": prompt or "",
        "notes": notes,
        "empty": not (issues or actions or timeline or prompt),
    }


def _insight_modal(
    table: list[dict[str, Any]], listed: list[dict[str, Any]], view: str, q: dict[str, str]
) -> dict[str, Any] | None:
    """The one row a person asked to see, with Devin's analysis laid out from its JSON and the
    JSON itself underneath, pretty-printed."""
    row = next((x for x in table if x["sid"] == view and x["state"] == "view"), None)
    if row is None:
        return None
    rec = next(r for r in listed if r.get("session_id") == view)
    return {
        **row,
        "detail": _analysis_detail(rec),
        "raw": json.dumps(rec.get("analysis"), indent=2, ensure_ascii=False),
        "closeUrl": url("/devin/insights", panel=q.get("panel") or None),
    }


def insights(
    store: Store, q: dict[str, str] | None = None, pending: set[str] | None = None
) -> dict[str, Any]:
    """Devin's own read on the sessions it ran, mirrored here so the behaviour can be watched
    and the instructions improved. Everything on this page comes from Session Insights; nothing
    on it is our own measurement, which is the point of keeping it separate from the Report.

    Three rates carry the page and nothing else shows until a person asks for the working."""
    from swe_loop import insights as ins

    q = q or {}
    pending = pending or set()
    view = q.get("view") or ""
    open_panels = {x for x in (q.get("panel") or "").split(",") if x}
    rows = store.insights()
    started = ins.known_ids(store)
    t = ins.turns(rows)
    adv = ins.advice(rows)
    sizes = ins.by_size(rows)
    tools = ins.tools(rows)
    nopb = ins.no_playbook(rows)
    right = sum(1 for r in rows if (r.get("session_size") or "").lower() not in ("l", "xl"))

    def panel(key: str, title: str, n: int, of: int, unit: str, colour: str) -> dict[str, Any]:
        is_open = key in open_panels
        return {
            "key": key,
            "title": title,
            "n": str(n),
            "of": str(of),
            "unit": unit,
            "pct": _pct(n, of) if of else "0",
            "open": is_open,
            "color": colour if of and n == of else (INK if of else FAINT),
            "toggle": url(
                "/devin/insights",
                panel=",".join(sorted(open_panels - {key} if is_open else open_panels | {key}))
                or None,
            ),
        }

    nums = {tk["id"]: tk.get("number") for tk in store.list_tickets()}
    which: dict[str, tuple[str, str]] = {}
    for r in store._all("SELECT * FROM sessions"):
        wo = store.get_work_order(r["work_order_id"]) or {}
        which[r["devin_session_id"]] = ("wrote the fix", ref(nums.get(wo.get("ticket_id"))))
    for r in store.list_triage_sessions():
        which[r["devin_session_id"]] = ("scoped the ticket", ref(nums.get(r["ticket_id"])))
    for r in store.list_scan_sessions():
        which[r["devin_session_id"]] = ("read the repository", "none")

    # every session the loop started is a row, whether or not Devin's record has been fetched
    # yet: the ones without a record can still be asked for an analysis
    have = {r.get("session_id") for r in rows}
    listed = rows + [
        {"session_id": sid, "url": f"https://app.devin.ai/sessions/{sid}", "title": ""}
        for sid in started
        if sid not in have
    ]
    table = []
    for r in listed:
        sid = r.get("session_id") or ""
        did, tk = which.get(sid, ("", "none"))
        cl = (r.get("analysis") or {}).get("classification") or {}
        conf = cl.get("confidence")
        has = ins.written(r)
        if has:
            state = "view"
        elif sid in pending:
            state = "writing"
        elif store.get_setting(f"insights.empty.{sid}"):
            # asked before: Devin answered already_exists and wrote nothing
            state = "empty"
        else:
            state = "generate"
        table.append(
            {
                "sid": sid,
                "state": state,
                "open": has and view == sid,
                "viewUrl": url("/devin/insights", view=sid, panel=q.get("panel") or None),
                "generateUrl": f"/devin/insights/{sid}/generate",
                "category": (cl.get("category") or "").replace("_", " ") or "uncategorised",
                "id": sid[:12],
                "url": r.get("url", ""),
                "did": did or clip(r.get("title", ""), 40),
                "ref": tk,
                "size": (r.get("session_size") or "").upper() or "not sized",
                "big": (r.get("session_size") or "").lower() in ("l", "xl"),
                "you": r.get("num_user_messages") or 0,
                "devin": r.get("num_devin_messages") or 0,
                "tools": ", ".join((cl.get("tools_and_frameworks") or [])[:3]) or "none named",
                "sure": f"{float(conf) * 100:.0f}%" if conf is not None else "not given",
                "playbook": "yes" if r.get("playbook_id") else "none",
                "analysed": r.get("analysis_status") or "not analysed",
            }
        )

    n = len(rows)
    return {
        # what this loop started against what Devin's record holds for those sessions; rows
        # Devin's own listing added for other sessions do not count either way
        "count": sum(1 for sid in started if sid in have),
        "started": len(started),
        "missing": sum(1 for sid in started if sid not in have),
        "panels": [
            panel(
                "turns",
                "Ran on one message",
                t["one"],
                t["total"],
                "sessions were told what to do once and got on with it",
                PL["ok"][0],
            ),
            panel(
                "size",
                "Cut to the right size",
                right,
                n,
                "pieces Devin judged small enough for one session",
                PL["ok"][0],
            ),
            panel(
                "playbook",
                "Ran on a playbook",
                n - len(nopb),
                n,
                "sessions followed instructions we can edit and reuse",
                PL["ok"][0],
            ),
        ],
        "replies": t["replies"],
        "repliesMax": max((r["sessions"] for r in t["replies"]), default=1),
        "sizes": sizes,
        "sizeChart": charts.histogram([(d["label"], d["n"], d["too_big"]) for d in sizes], w=300),
        "tools": tools,
        "constants": ins.constants(rows),
        "showSize": len({(r.get("session_size") or "") for r in rows}) > 1,
        "noPlaybook": nopb,
        "advice": {
            **adv,
            "actions": [
                {
                    **g,
                    "entries": [
                        {**x, "view": url("/devin/insights", view=x["sid"])} for x in g["entries"]
                    ],
                }
                for g in adv["actions"]
            ],
            "issues": [
                {
                    **g,
                    "entries": [
                        {**x, "view": url("/devin/insights", view=x["sid"])} for x in g["entries"]
                    ],
                }
                for g in adv["issues"]
            ],
        },
        "adviceEmpty": not (adv["n_actions"] or adv["n_issues"]),
        "rows": table,
        "written": sum(1 for x in table if x["state"] == "view"),
        "modal": _insight_modal(table, listed, view, q),
        "writing": sorted(pending),
        "refreshUrl": url("/devin/insights", view=view or None, panel=q.get("panel") or None)
        if pending
        else "",
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
        else ("settings" if active == "settings" else "Devin")
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
            f"ACU spent · cap {per_label} per session" if sp["metered"] else _spent_caption(sp)
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
        "goLog": "/report?log=1",
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
def _fold_findings(store: Store, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Several scanner findings waiting on the same decision become one row that opens. A board
    with a hundred findings should say a hundred, not grow a hundred rows."""
    finds = [r for r in rows if (store.get_ticket(r["ticket"]) or {}).get("source") == "code_scan"]
    if len(finds) < 2:
        return rows
    rest = [r for r in rows if r not in finds]
    oldest = min(finds, key=lambda r: r["ageSort"])
    first = finds[0]
    group = {
        **first,
        "ref": str(len(finds)),
        "ticket": "",
        "src": "security scan",
        "what": f"{len(finds)} security findings · each is a question until a person confirms it",
        "hover": "the scanner reported these; a person names the rule each one breaks, or dismisses it",
        "age": oldest["age"],
        "ageSort": oldest["ageSort"],
        "go": url("/tickets-page"),
        "answerUrl": "",
        "mergeUrl": "",
        "dismissUrl": "",
        "group": finds,
    }
    at = rows.index(first)
    out = rest[:]
    out.insert(min(at, len(out)), group)
    return out


def home(store: Store, cfg: TargetConfig, q: dict[str, str]) -> dict[str, Any]:
    hide_sec = codescan.masked(store)
    h = pages.home(store)
    counts = h["counts"]
    tickets = store.list_tickets()
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
    # a fix is written when there is a pull request to read, whatever the session's last status
    with_pr = [x for x in sess if x.get("pull_request_url")]
    verified_sessions = [
        x for x in passed if (store.latest_verdict(x["id"]) or {}).get("review_severity")
    ]
    by_src = Counter(source_tag(t) for t in tickets)
    counts5 = [
        (
            len(tickets),
            f"{len(tickets)} filed · "
            + " · ".join(f"{n} {name}" for name, n in by_src.most_common())
            if by_src
            else "0 filed",
        ),
        (
            len(verdicts),
            f"{len(verdicts)} plan{'s' if len(verdicts) != 1 else ''} written · {len(to_devin)} given to the AI",
        ),
        (
            len(with_pr),
            f"{len(with_pr)} pull request{'s' if len(with_pr) != 1 else ''} opened",
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
    nums = {t["id"]: t.get("number") for t in store.list_tickets()}
    events = [ev(e, nums) for e in reversed(store.timeline(limit=40))]
    groups = _group_events(events, open_groups, raw_mode)
    on, off = ("#e2e9f6", "#2457a8"), ("transparent", "#8f97a3")
    b = store.budget_state()
    sp0 = cost.spend(store)
    # every kind of session counts as working: a ticket being scoped is Devin at work too
    working_now = counts["running"] + sum(
        1
        for x in store.list_triage_sessions() + store.list_scan_sessions()
        if x.get("devin_session_id") and not x.get("terminal_at")
    )
    starting = sum(
        1
        for x in sess
        if x.get("status") == "reserved"
        and not x.get("devin_session_id")
        and not x.get("terminal_at")
    )
    working_now += starting
    gate_n = len(passed)
    gate_total = sum(1 for x in sess if store.latest_verdict(x["id"]))
    verified = len(merged)
    blocking = [x for x in needs if x["kind"] != "ready to merge"]
    five = [
        {
            "n": str(working_now),
            "of": f"{starting} starting" if starting else "",
            "label": "AI sessions working now",
            "color": PL["run"][0] if working_now else FAINT,
            "pct": None,
            "live": bool(working_now),
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
                "label": "",
                "color": INK,
                "pct": None,
            }
        ),
        {
            "n": str(verified),
            "of": f"of {store.funnel()['tickets_with_session']} given to the AI",
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
        what = clip(codescan.safe_title(t, hide_sec) or "", 64)
        if n["kind"] == "ready to merge":
            mn = reduce_mod.merge_notes(store, tid)
            what = " · ".join(mn["reviews"]) + (
                f" · {len(mn['notes'])} note{'' if len(mn['notes']) == 1 else 's'}"
                if mn["notes"]
                else ""
            )
        short_needs.append(
            {**n, "what": what or clip(n["reason"], 64), "hover": plain(n["reason"])}
        )
    rng = q.get("range", "run")
    spark = _sparklines(store, rng)
    issue_no = {
        t["id"]: ("#" + t["external_ref"].rsplit("#", 1)[-1])
        for t in tickets
        if t.get("external_ref") and "#" in t["external_ref"]
    }
    # Sessions that ran without a person typing anything into them. The poller's own reply,
    # logged as "answered waiting_for_user from the work order", is the loop answering the
    # session from the work order it already had; counting it here would report the opposite
    # of what happened. Only a person's own words count: an answer typed on a ticket, or an
    # escalation a person resolved.
    tri = store.list_triage_sessions()
    tl = store.timeline(limit=2000)
    helped = {e["ticket_id"] for e in tl if e["event"] == "answered by a person"}
    helped |= {
        h["ticket_id"]
        for h in store._all("SELECT ticket_id FROM human_actions WHERE kind='resolve'")
    }
    all_sessions = (
        [("tri", x) for x in tri]
        + [("rep", x) for x in sess]
        + [("scn", x) for x in store.list_scan_sessions()]
    )
    quiet = 0
    wo_ticket = {w["id"]: w["ticket_id"] for t in tickets for w in store.work_orders_for(t["id"])}
    for kind, x in all_sessions:
        # a scan is not opened against a ticket, so nobody could have answered it; a repair
        # session's ticket is the one its work order belongs to
        tid = wo_ticket.get(x.get("work_order_id")) if kind == "rep" else x.get("ticket_id")
        if kind == "scn" or tid not in helped:
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
            "svg": spark[3]["svg"],
            "note": f"{spark[3]['last']} handed over in this span",
        },
        {**five[2], "svg": spark[1]["svg"], "note": spark[1]["span"]},
        {**five[3], "svg": spark[2]["svg"], "note": spark[2]["last"]},
        {
            "n": f"{quiet} of {len(all_sessions)}" if all_sessions else "0 of 0",
            "of": "sessions",
            "label": "fixes that needed no help",
            "color": PL["ok"][0] if quiet else FAINT,
            "pct": None,
            "svg": charts.bars([], [], PL["ok"][0]),
            "note": (f"{len(all_sessions) - quiet} needed an answer" if all_sessions else ""),
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
                "ticket": e["ticket_id"],
                "src": source_tag(t),
                "kind": KIND_PLAIN.get(e["kind"], e["kind"]),
                **pill("bad"),
                "what": clip(plain(codescan.safe_title(t, hide_sec) or e["reason"]), 70),
                "hover": plain(e["reason"]),
                "age": _age(e["created_at"]),
                "ageSort": e["created_at"] or "",
                "go": url("/tickets-page", open=e["ticket_id"]),
                "answerUrl": f"/tickets/{e['ticket_id']}/answer" if can_answer else "",
                "mergeUrl": "",
                "dismissUrl": f"/escalations/{e['id']}/resolve",
            }
        )
    inbox = _fold_findings(store, inbox)
    for tid in h["summary"]["ready"]:
        mn = reduce_mod.merge_notes(store, tid)
        inbox.append(
            {
                **dot(store, tid, False),
                "src": source_tag(store.get_ticket(tid)),
                "kind": KIND_PLAIN["ready to merge"],
                **pill("ok"),
                "what": plain(
                    " · ".join(mn["reviews"]).replace("no issues", "the reviewer found nothing")
                    + (
                        f" · {len(mn['notes'])} note{'' if len(mn['notes']) == 1 else 's'} for you"
                        if mn["notes"]
                        else ""
                    )
                )
                or "every check passed, and it was reviewed",
                "hover": plain(
                    next(
                        (
                            x["reason"]
                            for x in h["needs"]
                            if x["ticket_id"] == tid and x["kind"] == "ready to merge"
                        ),
                        "every check passed and the AI reviewer read it; merge on GitHub first, "
                        "then record it here",
                    )
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
                    "last": _said(
                        (store.timeline(ticket_id=x["ticket_id"], limit=1) or [{}])[0].get(
                            "event", ""
                        )
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
                "last": _said(
                    (store.timeline(session_id=x["id"], limit=1) or [{}])[0].get("event", "")
                ),
                "needsInput": x["status_detail"] in ("waiting_for_user", "waiting_for_approval")
                and not x.get("pull_request_url"),
                "go": url("/tickets-page", open=tid),
            }
        )
    for x in store.list_scan_sessions():
        if not x.get("devin_session_id") or x.get("terminal_at"):
            continue
        inflight.append(
            {
                "ref": "scan",
                "color": TEAL,
                "ticket": "",
                "stage": "reading the repository",
                "elapsed": ops._elapsed(x["created_at"], None),
                "acu": _fmt_acu(x.get("acus_consumed"))
                if sp_home["metered"]
                else _usd_or_min(store, x, "scn", kind_rates_home),
                "cap": "",
                "pct": "0",
                "last": _said(
                    (store.timeline(session_id=x["id"], limit=1) or [{}])[0].get("event", "")
                ),
                "needsInput": False,
                "go": x.get("url") or url("/devin/sessions"),
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
        "inboxCount": sum(len(n["group"]) if n.get("group") else 1 for n in inbox),
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
    asks: list[datetime] = []
    for e in store._all("SELECT created_at FROM escalations"):
        try:
            asks.append(datetime.fromisoformat(e["created_at"]))
        except (TypeError, ValueError):
            pass
    times = [t for t, _ in starts] + [t for t, _ in verdicts]
    if not times:
        empty = charts.bars([], [], PURPLE)
        return [
            {"label": k, "svg": empty, "span": "no sessions yet", "last": "0"}
            for k in (
                "sessions started",
                "ACU" if metered else "AI minutes",
                "checks passed",
                "handed to your team",
            )
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

    s_bins, a_bins, g_bins, e_bins = [0.0] * bins, [0.0] * bins, [0.0] * bins, [0.0] * bins
    fails = 0
    for t in asks:
        if lo <= t <= hi:
            e_bins[bucket(t)] += 1
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
        {
            "label": "handed to your team",
            "svg": charts.bars(e_bins, labels, PL["bad"][0], unit="handed over"),
            "span": span,
            "last": str(int(sum(e_bins))),
        },
    ]


def collapse(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A poll every thirty seconds is not news. Consecutive identical events become one line that
    says how many there were and how long they covered, so what changed stands out."""
    out: list[dict[str, Any]] = []
    for e in events:
        last = out[-1] if out else None
        same = (
            last
            and last["event"] == e["event"]
            and last["layer"] == e["layer"]
            and not e.get("detail")
            and not last.get("detail")
        )
        if same:
            last["n"] = last.get("n", 1) + 1
            last["until"] = e["time"]
            continue
        out.append({**e, "n": 1})
    for e in out:
        if e.get("n", 1) > 1:
            e["detail"] = f"{e['n']} checks, up to {e['until']}"
    return out


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
        "Found by a session",
        {"scan"},
        "A session reads the repository for one area of problem and files what it finds here; the loop takes it from there.",
    ),
    (
        "Found by Devin's scanner",
        {"code_scan"},
        "Devin's own code scan, started against the repository with an area to look in. Every security finding goes to a person: this repository requires such a finding to name the capability row in SECURITY.md it violates and the principal the attacker holds, and to be filed as a question when it cannot.",
    ),
]
VIEWS = [("list", "List"), ("pipeline", "Pipeline")]


PLAIN_PHRASES = [
    ("the oracle is read-only", "the tests are not ours to change"),
    ("oracle read-only to sessions", "the AI may not change tests"),
    ("sessions never edit tests or CI", "the AI never edits tests or the build"),
    ("Devin Review found no issues", "the AI reviewer found nothing"),
    ("Devin Review completed", "the AI reviewer finished"),
    ("Devin Review left", "the AI reviewer left"),
    ("Devin Review", "the AI reviewer"),
    ("triage: ", ""),
    ("site(s)", "places"),
    # text written by an older version of this app, still in the store
    ("ticket(s)", "tickets"),
    ("session(s)", "sessions"),
    ("finding(s)", "findings"),
    ("row(s)", "rows"),
    ("command(s)", "commands"),
    ("remark(s)", "comments"),
    ("comment(s)", "comments"),
    ("work order(s)", "pieces of work"),
    ("work order", "piece of work"),
    ("verdict accepted", "plan accepted"),
    ("verdict", "plan"),
    ("T0 ok", "scope check ok"),
    ("T1 ok", "command ok"),
]
# Short words that must match whole, or "investigate" becomes "inveschecks".
PLAIN_WORDS = [
    ("gate", "checks"),
    ("gated", "checked"),
    ("wo", "piece"),
    ("shard", "piece"),
    ("escalated", "handed to your team"),
    ("escalations", "handovers"),
    ("escalation", "handover"),
]
_WORD_RE = re.compile(r"\b(" + "|".join(w for w, _ in PLAIN_WORDS) + r")\b", re.IGNORECASE)
_WORD_MAP = dict(PLAIN_WORDS)


_ABS_PATH_RE = re.compile(r"(?<![:/\w])/(?:[A-Za-z0-9._@+-]+/){2,}[A-Za-z0-9._@+-]+")
_KEEP_SEGMENTS = 4


def shorten_paths(text: str) -> str:
    """Absolute paths reach the store from the machine the checks ran on. A reader needs the end
    of the path, not somebody's home directory and the folders above it."""

    def tail(m: re.Match[str]) -> str:
        parts = m.group(0).strip("/").split("/")
        if len(parts) <= _KEEP_SEGMENTS:
            return m.group(0)
        return ".../" + "/".join(parts[-_KEEP_SEGMENTS:])

    return _ABS_PATH_RE.sub(tail, text)


def plain(text: str) -> str:
    """Recorded reasons carry the vocabulary of the code that wrote them. Readers do not."""
    for a, b in PLAIN_PHRASES:
        text = text.replace(a, b)
    text = _WORD_RE.sub(lambda m: _WORD_MAP[m.group(1).lower()], text)
    text = text.replace("1 comments", "1 comment").replace("1 places", "1 place")
    return shorten_paths(text)


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
        review_txt = "the AI reviewer found nothing"
    elif "found" in review:
        n = review.split()[0]
        review_txt = f"the AI reviewer left {n} comment" + ("" if n == "1" else "s")
    else:
        review_txt = "the AI reviewer is " + review
    if st == "merged":
        return plain(f"Merged by your team. Every check passed on a clean copy; {review_txt}.")
    if st == "refused" or route == "refuse":
        # a refusal is a decision the loop already took, not one waiting on anybody
        why = t.get("router_reason") or "the loop set it aside"
        return plain(clip(why[0].upper() + why[1:] if why else why, 170))
    if st == "escalated" or (route and route != "devin"):
        # why it stopped, not why it was routed: a ticket the loop sent to a person after the
        # work began has an escalation that says what happened, and the routing note from
        # before that is no longer the reason it is sitting here
        why = (row.get("escalations") or [{}])[-1].get("reason") or t.get("router_reason") or ""
        # the names we gave the acceptance commands are ours, not something to read on a page
        why = re.sub(r"\b[a-z0-9]+(?:_[a-z0-9]+){2,}:\s*", "", why)
        return plain("Needs you: " + clip(why or "a person decides", 160))
    if st == "new":
        return (
            "A session is reading the issue now and deciding what the work is."
            if row.get("triageLive")
            else "Waiting for a session to read the issue."
        )
    if st in ("triaged", "routed"):
        tail = (
            "Starting the repair session; Devin is setting up its machine."
            if row.get("starting")
            else "Waiting for a repair session."
        )
        return f"Scoped by the AI: {row.get('count') or 'the sites are known'}. {tail}"
    if st in ("dispatched", "running"):
        el = (sd or {}).get("elapsed") or ""
        since = f", {el} so far" if el else ""
        return f"The AI is working on the fix{since}. Open the session to watch."
    if st in ("gated", "reviewed"):
        if verdict and verdict.get("gate_result") != "pass":
            return plain("Checks failed: " + clip(verdict.get("reason") or "see the log", 150))
        if row.get("ready"):
            return plain(
                f"Ready for you: every check passed on a clean copy, {review_txt}. "
                "Read the pull request and merge."
            )
        if review == "requested":
            return (
                "Every check passed on a clean copy. The AI reviewer is reading it now, and the "
                "pull request is held as a draft until it is done."
            )
        if "found" in review:
            return plain(
                f"Every check passed on a clean copy; {review_txt}. The remarks go back to the "
                "session, and the checks and the reviewer run again before this is ready."
            )
        return plain(f"Every check passed on a clean copy; {review_txt}. Waiting on your decision.")
    return row.get("note") or ""


def _neg_time(t: Any) -> str:
    """Sort a timestamp descending without parsing it."""
    return "".join(chr(0x10FFFD - ord(c)) if c.isdigit() else c for c in str(t or ""))


def _attention(t: dict[str, Any], row: dict[str, Any]) -> int:
    """Sort by what a person has to do about it: their turn first, the machine's turn next, and
    what is finished at the bottom."""
    st = t.get("status")
    if st == "merged":
        return 3
    if row.get("ready") or st == "escalated" or (t.get("router_decision") or "devin") != "devin":
        return 0
    if st in ("dispatched", "running", "gated", "reviewed"):
        return 1
    return 2


def tickets(store: Store, cfg: TargetConfig, q: dict[str, str], note: str = "") -> dict[str, Any]:
    tk_page = pages.tickets(store)
    sm = tk_page["summary"]
    info = {r["id"]: r for g in tk_page["groups"] for r in g["rows"]}
    view = q.get("view") if q.get("view") in dict(VIEWS) else "list"
    f = q.get("f", "all")
    open_ids = {x for x in (q.get("open") or "").split(",") if x}
    hide_sec = codescan.masked(store)
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
        "waitingColor": FAINT,
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
        # between the verdict and Devin answering POST /sessions the loop is asking for a
        # machine; that is work in progress, not a wait for a person
        r["starting"] = bool(sd and sd.get("status") == "reserved" and not sd.get("devin_id"))
        files = i.get("files") or []
        sites = i.get("sites") or 0
        # Nothing has been read yet, so the size of the work is not zero, it is unknown. A row
        # of noughts reads as a broken page rather than as a ticket nobody has opened.
        r["count"] = (
            f"{len(files)} file{'s' if len(files) != 1 else ''} · "
            f"{sites} site{'s' if sites != 1 else ''}"
            if files
            else (f"{sites} site{'s' if sites != 1 else ''}" if sites else "size not known yet")
        )
        r["summary"] = _summary(t, r)
        r["source"] = t.get("source") or ""
        # An unverified security finding does not put a file and a line on a shared screen.
        r["title"] = codescan.safe_title(t, hide_sec) or i.get("title") or r["id"]
        r["withheld"] = hide_sec and codescan.is_unverified_security(t)
        r["age"] = _age(t.get("created_at"))
        n_s = i.get("sessions", 0)
        r["nSessions"] = plural(n_s, "session") if n_s else "no session yet"
        tri = store.list_triage_sessions(r["id"])
        live_url = (sd or {}).get("url") or (tri[-1].get("url") if tri else None)
        r["sessionUrl"] = live_url or ""
        r["sessionLabel"] = (
            "open the session" if sd else ("open the scoping session" if live_url else "")
        )
        r["prLabel"] = f"PR #{r['pr']}" if r.get("pr") and r["pr"] != "none" else ""
        r["scope"] = _ticket_panel(store, r["id"], f, q) if r["open"] else None
        r["isRunning"] = t.get("status") in ("dispatched", "running")
        r["order"] = _attention(t, r)
        rows.append(r)
    rows.sort(key=lambda x: (x["order"], x["ref"]))
    groups = []
    placed: set[str] = set()
    for name, sources, empty_note in SOURCE_GROUPS:
        g_rows = [r for r in rows if r["source"] in sources]
        placed.update(r["id"] for r in g_rows)
        groups.append(
            {
                "name": name,
                "rows": g_rows,
                "note": empty_note if not g_rows else "",
                "empty": not g_rows,
            }
        )
    groups[0]["rows"].extend(r for r in rows if r["id"] not in placed)
    # A finding nobody has taken on is not a ticket. It is kept so the same place is not filed
    # twice, and so it can be picked up when whatever is in its way lands, but it does not
    # belong in a list of work: nothing is happening to it and nothing is being asked of anyone.
    for g in groups:
        aside = [r for r in g["rows"] if r["route"] == "refuse"]
        g["rows"] = [r for r in g["rows"] if r["route"] != "refuse"]
        g["aside"] = aside
        g["asideNote"] = (
            plural(len(aside), "finding") + " set aside until the file each one needs is free"
            if aside
            else ""
        )
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
        # what is actually on screen: the ones set aside are behind their own line
        "ticketCount": f"{sum(len(g['rows']) for g in groups)}"
        + ("" if len(rows) == len(tr["trackerRows"]) else f" of {len(tr['trackerRows'])}"),
        "noTickets": not rows,
        "emptyText": EMPTY.get(f, "No tickets."),
        "note": note,
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
            else {"label": "not routed yet", **pill("na")}
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
                "msg": clip(s.get("r3") or s.get("r2") or s.get("prescribed_fix") or "", 220),
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
        "siteNote": "Nothing scoped yet. The session reading the issue decides what the work is; "
        "until it answers, this is only the issue as it was filed."
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
        tri = store.list_triage_sessions(r["id"])
        tri_last = tri[-1] if tri else None
        tri_live = bool(tri_last and not tri_last["terminal_at"])
        is_open = r["id"] in open_ids
        others = (open_ids - {r["id"]}) if is_open else (open_ids | {r["id"]})
        st_kind = _pill_kind(r["pill"])
        evidence = [
            {
                "tier": e["tier"],
                "cmd": (e["command"] or "").split(": ", 1)[0]
                if e["tier"] == "T1"
                else shorten_paths(e["command"] or ""),
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
                "status": (
                    "starting"
                    if r.get("starting")
                    else (
                        "in review"
                        if r["status"] == "reviewed" and not r["ready"] and not r["merged"]
                        else STATUS_PLAIN.get(r["status"], r["status"])
                    )
                ),
                "stBg": PL["run" if r.get("starting") else st_kind][1],
                "stFg": PL["run" if r.get("starting") else st_kind][0],
                "note": r.get("last_event") or "",
                "open": is_open,
                "chev": "▲" if is_open else "▼",
                "toggle": url(base, **extra, open=",".join(sorted(others)) or None),
                "route": r["route"],
                "ready": bool(r["ready"]),
                "sd": sd,
                "verdict": verdict,
                "triageLive": tri_live,
                "hasTriage": bool(tri_last),
                "triage": {
                    "id": (tri_last["devin_session_id"] or "")[:12] if tri_last else "",
                    "url": (tri_last["url"] or "#") if tri_last else "#",
                    "state": (
                        "reading the issue now"
                        if tri_live
                        else plain(str((tri_last or {}).get("outcome") or "finished"))
                    ),
                    "elapsed": ops._elapsed(
                        tri_last["created_at"], tri_last["terminal_at"] if tri_last else None
                    )
                    if tri_last
                    else "",
                    "timeline": collapse(
                        [ev(e) for e in store.timeline(session_id=tri_last["id"], limit=200)]
                    )[-30:]
                    if tri_last
                    else [],
                }
                if tri_last
                else None,
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
                "timeline": collapse([ev(e) for e in (sd["timeline"] if sd else [])])[-40:],
                "evidence": evidence,
                "said": (
                    f"{'done' if claim.get('self_reported_done') else 'not done'} · tests {claim.get('tests_passed', 0)}/{claim.get('tests_run', 0)}"
                    + (
                        " · "
                        + plural(len(claim.get("needs_human") or []), "note")
                        + " for a person"
                        if claim.get("needs_human")
                        else ""
                    )
                )
                if claim
                else "no claim yet",
                "gate": verdict["gate_result"] if verdict else "pending",
                "decision": verdict["decision"] if verdict else "",
                "review": verdict.get("review_severity") or "not requested" if verdict else "",
                "gateNote": shorten_paths(verdict.get("reason") or "") if verdict else "",
                "isMerged": bool(r["merged"]),
                "isReady": bool(r["ready"]) and not r["merged"],
                "mergeUrl": f"/tickets/{r['id']}/merge-form",
                "readyNote": f"{r['readiness']['verified']}/{r['readiness']['shards']} shards verified · reviewed · {'no conflicts' if not r['readiness']['conflicts'] else str(len(r['readiness']['conflicts'])) + ' conflict(s)'}. Merge on GitHub first; this records it.",
                "isEscalated": r["status"] == "escalated"
                or bool(r["route"] and r["route"] != "devin"),
                "routeReason": r.get("reason") or "",
                "escalations": [
                    {
                        "kind": KIND_PLAIN.get(e["kind"], e["kind"].replace("_", " ")),
                        **pill("bad" if not e.get("resolved_at") else "na"),
                        "reason": shorten_paths(e["reason"] or ""),
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
        # a scan is a session and costs money like any other, so it is priced here too
        for r in store.list_scan_sessions():
            mins[r["id"]] = cost.scan_active_seconds(store, r) / 60.0
            usd_rows[r["id"]] = cost.session_usd(store, r, "scn", kind_rates)
    rows = []
    # still working first, then the most recent
    for s in sorted(
        ss["sessions"],
        key=lambda x: (
            1 if (x.get("outcome") or x.get("gate")) else 0,
            _neg_time(x.get("created")),
        ),
    ):
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
                    else _session_price(usd_rows.get(s["id"]), mins.get(s["id"], 0.0))
                ),
                "cap": (
                    f"{TRIAGE_ACU_CAP if is_triage else cap:.0f}"
                    if metered
                    else (
                        f"{mins.get(s['id'], 0.0):.0f} min"
                        if usd_rows.get(s["id"], (None,))[0] is not None
                        and mins.get(s["id"], 0.0) > 0
                        else ""
                    )
                ),
                "acuPct": _pct(s.get("acus"), TRIAGE_ACU_CAP if is_triage else cap)
                if metered
                else _pct(mins.get(s["id"], 0.0), max(list(mins.values()) or [1.0])),
                "did": {
                    "triage": "read the ticket and wrote the plan",
                    "scan": "read the repository and filed what it found",
                    "code_scan": "Devin's scanner read the repository",
                }.get(s.get("kind"), "wrote the fix and opened a pull request"),
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
                "gate": (
                    {
                        "triaged": "planned",
                        "invalid": "output rejected",
                        "no_output": "no answer",
                    }.get(s.get("outcome") or "", s.get("outcome") or "scoping")
                    if is_triage
                    else {
                        "pass": "passed",
                        "fail": "failed",
                        "missing_evidence": "not verified",
                    }.get(gate or "", gate or "not run")
                ),
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
                "open": (
                    url("/tickets-page", open=s["ticket"])
                    if is_triage
                    else ("" if s.get("kind") == "scan" else url("/devin/sessions", drawer=s["id"]))
                ),
            }
        )
    d = (
        _drawer(store, drawer_id, cap)
        if drawer_id
        else {"timeline": [], "evidence": [], "verdicts": []}
    )
    return {
        "now": _now(ss["counts"]),
        "perSession": f"{cap:.0f}",
        "managed": "in use" if ss.get("managed") else "not exercised in this run",
        "sessionRows": rows,
        "etaFoot": "Cost is what the console charged where a person entered it, otherwise our own "
        "measure of the minutes the AI was working. Click a row for the timeline, the checks and "
        "what the session claimed.",
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
        "cap": f"{cap:.0f}" if (s.get("acus_consumed") or 0) > 0 else "",
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
        "state": (
            "finished"
            if s.get("terminal_at")
            and (s.get("status_detail") or "") in ("finished", "waiting_for_user", "inactivity")
            else (
                f"stopped: {s.get('status_detail')}"
                if s.get("terminal_at")
                else f"{s.get('status') or 'new'}/{s.get('status_detail') or 'starting'}"
            )
        ),
        "decision": last["decision"] if last else "",
        "review": (last.get("review_severity") or "not requested") if last else "",
        "gateNote": shorten_paths(last.get("reason") or "") if last else "the gate has not run",
        "timeline": [ev(e) for e in det.get("timeline") or []][-60:],
        "evidence": [
            {
                "tier": e["tier"],
                "cmd": shorten_paths(e["command"] or ""),
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
                "reason": shorten_paths(v.get("reason") or ""),
            }
            for v in verdicts
        ],
        "output": json.dumps(out, indent=1) if out else "none",
    }


# ---------------------------------------------------------------------------- automations
def _took(a: str | None, b: str | None) -> str:
    """How long a run took. A row written by hand, or by an older version, may carry a timestamp
    without a timezone; that is not a reason for the page to fall over."""
    if not a or not b:
        return "running"
    from datetime import UTC, datetime

    try:
        lo, hi = datetime.fromisoformat(a), datetime.fromisoformat(b)
    except ValueError:
        return "unknown"
    if (lo.tzinfo is None) != (hi.tzinfo is None):
        lo = lo.replace(tzinfo=lo.tzinfo or UTC)
        hi = hi.replace(tzinfo=hi.tzinfo or UTC)
    secs = (hi - lo).total_seconds()
    if secs < 0:
        return "unknown"
    return f"{int(secs // 60)} min {int(secs % 60)} s" if secs >= 60 else f"{int(secs)} s"


def _run_kind(status: str) -> str:
    """The pill colour for a run: ours by outcome, Devin's own in Devin's colour."""
    if status == "running":
        return "run"
    if status == "failed":
        return "bad"
    if status == "observed":
        return "devin"
    return "ok"


def _live_run_line(store: Store, started_at: str) -> dict[str, str]:
    """What a run still going is doing right now: the session it is on, how long that session
    has been at it, and the last thing the log said about it. A running row that says "no
    result recorded" tells a reader nothing; this tells them where to look."""
    live: list[tuple[str, str, str, str]] = []  # (created_at, kind, id, url)
    for x in store.list_scan_sessions():
        if x.get("devin_session_id") and not x.get("terminal_at") and x["created_at"] >= started_at:
            live.append(
                (
                    x["created_at"],
                    "a scan session is reading the repository",
                    x["id"],
                    x.get("url") or "",
                )
            )
    for x in store.list_triage_sessions():
        if x.get("devin_session_id") and not x.get("terminal_at") and x["created_at"] >= started_at:
            live.append(
                (x["created_at"], "a session is scoping a ticket", x["id"], x.get("url") or "")
            )
    for x in store.live_sessions():
        if x["created_at"] >= started_at:
            live.append(
                (x["created_at"], "a session is writing a fix", x["id"], x.get("url") or "")
            )
    if not live:
        last = (store.timeline(limit=1) or [{}])[0]
        return {"line": _said(last.get("event", "")) or "starting", "url": ""}
    created, what, sid, url_ = max(live)
    last = (store.timeline(session_id=sid, limit=1) or [{}])[0]
    said = _said(last.get("event", ""))
    return {
        "line": f"{what} · {ops._elapsed(created, None)}" + (f" · {said}" if said else ""),
        "url": url_,
    }


def _run_line(res: dict[str, Any]) -> str:
    if not res:
        return "no result recorded"
    if res.get("error"):
        return f"failed: {res['error']}"
    if res.get("started_by") == "Devin's schedule" and "orchestrator" in res:
        # a run Devin's schedule made on its own, seen after the fact
        return f"Devin's schedule · {plural(res.get('sessions', 1), 'session')}" + (
            f" · {res['title']}" if res.get("title") else ""
        )
    if res.get("scan") == "ran, nothing new":
        return f"the schedule ran ({res.get('scheduled_runs', 1)}), nothing new to file"
    if res.get("started_by") == "a new issue on GitHub":
        n_new = len(res.get("new_tickets") or [])
        return f"a new issue on GitHub · {plural(n_new, 'ticket')} filed, nothing started"
    parts = []
    if "issues" in res:
        n_new = len(res.get("new_tickets") or [])
        parts.append(f"{plural(res['issues'], 'issue')} found, {plural(n_new, 'new ticket')}")
    if "triaged" in res:
        parts.append(f"{plural(res['triaged'], 'ticket')} scoped")
    if "dispatched" in res:
        parts.append(f"{plural(res['dispatched'], 'fix', 'fixes')} started")
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
    open_ids = {x for x in (q.get("open") or "").split(",") if x}
    a = pages.automations(store, cfg, settings, client, running)
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
            how = (
                "on click, or when a new labelled issue appears; looked for every 2 min"
                if a["live"]
                else "on click, by webhook"
            ) + (f", {r['schedule']}" if r.get("schedule") else "")
        elif src == "github":
            trig = f"{t.get('event', '')} on {r['target']}" + (
                " · " + ", ".join(f"{k}={v}" for k, v in (t.get("match") or {}).items())
                if t.get("match")
                else ""
            )
            how = "by webhook"
        elif src == "schedule":
            trig = (
                f"Devin's own scanner reads {r['target']} and reports what it finds"
                if r["kind"] == "code_scan"
                else f"reads {r['target']} itself for {cfg.scan.get('area', 'the area in the config')} and files what it finds"
            )
            if r.get("devin_automation_id"):
                # Devin holds the recurrence; ours is the button that runs it out of turn
                how = f"on click, and {r['schedule']} on a schedule Devin runs"
            else:
                how = "on click" + (
                    f"; {r['schedule']} once a schedule is registered" if r.get("schedule") else ""
                )
        elif src == "manual":
            trig = "on click only"
            how = "the Run button"
        else:
            trig = f"{src}:{t.get('event', '')}"
            how = "by webhook"
        runs = store.list_automation_runs(r["id"], 8)
        kind_label = {
            "repair": "event-based · default",
            "scan": "finds the work itself",
            "code_scan": "Devin's own scanner · security only here",
            "custom": "event-based",
        }.get(r["kind"], r["kind"])
        autos.append(
            {
                "id": r["id"],
                "name": r["name"],
                "kindLabel": kind_label,
                # ours says whether this app may run it; Devin's schedule is its own switch,
                # shown and moved in the opened row where its live state is read
                "state": "next version"
                if is_next
                else ("running" if r["running"] else ("on" if r["enabled"] else "off")),
                "stBg": PL[state_kind][1],
                "stFg": PL[state_kind][0],
                "trigger": trig,
                "how": how,
                # a code scan runs on Devin's side: it has no playbook of ours and no ACU
                # ceiling we set, so the row says what it is instead of an empty parameter
                "playbook": r["playbook"] or "none",
                "playbookLabel": "runs" if r["kind"] == "code_scan" else "playbook",
                "limit": (
                    f"{int(r['max_acu'])} ACU"
                    if r.get("max_acu")
                    else ("set on Devin's side" if r["kind"] == "code_scan" else "none")
                ),
                "lastRun": (
                    f"last run {r['last_run'][:16].replace('T', ' ')}"
                    if r.get("last_run")
                    else "never run"
                ),
                "cap": f"at most {r['max_findings']} tickets a run"
                if r["kind"] == "scan" and r.get("max_findings")
                else "",
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
                "scopeUrl": f"/automations/{r['id']}/run?stop_after=scoping",
                "runLabel": "Running…" if r["running"] else "Run",
                "removable": r["kind"] in ("custom", "scan") and r["id"] != "auto_scan",
                "removeUrl": f"/automations/{r['id']}/delete",
                "desc": r.get("kind_note") or "",
                "rows": [
                    {"k": "what starts it", "v": trig, "mono": True},
                    {"k": "how it runs", "v": how, "mono": False},
                    {"k": "playbook", "v": r["playbook"] or "none", "mono": True},
                    *(
                        [
                            {
                                "k": "tickets per run",
                                "v": f"at most {r['max_findings']}",
                                "mono": False,
                            }
                        ]
                        if r["kind"] == "scan" and r.get("max_findings")
                        else []
                    ),
                    {
                        "k": "per session",
                        "v": f"{int(r['max_acu'])} ACU" if r.get("max_acu") else "Devin's default",
                        "mono": False,
                    },
                    {
                        "k": "on the Devin org",
                        "v": "",
                        "lazy": url("/automations/native", name=r["id"]) if a["live"] else "",
                        "mono": False,
                    },
                ],
                "runs": [
                    {
                        "when": run["started_at"][:16].replace("T", " "),
                        "took": _took(run["started_at"], run.get("finished_at")),
                        **(
                            _live_run_line(store, run["started_at"])
                            if run["status"] == "running"
                            else {"line": _run_line(run["result"]), "url": ""}
                        ),
                        **pill(_run_kind(run["status"])),
                        "status": "Devin's" if run["status"] == "observed" else run["status"],
                    }
                    for run in runs
                ],
                "hasRuns": bool(runs),
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
        + (
            ""
            if a["live"]
            else " · replay: sessions are played back from a recording, so the checks do not re-run"
        ),
        "cap": a["cap"],
        "playbookNames": a["playbook_names"],
        "triggerChoices": a["trigger_choices"],
        "mono": mono,
    }


# ---------------------------------------------------------------------------- playbooks
def _code(text: str) -> Markup:
    """A playbook is written in markdown, so show its backticked spans as code rather than
    leaving the backticks on the page."""
    return Markup(re.sub(r"`([^`]+)`", r"<code>\1</code>", escape(text)))


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
        elif raw[:1] in (" ", "\t") and cur["items"]:
            cur["items"][-1] += " " + line.strip()  # a wrapped line of the item above
        elif cur["paras"] and not cur["items"] and not cur["paras"][-1].endswith("."):
            cur["paras"][-1] += " " + line.strip()
        else:
            cur["paras"].append(line.strip())
    for s in out:
        s["ordered"] = bool(s["ordered"] and s["items"])
        s["unordered"] = bool(not s["ordered"] and s["items"])
        s["paras"] = [_code(x) for x in s["paras"]]
        s["items"] = [_code(x) for x in s["items"]]
    return out


def playbooks(
    store: Store,
    cfg: TargetConfig,
    client: Any,
    q: dict[str, str],
    err: bool = False,
    name: str = "",
) -> dict[str, Any]:
    """The instructions each kind of session follows, laid out like the automations that use
    them: a list you add to, and a row that opens on what it says."""
    p = pages.playbooks(store, cfg, client)
    open_ids = {x for x in (q.get("open") or "").split(",") if x}
    add_open = q.get("add") == "1" or err
    schema_for, out_for = q.get("schema") or "", q.get("out") or ""
    rows = []
    for r in p["rows"]:
        is_next = r["availability"] == "next"
        actor = "next" if is_next else "devin"
        is_open = r["id"] in open_ids
        others = (open_ids - {r["id"]}) if is_open else (open_ids | {r["id"]})
        d = pages.playbook_detail(store, r["id"]) or {}
        fields = d.get("schema_fields") or []
        keep = {"open": ",".join(sorted(others)) or None}
        rows.append(
            {
                "id": r["id"],
                "title": r["title"],
                "slug": r["name"],
                "chip": "next version" if is_next else r["agent"],
                "chipBg": ACT[actor][1],
                "chipFg": ACT[actor][0],
                "meta": f"{len(r['sections'])} sections · used by {r['used_by']}"
                + (
                    f" · Devin's limit {int(r['max_acu'])} ACU per session"
                    if r.get("max_acu")
                    else ""
                ),
                "usedBy": r["used_by"],
                "usedGo": r["used_by_link"],
                "opacity": ".72" if is_next else "1",
                "open": is_open,
                "chev": "▲" if is_open else "▼",
                "bg": "#faf9f6" if is_open else "#fff",
                "toggle": url("/devin/playbooks", **keep),
                "isNext": is_next,
                "nextNote": (
                    (
                        d.get("body", "").split("## Overview", 1)[-1].split("##", 1)[0].strip()
                        if "## Overview" in d.get("body", "")
                        else d.get("body", "")[:400]
                    )
                    + " It runs when the Scan agent automation is switched on; nothing runs before that."
                )
                if is_next
                else "",
                "sections": _sections(d.get("body", "")) if not is_next else [],
                "fields": " · ".join(fields),
                "schemaOpen": schema_for == r["id"],
                "schemaLabel": (
                    "hide the shape" if schema_for == r["id"] else "the shape its answer must have"
                )
                + (f" · {len(fields)} fields" if fields else ""),
                "toggleSchema": url(
                    "/devin/playbooks",
                    open=",".join(sorted(open_ids | {r["id"]})),
                    schema=None if schema_for == r["id"] else r["id"],
                    out=out_for or None,
                ),
                "schemaJson": json.dumps(d.get("schema"), indent=1)
                if d.get("schema")
                else "no shape recorded",
                "outOpen": out_for == r["id"],
                "outLabel": "hide the last answer"
                if out_for == r["id"]
                else "the last answer a session gave",
                "toggleOut": url(
                    "/devin/playbooks",
                    open=",".join(sorted(open_ids | {r["id"]})),
                    out=None if out_for == r["id"] else r["id"],
                    schema=schema_for or None,
                ),
                "outJson": json.dumps(d.get("last_output"), indent=1)
                if d.get("last_output")
                else "no session has answered against this shape yet",
            }
        )
    return {
        "pbRows": rows,
        "addOpen": add_open,
        "addUrl": url(
            "/devin/playbooks",
            add=None if add_open else "1",
            open=",".join(sorted(open_ids)) or None,
        ),
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
        "gate passed": "passed our checks",
        "human-merged": "merged by your team",
        "to your team, or waiting": "to your team, or waiting",
        "not passed yet": "not passed yet",
        "waiting for a merge": "waiting for a merge",
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


METRICS: list[tuple[str, str, str, str]] = [
    # key, label, unit, colour
    ("prs", "Pull requests opened", "", PURPLE),
    ("merged", "Pull requests merged", "", PL["person"][0]),
    ("cost", "Cost, dollars", "$", INK),
    ("sessions", "Sessions started", "", PURPLE),
    ("tickets", "Tickets filed", "", "#5b6f8a"),
    ("passed", "Checks passed", "", TEAL),
    ("handed", "Handed to your team", "", PL["bad"][0]),
]
METRIC_RANGES: list[tuple[str, str]] = [("1d", "last day"), ("7d", "7 days"), ("30d", "30 days")]


def _metric_points(store: Store, key: str) -> list[tuple[datetime, float]]:
    """Every event of one kind with its time and weight, from the store's own tables."""

    def t(iso: str | None) -> datetime | None:
        try:
            return datetime.fromisoformat(iso) if iso else None
        except (TypeError, ValueError):
            return None

    pts: list[tuple[datetime, float]] = []
    if key == "tickets":
        pts = [(x, 1.0) for r in store.list_tickets() if (x := t(r["created_at"]))]
    elif key == "sessions":
        rows = (
            store._all("SELECT created_at FROM sessions")
            + store.list_triage_sessions()
            + store.list_scan_sessions()
        )
        pts = [(x, 1.0) for r in rows if (x := t(r["created_at"]))]
    elif key == "prs":
        # the moment this app first saw the pull request: its first check, else the session's end
        for r in store._all("SELECT * FROM sessions WHERE pull_request_url IS NOT NULL"):
            first = store._all(
                "SELECT MIN(created_at) AS at FROM verdicts WHERE session_id=?", r["id"]
            )
            when = (first[0]["at"] if first else None) or r["terminal_at"] or r["created_at"]
            if x := t(when):
                pts.append((x, 1.0))
    elif key == "merged":
        pts = [
            (x, 1.0)
            for r in store._all("SELECT at FROM human_actions WHERE kind='merge'")
            if (x := t(r["at"]))
        ]
    elif key == "passed":
        pts = [
            (x, 1.0)
            for r in store._all("SELECT created_at FROM verdicts WHERE gate_result='pass'")
            if (x := t(r["created_at"]))
        ]
    elif key == "handed":
        pts = [
            (x, 1.0)
            for r in store._all("SELECT created_at FROM escalations")
            if (x := t(r["created_at"]))
        ]
    elif key == "cost":
        kr = cost.rates(store)
        for kind, rows in (
            ("rep", store._all("SELECT * FROM sessions")),
            ("tri", store.list_triage_sessions()),
            ("scn", store.list_scan_sessions()),
        ):
            for r in rows:
                if x := t(r["created_at"]):
                    pts.append((x, cost.session_usd(store, r, kind, kr)[0]))
    return pts


def metric_series(store: Store, key: str, rng: str) -> dict[str, Any]:
    """One metric against time for the Report: hourly bins over the last day, six-hour bins over
    seven days, daily bins over thirty. The total in the range sits beside the picker."""
    from datetime import timedelta

    key = key if key in {m[0] for m in METRICS} else METRICS[0][0]
    rng = rng if rng in {r[0] for r in METRIC_RANGES} else "7d"
    _, label, unit, color = next(m for m in METRICS if m[0] == key)
    now_ = datetime.now(UTC)
    if rng == "1d":
        lo, bins, step, fmt = now_ - timedelta(hours=24), 24, timedelta(hours=1), "%d %b %H:%M"
    elif rng == "30d":
        lo, bins, step, fmt = now_ - timedelta(days=30), 30, timedelta(days=1), "%d %b"
    else:
        lo, bins, step, fmt = now_ - timedelta(days=7), 28, timedelta(hours=6), "%d %b %H:%M"
    vals = [0.0] * bins
    total = 0.0
    for when, weight in _metric_points(store, key):
        if when < lo or when > now_:
            continue
        i = min(bins - 1, int((when - lo) / step))
        vals[i] += weight
        total += weight
    labels = [(lo + step * i).strftime(fmt) for i in range(bins)]
    shown = f"${total:.2f}" if unit == "$" else f"{int(total)}"
    return {
        "key": key,
        "range": rng,
        "label": label,
        "unit": unit,
        "svg": charts.series(vals, labels, color, unit=unit),
        "total": shown,
        "caption": f"{shown} in the {dict(METRIC_RANGES)[rng]}"
        + (" · by the session's start" if key == "cost" else ""),
        "empty": total == 0,
    }


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
    # the three headline rates are the number and nothing else until a person asks for the
    # working, which is what "panel" holds
    open_panels = {x for x in (q.get("panel") or "").split(",") if x}

    metric = q.get("metric") or METRICS[0][0]
    mrange = q.get("range") or "7d"

    def keep(**extra: Any) -> dict[str, Any]:
        """Everything else on the page stays where it was when one panel is opened."""
        return {
            "checks": "1" if checks_open else None,
            "log": "1" if log_open else None,
            "lt": log_ticket or None,
            "panel": ",".join(sorted(open_panels)) or None,
            "metric": metric if metric != METRICS[0][0] else None,
            "range": mrange if mrange != "7d" else None,
            **extra,
        }

    series_ = metric_series(store, metric, mrange)

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
        is_open = key in open_panels
        return {
            "key": key,
            "title": title,
            "n": str(n),
            "of": str(of) if of else "0",
            "unit": unit,
            "pct": _pct(n, of) if of else "0",
            "open": is_open,
            "toggle": url(
                "/report",
                **keep(
                    panel=",".join(sorted(open_panels - {key} if is_open else open_panels | {key}))
                    or None
                ),
            ),
            "color": colour if of and n == of else (INK if of else FAINT),
            "rows": [
                {
                    "label": r["label"],
                    "n": f"{r['n']} of {r.get('of', of)}" if r.get("of", of) else str(r["n"]),
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
            "Verification pass rate",
            ver["changes_passed"],
            ver["changes"],
            "changes the AI wrote passed every check",
            ver["kinds"],
            _bound(ver["changes_passed"], ver["changes"]),
            PL["ok"][0],
        ),
        card(
            "hands-off",
            "Human intervention rate",
            inter["untouched"],
            inter["tickets"],
            "jobs needed nobody, up to the merge",
            inter["rows"],
            "merging is yours by design and is not counted as stepping in",
            PL["gate"][0],
        ),
        card(
            "accepted",
            "Acceptance rate",
            acc["merged"],
            acc["offered"],
            "changes offered were merged, not rejected",
            acc["rows"],
            "read from the pull requests themselves, so one you close shows up here. "
            + (
                f"{acc['mergers']} {'person' if acc['mergers'] == 1 else 'people'} merged them"
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

    nums = {t["id"]: t.get("number") for t in store.list_tickets()}
    events = collapse(
        [ev(e, nums) for e in reversed(store.timeline(ticket_id=log_ticket or None, limit=400))]
    )
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
        "metric": {
            **series_,
            "options": [
                {"key": k, "label": lbl, "on": k == series_["key"]} for k, lbl, _u, _c in METRICS
            ],
            "ranges": [
                {
                    "key": k,
                    "label": lbl,
                    "on": k == series_["range"],
                    "href": url("/report", **keep(range=k)),
                }
                for k, lbl in METRIC_RANGES
            ],
            # the picker is a select; htmx sends its value as `metric` and these ride along
            "vals": json.dumps({k: v for k, v in keep(metric=None).items() if v is not None}),
        },
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
        "systemMetrics": rates.system(store),
        "checksLine": f"{sum(1 for r in receipts if r['passed'])} of {len(receipts)} checks passed, "
        f"each re-run by this app on a clean copy of the change",
        "checksOpen": checks_open,
        "checksToggle": url("/report", **keep(checks=None if checks_open else "1")),
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
        "logToggle": url("/report", **keep(log=None if log_open else "1")),
        "logToggleLabel": "show less" if log_open else "show the whole log",
        "logTickets": log_tickets,
        "logAll": url("/report", **keep(lt=None)),
        "logFilter": [
            {
                "label": t["ref"],
                "on": t["on"],
                "set": url("/report", **keep(lt=None if t["on"] else t["id"])),
            }
            for t in log_tickets
        ],
        "notMeasured": [
            ("does the fix still hold after 30 days", "the window has not passed"),
            ("minutes your engineers spent reviewing", "not instrumented in this run"),
            ("security findings", "no scanner runs in this loop"),
            ("continuous integration results", "the fork runs none"),
            (
                "what a session cost after this app stopped watching it",
                (
                    "cost is measured from our own polls, so a session nobody polled to the "
                    "end counts as less than it was. The figure is a floor, not a total."
                ),
            ),
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
