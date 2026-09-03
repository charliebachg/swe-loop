"""L9: report. Every number on the dashboard is a query over the ticket store, and every tile
carries the SQL that produced it. Nothing is narrated.

Six rows, in the order an engineering leader reads: the answer, the three things the brief asks
for (status, success/failure, progress), how we know it fixed it, the pre-registered tripwires,
the routing table, and escalations beside the list of metrics deliberately not shown.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from swe_loop.store import Store

ROOT = Path(__file__).resolve().parents[1]

BANNED = [
    (
        "Lines of code",
        "Volume is not value: about 10% velocity from about 75% AI-authored code at one large vendor is the public counterexample.",
    ),
    (
        "Pull requests opened",
        "28.3% of agent PRs merge within one minute and the tail is expensive (arXiv:2601.00753). Count what merged and survived.",
    ),
    (
        "Acceptance rate",
        "Rubber-stamping inflates it; the CTOs of two developer-metrics companies are on record against it.",
    ),
    ("Share of code written by AI", "Definitionally not the metric, per the point above."),
    (
        "Tokens",
        "Cost shifts into the review gate; cheaper tokens did not yield cheaper merged features.",
    ),
    (
        "Self-reported time saved",
        "The best RCT measured that self-report as wrong by about 40 points (METR, 2025; corrected 2026).",
    ),
]

Q = {
    "verified": """SELECT COUNT(DISTINCT t.id) FROM tickets t
JOIN work_orders w ON w.ticket_id = t.id
JOIN sessions s ON s.work_order_id = w.id
JOIN verdicts v ON v.session_id = s.id AND v.gate_result = 'pass'
JOIN human_actions h ON h.ticket_id = t.id AND h.kind = 'merge'""",
    "decided": "SELECT COUNT(*) FROM tickets WHERE router_decision IS NOT NULL",
    "acus": """SELECT s.acus_consumed FROM sessions s
JOIN verdicts v ON v.session_id = s.id AND v.gate_result = 'pass'
WHERE s.acus_consumed IS NOT NULL ORDER BY s.acus_consumed""",
    "said_done": "SELECT COUNT(*) FROM sessions WHERE self_reported_done = 1",
    "passed": "SELECT COUNT(DISTINCT session_id) FROM verdicts WHERE gate_result = 'pass'",
    "spent": "SELECT COALESCE(SUM(acus_consumed), 0) FROM sessions",
    "board": "SELECT id, class, status, router_decision, external_ref, updated_at FROM tickets ORDER BY created_at",
    "sizes": "SELECT session_size, COUNT(*) FROM sessions WHERE session_size IS NOT NULL GROUP BY session_size",
    "oracle": "SELECT COUNT(*) FROM escalations WHERE kind = 'oracle_touched'",
    "zero_review": """SELECT COUNT(*) FROM tickets t WHERE t.status = 'merged'
AND NOT EXISTS (SELECT 1 FROM human_actions h WHERE h.ticket_id = t.id AND h.kind = 'merge')""",
    "retries": "SELECT retries FROM sessions WHERE terminal_at IS NOT NULL ORDER BY retries",
    "escalations": "SELECT ticket_id, session_id, kind, reason, created_at, resolved_at FROM escalations ORDER BY created_at",
    "receipts": """SELECT s.id, s.devin_session_id, s.url, s.pull_request_url, s.acus_consumed, s.session_size,
s.self_reported_done, s.retries, w.shard_id, w.ticket_id FROM sessions s
JOIN work_orders w ON w.id = s.work_order_id WHERE s.devin_session_id IS NOT NULL ORDER BY s.created_at""",
}


def pct(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    k = max(0, min(len(xs) - 1, round(p * (len(xs) - 1))))
    return xs[k]


def load_sites(inventory_dir: Path | None) -> list[dict[str, Any]]:
    if not inventory_dir:
        return []
    p = Path(inventory_dir) / "sites.json"
    return json.loads(p.read_text()) if p.exists() else []


def build(
    store: Store, inventory_dir: Path | None = None, acu_rate: tuple[float, float] = (2.0, 2.25)
) -> dict[str, Any]:
    m = store.metrics()
    f = store.funnel()
    budget = m["budget"]

    # ---- row 1
    headline = {
        "verified": {**m["verified_changes"], "sql": Q["verified"] + "\n-- of --\n" + Q["decided"]},
        "acu": {**m["acu_per_verified"], "usd_low": None, "usd_high": None, "sql": Q["acus"]},
        "claims": {
            **m["self_reported_vs_verified"],
            "sql": Q["said_done"] + "\n-- vs --\n" + Q["passed"],
        },
        "budget": {**budget, "sql": Q["spent"]},
    }
    if headline["acu"]["median"] is not None:
        headline["acu"]["usd_low"] = round(headline["acu"]["median"] * acu_rate[0], 2)
        headline["acu"]["usd_high"] = round(headline["acu"]["median"] * acu_rate[1], 2)

    # ---- row 2
    board = store._all(Q["board"])
    funnel = [
        ("tickets decided", f["tickets"], None),
        ("routed to Devin", f["routed_to_devin"], None),
        ("refused or human-only", f["refused_or_human"], "drop"),
        ("sessions created", f["sessions_created"], None),
        ("sessions terminal", f["sessions_terminal"], None),
        ("gate passed", f["gate_passed"], None),
        ("gate failed or no evidence", f["gate_failed"], "drop"),
        ("human-merged", f["human_merged"], None),
    ]
    sites = load_sites(inventory_dir)
    product = [s for s in sites if s.get("where") == "superset"]
    test_only = [s for s in sites if s.get("where") == "test-only"]
    merged_files: set[str] = set()
    human_files: set[str] = set()
    for t in store.list_tickets():
        for w in store.work_orders_for(t["id"]):
            if t["status"] == "merged":
                merged_files.update(w["files"])
        if t["router_decision"] == "human_only":
            v = json.loads(t["triage_verdict_json"]) if t["triage_verdict_json"] else {}
            human_files.update(s.get("file") for s in v.get("sites", []))
    fixed = [s for s in product if s["file"] in merged_files]
    refused = [s for s in sites if s["file"] in human_files] + test_only
    burndown = {
        "total": len(sites),
        "product": len(product),
        "test_only": len(test_only),
        "fixed": len(fixed),
        "human": len({(s["file"], tuple(s["lines"])) for s in refused}),
        "remaining": max(0, len(product) - len(fixed)),
    }

    # ---- row 3
    receipts = []
    for s in store._all(Q["receipts"]):
        ev = store.evidence_for(s["id"])
        latest_tree = ev[-1]["tree_hash"] if ev else None
        ev = [e for e in ev if e["tree_hash"] == latest_tree] if latest_tree else []
        t0 = [e for e in ev if e["tier"] == "T0"]
        t1 = [e for e in ev if e["tier"] == "T1"]
        v = store.latest_verdict(s["id"])
        merged = store._one(
            "SELECT 1 AS x FROM human_actions WHERE ticket_id=? AND kind='merge'", s["ticket_id"]
        )
        receipts.append(
            {
                "ticket": s["ticket_id"],
                "shard": s["shard_id"],
                "session_url": s["url"],
                "devin_id": s["devin_session_id"],
                "pr_url": s["pull_request_url"],
                "acus": s["acus_consumed"],
                "size": s["session_size"],
                "said_done": bool(s["self_reported_done"]),
                "t0": None if not t0 else all(e["passed"] for e in t0),
                "t1": None if not t1 else f"{sum(1 for e in t1 if e['passed'])}/{len(t1)}",
                "gate": v["gate_result"] if v else None,
                "review": (v or {}).get("review_severity"),
                "retries": s["retries"],
                "merged_by": "human" if merged else "no",
                "evidence": [
                    {
                        "tier": e["tier"],
                        "command": e["command"],
                        "exit": e["exit_code"],
                        "path": e["output_path"],
                    }
                    for e in ev
                ],
            }
        )
    sizes = {r[0]: r[1] for r in store.conn.execute(Q["sizes"]).fetchall()}
    size_hist = [
        (k, sizes.get(k, 0), k in ("L", "XL", "l", "xl")) for k in ("XS", "S", "M", "L", "XL")
    ]

    # ---- row 4
    retries = [r["retries"] for r in store._all(Q["retries"])]
    acus = [r["acus_consumed"] for r in store._all(Q["acus"])]
    oracle = store._one(Q["oracle"])["COUNT(*)"]
    zero_review = store._one(Q["zero_review"])["COUNT(*)"]
    cap = budget.get("per_session_cap")
    p95 = pct(acus, 0.95)
    tripwires = [
        {
            "name": "oracle touched by a session",
            "value": f"{oracle} of {f['sessions_terminal']}",
            "threshold": "any occurrence is shown",
            "status": "FAIL" if oracle else "PASS",
            "sql": Q["oracle"],
        },
        {
            "name": "merged with zero human review",
            "value": f"{zero_review} of {f['human_merged']}",
            "threshold": "0, by construction: a person clicks merge",
            "status": "FAIL" if zero_review else "PASS",
            "sql": Q["zero_review"],
        },
        {
            "name": "retries before success, p95",
            "value": pct(retries, 0.95) if retries else "n/a",
            "threshold": "2",
            "status": ("FAIL" if (pct(retries, 0.95) or 0) > 2 else "PASS") if retries else "n/a",
            "sql": Q["retries"],
        },
        {
            "name": "ACU p95 vs per-session cap",
            "value": f"{p95} / {cap}" if p95 is not None else "n/a",
            "threshold": "at the cap means too large",
            "status": ("FAIL" if cap and p95 is not None and p95 >= cap else "PASS")
            if p95 is not None
            else "n/a",
            "sql": Q["acus"],
        },
        {
            "name": "human minutes per merged change",
            "value": "not instrumented in this run",
            "threshold": "no baseline yet",
            "status": "n/a",
            "sql": "-- derived from GitHub timestamps between PR ready and merge; not collected in this run",
        },
        {
            "name": "survived 30 days",
            "value": "window not elapsed",
            "threshold": "30 days",
            "status": "n/a",
            "sql": "-- reverts within 30 days of merge; the window has not elapsed",
        },
    ]

    # ---- row 5
    by_class: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"attempted": 0, "verified": 0, "acus": [], "route": set(), "review": set()}
    )
    for t in store.list_tickets():
        classes = [c for c in (t["class"] or "").split(",") if c] or ["unclassified"]
        v = json.loads(t["triage_verdict_json"]) if t["triage_verdict_json"] else {}
        for c in classes:
            row = by_class[c]
            row["route"].add(t["router_decision"] or "pending")
            if v.get("review") == "required":
                row["review"].add("required")
            for w in store.work_orders_for(t["id"]):
                for s in store.sessions_for(w["id"]):
                    if s["devin_session_id"]:
                        row["attempted"] += 1
                        vd = store.latest_verdict(s["id"])
                        if vd and vd["gate_result"] == "pass":
                            row["verified"] += 1
                            if s["acus_consumed"] is not None:
                                row["acus"].append(s["acus_consumed"])
    routing = []
    for c, r in sorted(by_class.items()):
        a = sorted(r["acus"])
        if "human_only" in r["route"] or "refuse" in r["route"]:
            verdict = "human-only" if "human_only" in r["route"] else "refused"
        elif r["attempted"] and r["verified"] == r["attempted"]:
            verdict = "assisted (review required)" if r["review"] else "autonomous"
        elif r["attempted"]:
            verdict = "escalated" if r["verified"] == 0 else "partial"
        else:
            verdict = "not attempted"
        routing.append(
            {
                "class": c,
                "attempted": r["attempted"],
                "verified": r["verified"],
                "median": pct(a, 0.5),
                "p95": pct(a, 0.95),
                "verdict": verdict,
            }
        )

    # ---- row 6
    escalations = store._all(Q["escalations"])

    return {
        "headline": headline,
        "board": board,
        "funnel": funnel,
        "funnel_sql": "-- see Store.funnel(): one COUNT per stage over tickets, sessions, verdicts, human_actions",
        "burndown": burndown,
        "receipts": receipts,
        "size_hist": size_hist,
        "tripwires": tripwires,
        "routing": routing,
        "escalations": escalations,
        "banned": BANNED,
        "n_sessions": f["sessions_created"],
        "acu_rate": acu_rate,
    }
