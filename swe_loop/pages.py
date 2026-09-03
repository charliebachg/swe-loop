"""One builder per page. Thin: every number comes from the store, the timeline, the seam, or
the files on disk. Nothing here calls Devin except the read-only listings in live mode."""

from __future__ import annotations

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
