from pathlib import Path

import pytest

from swe_loop.config import TargetConfig
from swe_loop.store import Store
from swe_loop.triage import PLAYBOOK_PATH, apply_verdict, build_triage_spec, validate_verdict

ROOT = Path(__file__).resolve().parents[1]
CFG = TargetConfig.load(ROOT / "configs" / "superset-pandas3.yaml")

GOOD = {
    "ticket_id": "tkt_pu42671",
    "summary": "Ten product sites in eight files; four shards; fifteen test expectations need a person.",
    "classes": ["inplace-on-copy", "str-dtype"],
    "sites": [
        {
            "file": "superset/models/helpers.py",
            "line": 345,
            "class": "to_datetime-mixed-tz",
            "kind": "mechanical",
            "prescribed_fix": "pass utc=True",
            "tests": ["tests/unit_tests/common/test_time_shifts.py"],
        },
        {
            "file": "superset/charts/client_processing.py",
            "line": 639,
            "class": "inplace-on-copy",
            "kind": "semantic",
            "tests": ["tests/unit_tests/charts/test_client_processing.py"],
        },
    ],
    "acceptance_cmd": {
        "p2": ".venv-p2/bin/python -m pytest -W error::FutureWarning tests/unit_tests/common/test_time_shifts.py",
        "p3": ".venv-p3/bin/python -m pytest tests/unit_tests/common/test_time_shifts.py",
    },
    "context_sufficient": True,
    "missing": [],
    "split": "parallel",
    "shards": [
        {
            "id": "D",
            "files": ["superset/models/helpers.py"],
            "tests": ["tests/unit_tests/common/test_time_shifts.py"],
            "classes": ["to_datetime-mixed-tz"],
            "est_size": "XS",
        },
        {
            "id": "A",
            "files": ["superset/charts/client_processing.py"],
            "tests": ["tests/unit_tests/charts/test_client_processing.py"],
            "classes": ["inplace-on-copy"],
            "est_size": "S",
            "review": "required",
        },
    ],
    "est_size": "M",
    "needs_human": [
        {
            "site": "tests/unit_tests/utils/excel_tests.py:41",
            "reason": "test expectation; oracle is read-only",
        }
    ],
}


def test_playbook_has_the_six_sections():
    text = PLAYBOOK_PATH.read_text()
    for section in (
        "## Overview",
        "## Procedure",
        "## Specifications",
        "## Advice and Pointers",
        "## Forbidden Actions",
        "## Required from User",
    ):
        assert section in text
    assert "Do not modify any file" in text


def test_schema_accepts_a_good_verdict_and_rejects_bad_ones():
    assert validate_verdict(GOOD) == []
    bad = dict(GOOD, split="parallel", shards=[])
    assert any("shards" in p for p in validate_verdict(bad))
    bad = dict(GOOD, est_size="huge")
    assert any("est_size" in p for p in validate_verdict(bad))
    bad = {k: v for k, v in GOOD.items() if k != "acceptance_cmd"}
    assert any("acceptance_cmd" in p for p in validate_verdict(bad))
    bad = dict(GOOD, extra_field=1)
    assert any("extra_field" in p for p in validate_verdict(bad))


def test_spec_carries_the_contract_and_the_cap():
    ticket = {
        "id": "tkt_pu42671",
        "title": "bump pandas",
        "external_ref": "charliebachg/superset#42671",
    }
    spec = build_triage_spec(
        ticket, CFG, inventory_path="data/inventory/2026-09-03/sites.json", playbook_id=None
    )
    p = spec.to_payload()
    assert p["max_acu_limit"] == 3 and p["repos"] == ["charliebachg/superset"]
    assert p["structured_output_required"] is True and p["structured_output_schema"][
        "title"
    ].startswith("swe-loop")
    assert "## What" in p["prompt"] and "## How" in p["prompt"] and "## Result" in p["prompt"]
    assert "Do not change any code" in p["prompt"] and "sites.json" in p["prompt"]
    assert "tests/" in p["prompt"]  # forbidden paths from the seam appear in the Don'ts
    assert p["tags"] == ["swe-loop", "triage", "tkt_pu42671"]


def test_apply_verdict_creates_one_work_order_per_shard(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    st.upsert_ticket(id="tkt_pu42671", source="github", title="bump pandas", status="triaged")
    ids = apply_verdict(st, "tkt_pu42671", GOOD)
    assert len(ids) == 2
    wos = st.work_orders_for("tkt_pu42671")
    assert {w["shard_id"] for w in wos} == {"A", "D"}
    assert st.get_ticket("tkt_pu42671")["class"] == "inplace-on-copy,str-dtype"


def test_apply_verdict_refuses_invalid(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    st.upsert_ticket(id="t1", source="manual", title="x", status="triaged")
    with pytest.raises(ValueError):
        apply_verdict(st, "t1", {"ticket_id": "t1"})


def test_review_required_on_a_shard_is_lifted_for_the_router(tmp_path):
    from swe_loop.config import TargetConfig
    from swe_loop.router import route_all
    from swe_loop.store import Store

    st = Store(tmp_path / "t.sqlite")
    st.upsert_ticket(id="tkt_pu42671", source="manual", title="x", status="new")
    v = {**GOOD, "split": "one", "shards": [{**GOOD["shards"][0], "review": "required"}]}
    v.pop("review", None)
    apply_verdict(st, "tkt_pu42671", v)
    cfg = TargetConfig.load(ROOT / "configs" / "superset-pandas3.yaml")
    d = route_all(st, cfg)["tkt_pu42671"]
    assert d and d[0].review == "required"
    assert "review required" in st.get_ticket("tkt_pu42671")["router_reason"]


def test_a_second_verdict_sets_the_first_plans_work_orders_aside(tmp_path):
    """A person answers, the ticket is scoped again, and the new verdict is the plan. The old
    plan's work orders must not be dispatched beside the new ones: they are set aside, the
    dispatch loop skips them, and readiness does not count them as shards."""
    from swe_loop.reduce import readiness
    from swe_loop.router import route_all

    st = Store(tmp_path / "t.sqlite")
    st.upsert_ticket(id="tkt_pu42671", source="github", title="bump pandas", status="triaged")
    first = apply_verdict(st, "tkt_pu42671", GOOD, CFG)
    route_all(st, CFG)
    assert [w["status"] for w in st.work_orders_for("tkt_pu42671")] == ["devin", "devin"]

    second = apply_verdict(st, "tkt_pu42671", GOOD, CFG)
    route_all(st, CFG)
    wos = st.work_orders_for("tkt_pu42671")
    by_id = {w["id"]: w["status"] for w in wos}
    assert all(by_id[i] == "superseded" for i in first), "the old plan is set aside"
    assert all(by_id[i] == "devin" for i in second), "only the new plan is live"
    live = [w for w in wos if w["status"] in ("devin", "dispatched")]
    assert len(live) == 2, "exactly one plan's worth of work orders can be dispatched"
    assert readiness(st, "tkt_pu42671").shards == 2
    assert any("set aside" in e["event"] for e in st.timeline(limit=20))
