from pathlib import Path

import pytest

from swe_loop.config import TargetConfig
from swe_loop.store import Store, load_tickets

ROOT = Path(__file__).resolve().parents[1]
TICKETS = ROOT / "data" / "inventory" / "2026-09-03" / "tickets.json"


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "t.sqlite")


def test_schema_and_wal(store):
    assert store.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    tables = {r[0] for r in store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "events",
        "tickets",
        "work_orders",
        "sessions",
        "evidence",
        "verdicts",
        "escalations",
        "human_actions",
        "budget",
    } <= tables


def test_seed_from_inventory(store):
    ids = load_tickets(store, TICKETS)
    assert len(ids) == 5  # A-E; P is the parent_ref, not a ticket
    tickets = store.list_tickets()
    assert all(t["status"] == "triaged" for t in tickets)
    assert all(t["parent_ref"] and t["parent_ref"].endswith("#6") for t in tickets)
    devin = [t for t in tickets if store.work_orders_for(t["id"])]
    assert len(devin) == 4  # E has no work order: it is human-only
    wo = store.work_orders_for("tkt_D")[0]
    assert wo["files"] == ["superset/models/helpers.py"]
    assert "pandas_3_0_5" in wo["acceptance"]


def test_router_decisions_create_escalations(store):
    load_tickets(store, TICKETS)
    store.set_router_decision("tkt_E", "human_only", "sessions never edit tests")
    store.set_router_decision("tkt_D", "devin", "one site, acceptance command exists")
    assert store.get_ticket("tkt_E")["status"] == "escalated"
    assert store.get_ticket("tkt_D")["status"] == "routed"
    esc = store.list_escalations()
    assert len(esc) == 1 and esc[0]["kind"] == "human_only"


def test_session_row_exists_before_devin_id(store):
    load_tickets(store, TICKETS)
    wo = store.work_orders_for("tkt_D")[0]
    sid = store.reserve_session(work_order_id=wo["id"], playbook_id=None, tags=["swe-loop", "D"])
    s = store.get_session(sid)
    assert s["status"] == "reserved" and s["devin_session_id"] is None
    store.bind_devin_session(
        sid, devin_session_id="devin-abc", url="https://app.devin.ai/sessions/abc"
    )
    assert store.session_by_devin_id("devin-abc")["id"] == sid


def test_evidence_is_bound_to_tree(store):
    load_tickets(store, TICKETS)
    wo = store.work_orders_for("tkt_D")[0]
    sid = store.reserve_session(work_order_id=wo["id"], playbook_id=None, tags=[])
    store.insert_evidence(
        session_id=sid,
        tier="T1",
        command="pytest x",
        cwd="/r",
        tree_hash="aaa",
        exit_code=0,
        output="ok",
    )
    assert len(store.evidence_for(sid, "aaa")) == 1
    assert store.evidence_for(sid, "bbb") == []  # a receipt from another tree does not count


def test_metrics_and_funnel(store):
    load_tickets(store, TICKETS)
    store.set_budget(acu_cap=300, per_session_cap=6)
    for t in ("tkt_A", "tkt_B", "tkt_C", "tkt_D"):
        store.set_router_decision(t, "devin", "ok")
    store.set_router_decision("tkt_E", "human_only", "tests are the oracle")
    wo = store.work_orders_for("tkt_D")[0]
    sid = store.reserve_session(work_order_id=wo["id"], playbook_id=None, tags=[])
    store.bind_devin_session(sid, devin_session_id="d1", url="u")
    store.update_session(
        sid,
        status="exit",
        status_detail="finished",
        terminal_at="t",
        acus_consumed=2.5,
        session_size="S",
        self_reported_done=1,
    )
    store.insert_verdict(
        session_id=sid, gate_result="pass", decision="pass", reason="T0 clean, T1 0 hits"
    )
    store.record_human_action("tkt_D", "merge", "someone")
    m = store.metrics()
    assert m["verified_changes"] == {"n": 1, "of": 5}
    assert m["acu_per_verified"]["median"] == 2.5
    assert m["self_reported_vs_verified"] == {"said_done": 1, "passed_gate": 1, "gap": 0}
    assert m["budget"]["spent"] == 2.5 and m["budget"]["cap"] == 300
    f = store.funnel()
    assert f["routed_to_devin"] == 4 and f["refused_or_human"] == 1 and f["human_merged"] == 1


def test_target_config_loads_the_seam():
    cfg = TargetConfig.load(ROOT / "configs" / "superset-pandas3.yaml")
    assert cfg.repo == "charliebachg/superset"
    assert "tests/" in cfg.forbidden_paths
    assert cfg.max_acu_limit == 6
