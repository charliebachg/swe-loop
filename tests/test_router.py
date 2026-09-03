from pathlib import Path

from swe_loop.config import TargetConfig
from swe_loop.router import decide, route_all, route_ticket
from swe_loop.shard import split_work_order
from swe_loop.store import Store, load_tickets

ROOT = Path(__file__).resolve().parents[1]
CFG = TargetConfig.load(ROOT / "configs" / "superset-pandas3.yaml")
MIN = TargetConfig.load(ROOT / "configs" / "example-minimal.yaml")
TICKETS = ROOT / "data" / "inventory" / "2026-09-03" / "tickets.json"


def wo(files, tests=None, shard="X"):
    return {
        "id": "wo",
        "shard_id": shard,
        "files": files,
        "tests": tests or ["tests/unit_tests/x_test.py"],
        "acceptance": {"p3": "pytest"},
        "est_size": "S",
    }


def test_refuses_forbidden_paths():
    d = decide(split_work_order(wo(["tests/unit_tests/utils/excel_tests.py"]), CFG)[0], CFG, None)
    assert d.route == "refuse" and "forbidden" in d.reason
    d = decide(split_work_order(wo([".github/workflows/ci.yml"]), CFG)[0], CFG, None)
    assert d.route == "refuse"


def test_coverage_gate_needs_a_covering_test():
    sh = split_work_order(
        wo(["superset/sql/parse.py"], ["tests/unit_tests/charts/x_test.py"]), CFG
    )[0]
    assert decide(sh, CFG, None).route == "refuse"
    sh = split_work_order(
        wo(["superset/sql/parse.py"], ["tests/unit_tests/sql/parse_test.py"]), CFG
    )[0]
    assert decide(sh, CFG, None).route == "devin"


def test_human_only_class_from_the_seam():
    verdict = {"sites": [{"file": "superset/a.py", "line": 1, "class": "chained-assignment"}]}
    sh = split_work_order(wo(["superset/a.py"]), CFG)[0]
    d = decide(sh, CFG, verdict)
    assert d.route == "human_only" and "context-dependent" in d.reason


def test_review_required_for_silent_behaviour_change():
    verdict = {
        "sites": [
            {
                "file": "superset/charts/client_processing.py",
                "line": 639,
                "classes": ["inplace-on-copy"],
                "warned": True,
                "broke": False,
            },
        ]
    }
    sh = split_work_order(wo(["superset/charts/client_processing.py"]), CFG, verdict["sites"])[0]
    d = decide(sh, CFG, verdict)
    assert d.route == "devin" and d.review == "required" and "silent" in d.reason


def test_sharder_keeps_files_whole_and_respects_caps():
    files = ["superset/a.py", "superset/b.py", "superset/c.py", "superset/d.py"]
    sites = (
        [{"file": "superset/a.py"}] * 5
        + [{"file": "superset/b.py"}] * 2
        + [{"file": "superset/c.py"}]
        + [{"file": "superset/d.py"}]
    )
    shards = split_work_order(wo(files, shard="Q"), CFG, sites)  # cap: 3 files, 6 sites
    assert [s["files"] for s in shards] == [
        ["superset/a.py"],
        ["superset/b.py", "superset/c.py", "superset/d.py"],
    ]
    assert [s["shard_id"] for s in shards] == ["Q1", "Q2"]
    assert all(not s["oversize"] for s in shards)
    big = split_work_order(wo(["superset/a.py"]), CFG, [{"file": "superset/a.py"}] * 9)
    assert big[0]["oversize"] and decide(big[0], CFG, None).route == "human_only"


def test_second_seam_routes_with_its_own_policy():
    # the minimal seam caps at 2 files and has no coverage gate or human-only classes
    shards = split_work_order(wo(["src/a.py", "src/b.py", "src/c.py"]), MIN)
    assert len(shards) == 2
    assert decide(shards[0], MIN, None).route == "devin"
    assert decide(split_work_order(wo(["tests/test_a.py"]), MIN)[0], MIN, None).route == "refuse"
    assert MIN.max_acu_limit == 4 and MIN.repo == "acme/widgets"


def test_route_all_on_the_seeded_tickets(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    load_tickets(st, TICKETS)
    decisions = route_all(st, CFG)
    top = {tid: st.get_ticket(tid)["router_decision"] for tid in decisions}
    assert top == {
        "tkt_A": "devin",
        "tkt_B": "devin",
        "tkt_C": "devin",
        "tkt_D": "devin",
        "tkt_E": "human_only",
    }
    a = decisions["tkt_A"][0]
    assert a.review == "required"  # three warned-but-didn't-break sites in client_processing.py
    assert all(d.review == "normal" for d in decisions["tkt_D"])
    esc = st.list_escalations()
    assert len(esc) == 1 and esc[0]["ticket_id"] == "tkt_E" and esc[0]["kind"] == "human_only"
    assert st.get_ticket("tkt_E")["status"] == "escalated"
    assert all(st.get_ticket(t)["status"] == "routed" for t in ("tkt_A", "tkt_B", "tkt_C", "tkt_D"))
    # no work order was split: every shard already fits the caps
    assert all(
        w["status"] == "devin"
        for t in ("tkt_A", "tkt_B", "tkt_C", "tkt_D")
        for w in st.work_orders_for(t)
    )


def test_route_ticket_without_work_order_or_verdict_refuses(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    st.upsert_ticket(id="t1", source="manual", title="x", status="triaged")
    d = route_ticket(st, "t1", CFG)
    assert d[0].route == "refuse" and st.get_ticket("t1")["status"] == "refused"
