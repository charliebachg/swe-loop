"""One builder per page. Thin: every number comes from the store, the timeline, the seam, or
the files on disk. Nothing here calls Devin except the read-only listings in live mode."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from swe_loop import connect, ops
from swe_loop import reduce as reduce_mod
from swe_loop.config import Settings, TargetConfig
from swe_loop.devin import DevinClient
from swe_loop.store import Store
from swe_loop.triage import TRIAGE_ACU_CAP

NAV = [
    ("home", "Home", "/"),
    ("automations", "Automations", "/automations"),
    ("tickets", "Tickets", "/tickets-page"),
    ("report", "Report", "/report"),
]
NAV_DEVIN = [
    ("sessions", "Sessions", "/devin/sessions", ""),
    ("playbooks", "Playbooks", "/devin/playbooks", ""),
    ("knowledge", "Knowledge", "/devin/knowledge", ""),
    ("insights", "Insights", "/devin/insights", ""),
    ("review", "Review", "/devin/review", ""),
    ("integrations", "Integrations", "/devin/integrations", ""),
    ("next", "Next", "/devin/next", "next"),
]


def shell(settings: Settings, cfg: TargetConfig, store: Store, active: str) -> dict[str, Any]:
    b = store.budget_state()
    return {
        "nav": NAV,
        "nav_devin": NAV_DEVIN,
        "active": active,
        "mode": settings.mode,
        "target": cfg.repo,
        "target_name": cfg.name,
        "branch": cfg.base_branch,
        "spent": round(b.get("spent") or 0, 1),
        "cap": b.get("cap"),
    }


def _owner_link(e: dict[str, Any]) -> str:
    if e.get("session_id"):
        return f"/devin/sessions#{e['session_id']}"
    if e.get("ticket_id"):
        return f"/tickets-page?open={e['ticket_id']}"
    return "/tickets-page"


def home(store: Store) -> dict[str, Any]:
    o = ops.build(store)
    reduce_mod.detect_conflicts(store)
    summary = reduce_mod.summary(store)
    needs = []
    for e in store.list_escalations():
        needs.append(
            {
                "kind": e["kind"],
                "ticket_id": e["ticket_id"],
                "reason": e["reason"],
                "since": e["created_at"],
                "link": f"/tickets-page?open={e['ticket_id']}",
                "action": "a person decides",
            }
        )
    for tid in summary["ready"]:
        mn = reduce_mod.merge_notes(store, tid)
        reason = "every shard passed the gate; no conflicts"
        if mn["reviews"]:
            reason += "; Devin Review " + ", ".join(mn["reviews"])
        if mn["notes"]:
            n_notes = len(mn["notes"])
            reason += (
                f"; read first: the session left {n_notes} note{'' if n_notes == 1 else 's'}: "
            ) + " | ".join(f"{n['site']}: {n['reason'][:90]}" for n in mn["notes"][:2])
        needs.append(
            {
                "kind": "ready to merge",
                "ticket_id": tid,
                "reason": reason,
                "since": "",
                "link": f"/tickets-page?open={tid}",
                "action": "a person merges",
            }
        )
    recent = [{**e, "link": _owner_link(e)} for e in reversed(store.timeline(limit=25))]
    return {"counts": o["counts"], "needs": needs, "recent": recent, "summary": summary}


def settings_page(
    settings: Settings,
    cfg: TargetConfig,
    store: Store,
    client: DevinClient | None,
    with_checks: bool = True,
) -> dict[str, Any]:
    checks = connect.run_checks(settings, cfg, store, client) if with_checks else []
    return {
        "checks": checks,
        "all_ok": all(c.ok for c in checks),
        "seam": {
            "path": str(settings.config_path),
            "repo": cfg.repo,
            "upstream": cfg.upstream,
            "base_branch": cfg.base_branch,
            "trigger": cfg.trigger,
            "forbidden": cfg.forbidden_paths,
            "coverage": cfg.router.get("coverage_100_paths", []),
            "human_only": cfg.router.get("human_only_classes", []),
            "caps": {
                "files": cfg.router.get("max_files_per_shard"),
                "sites": cfg.router.get("max_call_sites_per_shard"),
                "acu": cfg.max_acu_limit,
            },
            "version_range": cfg.session.get("version_range"),
        },
        "budget": store.budget_state(),
    }


# ---------------------------------------------------------------------------- tickets
SOURCE_LABELS = {
    "github": (
        "Issues on the fork",
        "filed on the repository, read back through the intake adapter",
    ),
    "inventory": ("Issues on the fork", "filed from the measured inventory"),
    "manual": ("Posted directly", "replay and simulation"),
    "scan": ("Scan", "found by a scan session; next"),
    "gmail": ("Gmail", "next"),
    "slack": ("Slack", "next"),
}
SOURCE_ORDER = ["github", "inventory", "manual", "scan", "gmail", "slack"]
TICKET_PILL = ops.TICKET_PILL


def _issue(t: dict[str, Any]) -> tuple[str | None, str | None]:
    ref = t.get("external_ref") or ""
    if "#" in ref and "/" in ref:
        repo, num = ref.rsplit("#", 1)
        return num, f"https://github.com/{repo}/issues/{num}"
    return None, None


def _ticket_row(store: Store, t: dict[str, Any]) -> dict[str, Any]:
    wos = store.work_orders_for(t["id"])
    files = sorted({f for w in wos for f in w["files"]})
    sites = 0
    verdict = json.loads(t["triage_verdict_json"]) if t.get("triage_verdict_json") else {}
    sites = len(verdict.get("sites", [])) or sum(len(w["files"]) for w in wos)
    n_sessions = sum(len(store.sessions_for(w["id"])) for w in wos) + len(
        store.list_triage_sessions(t["id"])
    )
    num, url = _issue(t)
    return {
        "id": t["id"],
        "number": t.get("number"),
        "issue": num,
        "issue_url": url,
        "title": t["title"],
        "classes": [c for c in (t["class"] or "").split(",") if c],
        "files": files,
        "sites": sites,
        "route": t["router_decision"],
        "reason": t["router_reason"] or "",
        "status": t["status"],
        "pill": TICKET_PILL.get(t["status"], "p-na"),
        "sessions": n_sessions,
        "source": t["source"],
    }


def ticket_detail(store: Store, tid: str) -> dict[str, Any] | None:
    t = store.get_ticket(tid)
    if not t:
        return None
    row = _ticket_row(store, t)
    verdict = json.loads(t["triage_verdict_json"]) if t.get("triage_verdict_json") else {}
    wos = store.work_orders_for(tid)
    acceptance = {}
    for w in wos:
        acceptance.update(w["acceptance"])
    if not acceptance and isinstance(verdict.get("acceptance_cmd"), dict):
        acceptance = verdict["acceptance_cmd"]
    return {
        **row,
        "sites": verdict.get("sites", []),
        "acceptance": acceptance,
        "work_orders": wos,
        "escalations": [
            e for e in store.list_escalations(unresolved_only=False) if e["ticket_id"] == tid
        ],
        "timeline": store.timeline(ticket_id=tid, limit=40),
        "review": verdict.get("review"),
        "needs_human": verdict.get("needs_human"),
    }


def tickets(store: Store) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for t in store.list_tickets():
        groups.setdefault(t["source"], []).append(_ticket_row(store, t))
    ordered = []
    seen = set()
    for src in SOURCE_ORDER + sorted(set(groups) - set(SOURCE_ORDER)):
        if src in seen:
            continue
        seen.add(src)
        label, note = SOURCE_LABELS.get(src, (src, ""))
        rows = groups.get(src, [])
        if rows or src in ("scan", "gmail", "slack"):
            ordered.append(
                {
                    "key": src,
                    "label": label,
                    "note": note,
                    "rows": rows,
                    "next": src in ("scan", "gmail", "slack"),
                }
            )
    # merge the two v0 sources under one heading
    merged: list[dict[str, Any]] = []
    for g in ordered:
        if (
            g["label"] == "Issues on the fork"
            and merged
            and merged[-1]["label"] == "Issues on the fork"
        ):
            merged[-1]["rows"] += g["rows"]
        else:
            merged.append(g)
    total = sum(len(g["rows"]) for g in merged)
    allrows = [r for g in merged for r in g["rows"]]
    summary = {
        "total": total,
        "devin": sum(1 for r in allrows if r["route"] == "devin"),
        "human": sum(1 for r in allrows if r["route"] in ("human_only", "refuse")),
        "pending": sum(1 for r in allrows if not r["route"]),
        "merged": sum(1 for r in allrows if r["status"] == "merged"),
        "active": sum(1 for r in allrows if r["status"] in ("dispatched", "running", "gated")),
        "sites": sum(r["sites"] for r in allrows),
    }
    return {"groups": merged, "total": total, "summary": summary}


# ---------------------------------------------------------------------------- tracker
STAGES = ops.STEPS
ACTOR = {
    "code": "code",
    "devin": "devin",
    "gate": "gate",
    "human": "person",
}


def _ticket_steps(
    store: Store, t: dict[str, Any], wos: list[dict[str, Any]], merged: bool
) -> list[dict[str, Any]]:
    """Ticket-level pipeline strip: the furthest any of its sessions got, plus the human-only case."""
    done: set[str] = {"intake"}
    now = None
    bad = None
    if t.get("triage_verdict_json"):
        done.add("triage")
    if t.get("router_decision"):
        done.add("route")
    if t.get("router_decision") in ("human_only", "refuse"):
        bad = "dispatch"
    sessions = [s for w in wos for s in store.sessions_for(w["id"])]
    if any(s["devin_session_id"] for s in sessions):
        done.add("dispatch")
    finished = [
        s
        for s in sessions
        if s["terminal_at"] and s["status"] == "exit" and s["status_detail"] == "finished"
    ]
    dead = [
        s
        for s in sessions
        if s["terminal_at"] and not (s["status"] == "exit" and s["status_detail"] == "finished")
    ]
    live = [s for s in sessions if s["devin_session_id"] and not s["terminal_at"]]
    if finished:
        done.add("session")
    elif dead and not live:
        bad = "session"
    elif live:
        now = "session"
    verdicts = [store.latest_verdict(s["id"]) for s in sessions]
    verdicts = [v for v in verdicts if v]
    if any(v["gate_result"] == "pass" for v in verdicts):
        done.add("gate")
        if any(
            (v.get("review_severity") or "").startswith(("requested", "completed"))
            for v in verdicts
        ):
            done.add("review")
    elif verdicts and not live:
        bad = "gate"
    elif "session" in done and not now:
        now = "gate"
    if merged:
        done.add("merge")
    elif "review" in done and not now and not bad:
        now = "merge"
    out = []
    for name, kind in STAGES:
        cls = kind
        if name in done:
            cls += " done"
        elif name == bad:
            cls += " bad"
        elif name == now:
            cls += " now"
        out.append({"name": name, "cls": cls, "actor": ACTOR.get(kind, "code")})
    return out


def tracker(store: Store) -> dict[str, Any]:
    reduce_mod.detect_conflicts(store)
    merged_set = {
        r["ticket_id"]
        for r in store._all("SELECT DISTINCT ticket_id FROM human_actions WHERE kind='merge'")
    }
    rows = []
    for t in store.list_tickets():
        wos = store.work_orders_for(t["id"])
        merged = t["id"] in merged_set
        ready = reduce_mod.readiness(store, t["id"])
        sessions = []
        for w in wos:
            for s in store.sessions_for(w["id"]):
                v = store.latest_verdict(s["id"])
                ev = store.evidence_for(s["id"])
                latest_tree = ev[-1]["tree_hash"] if ev else None
                ev_latest = [e for e in ev if e["tree_hash"] == latest_tree] if latest_tree else []
                claim = (
                    json.loads(s["structured_output_json"]) if s["structured_output_json"] else None
                )
                sessions.append(
                    {
                        "id": s["id"],
                        "devin_id": s["devin_session_id"],
                        "url": s["url"],
                        "shard": w["shard_id"],
                        "files": w["files"],
                        "status": s["status"],
                        "status_detail": s["status_detail"],
                        "acus": s["acus_consumed"],
                        "size": s["session_size"],
                        "retries": s["retries"],
                        "pr_url": s["pull_request_url"],
                        "claim": claim,
                        "said_done": bool(s["self_reported_done"]),
                        "verdict": v,
                        "evidence": ev_latest,
                        "timeline": store.timeline(session_id=s["id"], limit=100),
                        "elapsed": ops._elapsed(s["created_at"], s["terminal_at"]),
                    }
                )
        last = store.timeline(ticket_id=t["id"], limit=1)
        row = _ticket_row(store, t)
        row.update(
            {
                "steps": _ticket_steps(store, t, wos, merged),
                "sessions_detail": sessions,
                "escalations": [
                    e
                    for e in store.list_escalations(unresolved_only=False)
                    if e["ticket_id"] == t["id"]
                ],
                "ready": ready.ready and not merged,
                "merged": merged,
                "readiness": {
                    "verified": ready.verified,
                    "shards": ready.shards,
                    "reviewed": ready.reviewed,
                    "conflicts": ready.conflicts,
                },
                "last_event": f"{last[0]['layer']}: {last[0]['event']}" if last else "",
                "verdict_json": t.get("triage_verdict_json"),
            }
        )
        rows.append(row)
    return {"rows": rows, "stage_names": [n for n, _ in STAGES]}


# ---------------------------------------------------------------------------- sessions
def _seconds(start: str | None, end: str | None) -> float | None:
    from datetime import UTC, datetime

    if not start:
        return None
    try:
        a = datetime.fromisoformat(start)
        b = datetime.fromisoformat(end) if end else datetime.now(UTC)
    except ValueError:
        return None
    a = a if a.tzinfo else a.replace(tzinfo=UTC)
    b = b if b.tzinfo else b.replace(tzinfo=UTC)
    return (b - a).total_seconds()


def _fmt(secs: float | None) -> str:
    if secs is None:
        return ""
    secs = int(secs)
    if secs < 90:
        return f"{secs}s"
    if secs < 5400:
        return f"{secs // 60}m"
    return f"{secs // 3600}h {(secs % 3600) // 60:02d}m"


def sessions(store: Store, cfg: TargetConfig) -> dict[str, Any]:
    o = ops.build(store)
    rows = store._all("SELECT * FROM sessions ORDER BY created_at DESC, rowid DESC")
    by_id = {r["id"]: r for r in rows}
    ticket_of: dict[str, dict[str, Any]] = {}
    for r in rows:
        wo = store.get_work_order(r["work_order_id"])
        ticket_of[r["id"]] = store.get_ticket(wo["ticket_id"]) if wo else {}
    # ETA reference: median elapsed of finished sessions, by size then overall
    finished = [
        r
        for r in rows
        if r["terminal_at"] and r["status"] == "exit" and r["status_detail"] == "finished"
    ]
    by_size: dict[str, list[float]] = {}
    for r in finished:
        secs = _seconds(r["created_at"], r["terminal_at"])
        if secs is not None:
            by_size.setdefault((r["session_size"] or "").upper(), []).append(secs)
            by_size.setdefault("*", []).append(secs)

    def median(xs: list[float]) -> float | None:
        xs = sorted(xs)
        return xs[len(xs) // 2] if xs else None

    devin_children: dict[str, list[str]] = {}
    for r in rows:
        if r["parent_session_id"]:
            devin_children.setdefault(r["parent_session_id"], []).append(
                r["devin_session_id"] or r["id"]
            )
    out = []
    for s in o["sessions"]:
        r = by_id[s["id"]]
        t = ticket_of.get(s["id"], {})
        live = bool(r["devin_session_id"]) and not r["terminal_at"]
        ref = median(by_size.get((r["session_size"] or "").upper(), [])) or median(
            by_size.get("*", [])
        )
        if live and ref is not None and ref >= 60:
            elapsed = _seconds(r["created_at"], None)
            eta = f"est. {_fmt(max(ref - (elapsed or 0), 0))} left"
        elif live:
            eta = f"cap {cfg.max_acu_limit} ACU · no completed session to estimate from"
        else:
            eta = "done"
        out.append(
            {
                **s,
                "source": SOURCE_LABELS.get(t.get("source", ""), (t.get("source", ""), ""))[0],
                "parent": r["parent_session_id"],
                "children": devin_children.get(r["devin_session_id"] or "", []),
                "eta": eta,
                "created": (r["created_at"] or "")[11:19],
            }
        )
    triage_rows = []
    for tr in store.list_triage_sessions():
        t = store.get_ticket(tr["ticket_id"]) or {}
        live = tr["devin_session_id"] and not tr["terminal_at"]
        detail = tr["status_detail"] or ""
        triage_rows.append(
            {
                "id": tr["id"],
                "kind": "triage",
                "devin_id": tr["devin_session_id"],
                "url": tr["url"],
                "ticket": tr["ticket_id"],
                "shard": "triage",
                "source": SOURCE_LABELS.get(t.get("source", ""), (t.get("source", ""), ""))[0],
                "status": tr["status"] or "new",
                "status_detail": detail,
                "pill": ops.STATUS_PILL.get(
                    detail, ops.STATUS_PILL.get(tr["status"] or "", "p-na")
                ),
                "acus": tr["acus_consumed"],
                "size": None,
                "parent": None,
                "children": [],
                "pr_url": None,
                "gate": None,
                "outcome": tr["outcome"],
                "eta": f"cap {TRIAGE_ACU_CAP} ACU" if live else "done",
                "created": (tr["created_at"] or "")[11:19],
                "elapsed": ops._elapsed(tr["created_at"], tr["terminal_at"]),
            }
        )
    out = sorted(out + triage_rows, key=lambda r: r["created"], reverse=True)
    managed = any(r["parent_session_id"] for r in rows)
    return {
        "sessions": out,
        "counts": o["counts"],
        "managed": managed,
        "eta_basis": {k: _fmt(median(v)) for k, v in by_size.items()},
    }


# ---------------------------------------------------------------------------- automations
VERIFIED_SOURCES = [
    ("github", "issues · pull_request · check_run · push · issue_comment · pull_request_review"),
    ("schedule", "recurring"),
    ("slack", "message · reaction_added"),
    ("webhook", "incoming"),
    ("snapshot_build", "completed"),
    ("linear · jira · pylon · incident_io · gitlab", "issue and pipeline events"),
]
TRIGGER_CHOICES = [
    ("github:issues", "Issues on the repository carrying a label"),
    ("github:pull_request", "A pull request opened by a bot"),
    ("github:check_run", "A failed check on a branch"),
    ("manual", "On click only"),
    ("schedule", "Periodically, on a schedule"),
]
KIND_NOTES = {
    "repair": "Run pulls the open issues with the label, makes a ticket of each new one, starts one triage session per ticket, routes them, starts the repair sessions, checks every PR from a clean checkout and asks Devin Review. You merge.",
    "scan": "Run points a session at the repository itself. It reads, finds places the upgrade changes behaviour, and files each one as a ticket. From there they go through the same scoping, checks and review as anything else. The session changes nothing.",
    "custom": "Run does the same as the default: issues to tickets, triage, route, repair, gate, review. You merge.",
}


def DEFAULT_TRIGGER(cfg: TargetConfig) -> dict[str, Any]:
    return {
        "source": "github",
        "event": "issues",
        "actions": ["opened", "labeled"],
        "match": {},
        "issue_label": cfg.trigger.get("issue_label", "swe-loop"),
    }


DEFAULT_NOTE = "The default. Every issue on the repository carrying the label becomes a ticket and goes through the whole loop."


def seed_automations(store: Store, cfg: TargetConfig) -> None:
    """The two automations every target starts with. Idempotent: keyed by fixed ids."""
    if store.get_automation("auto_repair") is None:
        store.upsert_automation(
            id="auto_repair",
            name="Issues from the fork",
            kind="repair",
            enabled=True,
            availability="live",
            trigger=DEFAULT_TRIGGER(cfg),
            target=cfg.repo,
            playbook="triage-pandas3 then repair-pandas3",
            max_acu=cfg.max_acu_limit,
            concurrency=4,
            notes=DEFAULT_NOTE,
        )
    sc = store.get_automation("auto_scan")
    if sc and (sc.get("notes") or "").startswith(("v1:", "Next version.")):
        store.set_automation("auto_scan", notes=None)
    if store.get_automation("auto_repair") is not None:
        a = store.get_automation("auto_repair")
        if a and a["name"] == "Repair":  # a store from before the rename
            store.conn.execute(
                "UPDATE automations SET name=?, trigger_json=?, playbook=?, notes=? WHERE id='auto_repair'",
                (
                    "Issues from the fork",
                    json.dumps(DEFAULT_TRIGGER(cfg), sort_keys=True),
                    "triage-pandas3 then repair-pandas3",
                    DEFAULT_NOTE,
                ),
            )
    if store.get_automation("auto_scan") is None:
        store.upsert_automation(
            id="auto_scan",
            name="Scan the repository",
            kind="scan",
            enabled=False,
            availability="live",
            trigger={"source": "schedule", "event": "recurring"},
            target=cfg.repo,
            playbook="scan-pandas3 then triage and repair",
            max_acu=4,
            concurrency=1,
            schedule="every weekday at 06:00",
            notes=None,
        )


_NATIVE: dict[str, tuple[float, dict[str, Any]]] = {}
NATIVE_TTL = 120.0


def _native_automations(client: DevinClient | None) -> dict[str, Any]:
    """What the organisation itself has, read at most once every couple of minutes.

    It is one line of context inside an opened row, so it must never be on the path that draws
    the page: a call across the network on every render made this the slowest page in the app."""
    import time as _time

    if client is None or client.is_fake:
        return {}
    hit = _NATIVE.get("all")
    if hit and _time.monotonic() - hit[0] < NATIVE_TTL:
        return hit[1]
    out: dict[str, Any] = {}
    try:
        for a in client.t.list_automations():
            out[a.get("name", "")] = a
    except Exception:  # noqa: BLE001 - context, never the page
        out = {}
    _NATIVE["all"] = (_time.monotonic(), out)
    return out


def automations(
    store: Store,
    cfg: TargetConfig,
    settings: Settings,
    client: DevinClient | None,
    running: bool,
    want_native: bool = False,
) -> dict[str, Any]:
    from swe_loop.intake import ADAPTERS

    seed_automations(store, cfg)
    native = _native_automations(client) if want_native else {}
    rows = []
    for a in store.list_automations():
        t = a["trigger"]
        rows.append(
            {
                **a,
                "trigger_label": f"{t.get('source', '')}:{t.get('event', '')}",
                "trigger_detail": (
                    (", ".join(t.get("actions", [])) + " · " if t.get("actions") else "")
                    + (" · ".join(f"{k}={v}" for k, v in (t.get("match") or {}).items()))
                    + (f" · label {t['issue_label']}" if t.get("issue_label") else "")
                    + (f" · {a['schedule']}" if a.get("schedule") else "")
                ),
                "kind_note": KIND_NOTES.get(a["kind"], KIND_NOTES["custom"]),
                "native": native.get(a["name"]),
                "runnable": a["availability"] == "live",
                "running": running and store.get_setting("automation.running") == a["id"],
            }
        )
    return {
        "rows": rows,
        "adapters": [(src, [ad.kind for ad in ads]) for src, ads in ADAPTERS.items()],
        "sources": VERIFIED_SOURCES,
        "trigger_choices": TRIGGER_CHOICES,
        "playbook_names": [p["name"] for p in store.list_playbooks()]
        or ["repair-pandas3", "triage-pandas3"],
        "target": cfg.repo,
        "cap": cfg.max_acu_limit,
        "routed": len(store.list_tickets("routed")),
        "live": settings.live,
        "native_note": (
            "native Devin Automations on the org are listed beside their config"
            if settings.live
            else "replay: the GitHub webhook posts to /intake/github; native Automations are created on the org in live mode"
        ),
    }


# ---------------------------------------------------------------------------- devin capability pages
def _md_to_html(md: str) -> str:
    """Just enough for the six-section playbooks: headings, numbered and bulleted lists,
    paragraphs, inline code."""
    import html as _html
    import re as _re

    out: list[str] = []
    mode = None
    for raw in md.splitlines():
        line = raw.rstrip()
        if line.startswith("# "):
            continue  # the title is the card header
        if line.startswith("## "):
            if mode:
                out.append(f"</{mode}>")
                mode = None
            out.append(f"<h4>{_html.escape(line[3:])}</h4>")
            continue
        m = _re.match(r"^(\d+)\.\s+(.*)", line)
        if m:
            if mode != "ol":
                if mode:
                    out.append(f"</{mode}>")
                out.append("<ol>")
                mode = "ol"
            out.append(f"<li>{_inline(m.group(2))}</li>")
            continue
        if line.startswith("- "):
            if mode != "ul":
                if mode:
                    out.append(f"</{mode}>")
                out.append("<ul>")
                mode = "ul"
            out.append(f"<li>{_inline(line[2:])}</li>")
            continue
        if line.startswith("   ") and mode in ("ol", "ul") and out and out[-1].endswith("</li>"):
            out[-1] = out[-1][:-5] + " " + _inline(line.strip()) + "</li>"
            continue
        if not line:
            if mode:
                out.append(f"</{mode}>")
                mode = None
            continue
        if mode:
            out.append(f"</{mode}>")
            mode = None
        out.append(f"<p>{_inline(line)}</p>")
    if mode:
        out.append(f"</{mode}>")
    return "\n".join(out)


def _inline(s: str) -> str:
    import html as _html
    import re as _re

    s = _html.escape(s)
    return _re.sub(r"`([^`]+)`", r"<code>\1</code>", s)


def seed_playbooks(store: Store, cfg: TargetConfig) -> None:
    """The playbooks on disk are the source of truth for the two agents; they are mirrored into
    the store so the page is one list, and user-added playbooks sit beside them."""
    from swe_loop.dispatch import ROOT as _R
    from swe_loop.dispatch import load_result_schema
    from swe_loop.knowledge import load_playbook
    from swe_loop.triage import TRIAGE_ACU_CAP, load_schema

    specs = [
        ("pb_triage", "triage-pandas3", "triage session", load_schema(), TRIAGE_ACU_CAP, "live"),
        (
            "pb_repair",
            "repair-pandas3",
            "repair sessions",
            load_result_schema(),
            cfg.max_acu_limit,
            "live",
        ),
    ]
    for pid, name, agent, schema, cap, avail in specs:
        pb = load_playbook(_R / "playbooks" / f"{name}.md")
        store.upsert_playbook(
            id=pid,
            name=name,
            agent=agent,
            body=pb.body,
            schema=schema,
            max_acu=cap,
            source="file",
            availability=avail,
        )
    from swe_loop.scan import SCAN_ACU_CAP
    from swe_loop.scan import load_schema as load_scan_schema

    scan_pb = _R / "playbooks" / "scan-pandas3.md"
    if scan_pb.exists():
        store.upsert_playbook(
            id="pb_scan",
            name="scan-pandas3",
            agent="scan session",
            body=scan_pb.read_text(),
            schema=load_scan_schema(),
            max_acu=SCAN_ACU_CAP,
            source="file",
            availability="live",
        )


_ORG_PB: dict[str, tuple[float, dict[str, str]]] = {}


def _org_playbooks(client: DevinClient | None) -> dict[str, str]:
    """Which playbooks the organisation already has, read at most once every couple of minutes."""
    import time as _time

    if client is None or client.is_fake:
        return {}
    hit = _ORG_PB.get("all")
    if hit and _time.monotonic() - hit[0] < NATIVE_TTL:
        return hit[1]
    out: dict[str, str] = {}
    try:
        for p in client.t.list_playbooks():
            out[p.get("title", "")] = p.get("playbook_id") or p.get("id") or ""
    except Exception:  # noqa: BLE001 - context, never the page
        out = {}
    _ORG_PB["all"] = (_time.monotonic(), out)
    return out


def playbooks(store: Store, cfg: TargetConfig, client: DevinClient | None) -> dict[str, Any]:
    seed_playbooks(store, cfg)
    org = _org_playbooks(client)
    used_by = {
        "pb_triage": ("the triage step", "/tickets-page?view=pipeline"),
        "pb_repair": ("every repair session", "/devin/sessions"),
        "pb_scan": ("the Scan automation", "/automations"),
    }
    rows = []
    for p in store.list_playbooks():
        title = next(
            (ln[2:].strip() for ln in p["body"].splitlines() if ln.startswith("# ")), p["name"]
        )
        sections = [ln[3:].strip() for ln in p["body"].splitlines() if ln.startswith("## ")]
        rows.append(
            {
                **p,
                "title": title,
                "sections": sections,
                "schema_fields": list((p.get("schema") or {}).get("properties", {}).keys()),
                "org_id": org.get(title),
                "used_by": used_by.get(
                    p["id"], ("user-added; attach it to an automation", "/automations")
                )[0],
                "used_by_link": used_by.get(p["id"], ("", "/automations"))[1],
            }
        )
    return {"rows": rows}


def playbook_detail(store: Store, pid: str) -> dict[str, Any] | None:
    p = store.get_playbook(pid)
    if not p:
        return None
    last = None
    if pid == "pb_triage":
        r = store._one(
            "SELECT triage_verdict_json FROM tickets WHERE triage_verdict_json IS NOT NULL ORDER BY updated_at DESC, rowid DESC LIMIT 1"
        )
        last = json.loads(r["triage_verdict_json"]) if r else None
    elif pid == "pb_repair":
        r = store._one(
            "SELECT structured_output_json FROM sessions WHERE structured_output_json IS NOT NULL ORDER BY created_at DESC, rowid DESC LIMIT 1"
        )
        last = json.loads(r["structured_output_json"]) if r else None
    title = next(
        (ln[2:].strip() for ln in p["body"].splitlines() if ln.startswith("# ")), p["name"]
    )
    return {
        **p,
        "title": title,
        "html": _md_to_html(p["body"]),
        "last_output": last,
        "schema_fields": list((p.get("schema") or {}).get("properties", {}).keys()),
    }


def knowledge(store: Store, settings: Settings, cfg: TargetConfig | None = None) -> dict[str, Any]:
    """The notes a session is given when it works on this repository, and whether any session
    has actually pulled each one."""
    from swe_loop.knowledge import load_notes

    used = {
        t["detail"].split("note ", 1)[-1].strip()
        for t in store.timeline(limit=5000)
        if t["event"] == "knowledge used" and t["detail"]
    }
    notes = []
    for n in load_notes():
        first = next((ln.strip() for ln in n.body.splitlines() if ln.strip()), "")
        notes.append(
            {
                "name": n.name,
                "trigger": n.trigger_description,
                "summary": first[:180],
                "body": n.body,
                "lines": len([ln for ln in n.body.splitlines() if ln.strip()]),
                "used": n.name in used,
                "file": Path(n.path).name if getattr(n, "path", None) else "",
            }
        )
    return {
        "notes": notes,
        "count": len(notes),
        "repo": (cfg.repo if cfg else ""),
        "live": settings.live,
    }


def insights(store: Store) -> dict[str, Any]:
    """What Devin reports back about each session, next to what it cost us.

    Devin sizes a session from XS to XL; anything at L or above means the piece was cut too
    large and should have been split. That size is real on every plan. What Devin reports as
    compute is 0 on a plan billed in credits, so cost here is our own measure."""
    from swe_loop import cost as cost_mod

    metered = cost_mod.spend(store)["metered"]
    kind_rates = cost_mod.rates(store)
    rows = []
    for s in store._all(
        "SELECT * FROM sessions WHERE devin_session_id IS NOT NULL ORDER BY created_at DESC, rowid DESC"
    ):
        wo = store.get_work_order(s["work_order_id"])
        v = store.latest_verdict(s["id"])
        usd, src = cost_mod.session_usd(store, s, "rep", kind_rates)
        rows.append(
            {
                "id": s["id"],
                "devin_id": s["devin_session_id"],
                "url": s["url"],
                "ticket": wo["ticket_id"] if wo else "",
                "number": (store.get_ticket(wo["ticket_id"]) or {}).get("number") if wo else None,
                "size": (s["session_size"] or "").upper(),
                "big": (s["session_size"] or "").upper() in ("L", "XL"),
                "minutes": cost_mod.repair_active_seconds(store, s) / 60.0,
                "usd": usd,
                "priced": src == "console",
                "gate": v["gate_result"] if v else None,
            }
        )
    for t in store.list_triage_sessions():
        usd, src = cost_mod.session_usd(store, t, "tri", kind_rates)
        rows.append(
            {
                "id": t["id"],
                "devin_id": t["devin_session_id"],
                "url": t["url"],
                "ticket": t["ticket_id"],
                "number": (store.get_ticket(t["ticket_id"]) or {}).get("number"),
                "size": "",
                "big": False,
                "minutes": cost_mod.triage_active_seconds(store, t) / 60.0,
                "usd": usd,
                "priced": src == "console",
                "gate": None,
            }
        )
    rows.sort(key=lambda r: r["minutes"], reverse=True)
    counts = {k: 0 for k in ("XS", "S", "M", "L", "XL")}
    for r in rows:
        if r["size"] in counts:
            counts[r["size"]] += 1
    return {
        "rows": rows,
        "hist": [(k, n, k in ("L", "XL")) for k, n in counts.items()],
        "sized": sum(counts.values()),
        "total": len(rows),
        "unhealthy": counts["L"] + counts["XL"],
        "metered": metered,
        "priced": sum(1 for r in rows if r["priced"]),
    }


def review(store: Store) -> dict[str, Any]:
    rows = []
    for v in store._all(
        "SELECT * FROM verdicts WHERE review_severity IS NOT NULL ORDER BY created_at DESC, rowid DESC"
    ):
        s = store.get_session(v["session_id"])
        wo = store.get_work_order(s["work_order_id"]) if s else None
        rows.append(
            {
                "pr_url": s["pull_request_url"] if s else None,
                "ticket": wo["ticket_id"] if wo else "",
                "devin_id": s["devin_session_id"] if s else "",
                "at": v["created_at"],
                "result": v["review_severity"],
            }
        )
    return {"rows": rows}


def integrations(
    settings: Settings, cfg: TargetConfig, store: Store, client: DevinClient | None
) -> dict[str, Any]:
    checks = {c.key: c for c in connect.run_checks(settings, cfg, store, client)}
    app = checks.get("app")
    secrets: list[str] = []
    if client is not None and not client.is_fake:
        try:
            secrets = [s.get("name") or s.get("key") or "?" for s in client.t.list_secrets()]
        except Exception:  # noqa: BLE001
            secrets = []
    return {
        "app": {
            "status": app.status if app else "skipped",
            "value": app.value if app else "unknown",
            "call": app.call if app else "",
        },
        "repo": cfg.repo,
        "secrets": secrets,
        "allowlist": ["pypi.org", "files.pythonhosted.org", "api.github.com", "github.com"],
        "snapshot": "not built in this run",
        "org": settings.devin_org_id or "replay",
        "plan": "Free plan; every endpoint this system uses answers on it",
        "live": settings.live,
    }


NEXT = [
    {
        "name": "Computer Use",
        "what": "Every cloud session runs on a VM with a desktop and a browser. A QA session starts the application, opens it, exercises the fixed path, and reports through structured output with a screenshot.",
        "where": "a third check after the gate, on the Tracker; enabled per org by an admin (Settings, Customization, Enable desktop mode)",
    },
    {
        "name": "DeepWiki",
        "what": "Generated documentation for the repository, kept current, so the people who merge can read what the code does before they read the diff.",
        "where": "a link on the repository card in Settings, and context for the scan session",
    },
    {
        "name": "Security Swarm",
        "what": "Devin's own scanner, an orchestration of parallel sessions that builds a threat model and validates findings. Consumed as a ticket source, never rebuilt.",
        "where": "a second source on the Tickets page; each finding must name the SECURITY.md row and the principal, or it is filed as a question",
    },
    {
        "name": "Scan session",
        "what": "The triage playbook pointed at a repository instead of a ticket. It reads the code and the test output and files the tickets itself, on a schedule.",
        "where": "the Scan automation; tickets appear in the Scan group",
    },
    {
        "name": "Evaluator session",
        "what": "Reads Session Insights across completed sessions and proposes edits to the playbooks and Knowledge notes. Grades on outcomes only, never on transcripts; every proposal is approved by a person.",
        "where": "the Insights page, as proposed diffs awaiting approval",
    },
    {
        "name": "Devin MCP",
        "what": "The MCP server exposes session creation, search, interaction and a gather primitive that waits on many sessions at once, with no REST equivalent.",
        "where": "fan-in over MCP instead of the poller, when the orchestrator is itself agent-driven",
    },
]


def next_page() -> list[dict[str, str]]:
    return NEXT
