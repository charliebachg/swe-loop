"""One builder per page. Thin: every number comes from the store, the timeline, the seam, or
the files on disk. Nothing here calls Devin except the read-only listings in live mode."""

from __future__ import annotations

import json
from typing import Any

from swe_loop import connect, ops
from swe_loop import reduce as reduce_mod
from swe_loop.config import Settings, TargetConfig
from swe_loop.devin import DevinClient
from swe_loop.store import Store

NAV = [
    ("home", "Home", "/"),
    ("automations", "Automations", "/automations"),
    ("tickets", "Tickets", "/tickets-page"),
    ("tracker", "Tracker", "/tracker"),
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
        return f"/tracker#{e['ticket_id']}"
    return "/tracker"


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
                "link": f"/tracker#{e['ticket_id']}",
                "action": "a person decides",
            }
        )
    for tid in summary["ready"]:
        needs.append(
            {
                "kind": "ready to merge",
                "ticket_id": tid,
                "reason": "every shard passed the gate and was reviewed; no conflicts",
                "since": "",
                "link": f"/tracker#{tid}",
                "action": "a person merges",
            }
        )
    recent = [{**e, "link": _owner_link(e)} for e in reversed(store.timeline(limit=25))]
    return {"counts": o["counts"], "needs": needs, "recent": recent, "summary": summary}


def settings_page(
    settings: Settings, cfg: TargetConfig, store: Store, client: DevinClient | None
) -> dict[str, Any]:
    checks = connect.run_checks(settings, cfg, store, client)
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
    n_sessions = sum(len(store.sessions_for(w["id"])) for w in wos)
    num, url = _issue(t)
    letter = t["id"].removeprefix("tkt_")[:1] if t["id"].startswith("tkt_") else ""
    return {
        "id": t["id"],
        "letter": letter if letter in "ABCDE" else "",
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
    return {"groups": merged, "total": total}


# ---------------------------------------------------------------------------- tracker
STAGES = ops.STEPS
ACTOR = {
    "L0": "code",
    "L1": "devin",
    "L2": "code",
    "L4": "code",
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
        if any((v.get("review_severity") or "").startswith("requested") for v in verdicts):
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
