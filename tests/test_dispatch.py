from pathlib import Path

from jsonschema import Draft7Validator

from swe_loop.config import TargetConfig
from swe_loop.devin import DevinClient, FakeTransport
from swe_loop.dispatch import build_repair_spec, dispatch, load_result_schema
from swe_loop.knowledge import load_notes, load_playbook
from swe_loop.router import route_all
from swe_loop.store import Store, load_tickets

ROOT = Path(__file__).resolve().parents[1]
CFG = TargetConfig.load(ROOT / "configs" / "superset-pandas3.yaml")
TICKETS = ROOT / "data" / "inventory" / "2026-09-03" / "tickets.json"


def seeded(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    load_tickets(st, TICKETS)
    route_all(st, CFG)
    return st


def test_repair_playbook_has_the_six_sections_and_the_template_steps():
    pb = load_playbook(
        ROOT / "playbooks" / "repair-pandas3.md", ROOT / "schemas" / "repair_result.schema.json"
    )
    for section in (
        "## Overview",
        "## Procedure",
        "## Specifications",
        "## Advice and Pointers",
        "## Forbidden Actions",
        "## Required from User",
    ):
        assert section in pb.body
    assert "Do not edit any file under `tests/`" in pb.body
    assert "lower bound does not move" in pb.body
    assert pb.to_payload()["structured_output_schema"]["title"] == "swe-loop repair result"


def test_result_schema_validates_a_good_report_and_rejects_padding():
    v = Draft7Validator(load_result_schema())
    good = {
        "shard": "D",
        "self_reported_done": True,
        "files_changed": ["superset/models/helpers.py"],
        "call_sites_fixed": [
            {"file": "superset/models/helpers.py", "line": 345, "change": "utc=True"}
        ],
        "tests_run": 1,
        "tests_passed": 1,
        "acceptance": {"p2": 0, "p3": 0},
        "pr_url": "https://github.com/charliebachg/superset/pull/7",
        "needs_human": [],
    }
    assert list(v.iter_errors(good)) == []
    assert list(v.iter_errors({**good, "confidence": 0.9}))  # no self-assessed confidence field
    assert list(v.iter_errors({**good, "pr_url": "not a url"}))


def test_knowledge_notes_have_triggers_and_cover_the_conventions():
    notes = load_notes()
    assert len(notes) == 7  # six conventions plus the test-environment recipe
    assert all(n.trigger_description and n.body for n in notes)
    names = " ".join(n.name.lower() for n in notes)
    for word in ("ruff", "oxlint", "uv", "conventional", "license", "pandas 3"):
        assert word in names
    payload = notes[0].to_payload()
    assert set(payload) == {"name", "trigger", "body"}


def test_repair_spec_carries_sites_constraints_and_cap(tmp_path):
    st = seeded(tmp_path)
    wo = st.work_orders_for("tkt_A")[0]
    spec = build_repair_spec(wo, st.get_ticket("tkt_A"), CFG, review="required")
    p = spec.to_payload()
    assert p["max_acu_limit"] == 6 and p["repos"] == ["charliebachg/superset"]
    assert p["structured_output_required"] is True
    assert p["tags"][0] == "swe-loop" and p["tags"][1] == f"wo:{wo['id']}"
    assert {"repair", "target:superset-pandas3", "tkt_A", "shard:A"} <= set(p["tags"])
    prompt = p["prompt"]
    assert "client_processing.py:639" in prompt and "client_processing.py:754" in prompt
    assert ">=2.3.3, <3.1" in prompt and "lower bound does not move" in prompt
    assert "tests/" in prompt and "ruff" in prompt and "fix(pandas)" in prompt
    assert (
        "Review is required for this shard" in prompt
    )  # review required adds the explanation requirement
    assert "pandas_3_0_5" in prompt and "pandas_2_3_3_warnings_as_errors" in prompt


def test_dispatch_reserves_before_start_and_is_idempotent(tmp_path):
    st = seeded(tmp_path)
    client = DevinClient(FakeTransport(tmp_path))
    wo = st.work_orders_for("tkt_D")[0]
    sid = dispatch(st, client, wo, CFG)
    s = st.get_session(sid)
    assert s["devin_session_id"].startswith("fake-") and s["status"] == "new"
    assert st.get_ticket("tkt_D")["status"] == "dispatched"
    assert st.get_work_order(wo["id"])["status"] == "dispatched"
    # order on the wire: the tag pre-check, then the create. The store row already existed before either.
    assert [k for k, _ in client.t.calls][:2] == ["list_sessions", "create_session"]
    sent = client.t.calls[1][1]
    assert sent["max_acu_limit"] == 6 and sent["repos"] == ["charliebachg/superset"]
    # dispatching again while the session is live returns the same row
    assert dispatch(st, client, wo, CFG) == sid
    assert len([c for c in client.t.calls if c[0] == "create_session"]) == 1


def test_repair_prompt_carries_the_prescribed_fix_and_marks_gate_only_commands(tmp_path):
    from swe_loop.dispatch import build_repair_prompt, gate_only
    from swe_loop.store import Store

    st = Store(tmp_path / "t.sqlite")
    verdict = {
        "summary": "One site; semantic; preserve wall clock.",
        "review": "required",
        "sites": [
            {
                "file": "superset/models/helpers.py",
                "line": 345,
                "class": "to_datetime-mixed-tz",
                "kind": "semantic",
                "prescribed_fix": "Do NOT take utc=True; fall back to element-wise pd.Timestamp parsing.",
            }
        ],
        "acceptance_cmd": {
            "p3": "true",
            "detector": "REPO_ROOT=$PWD python -m swe_loop.detect.pandas_warnings",
        },
    }
    st.upsert_ticket(
        id="tkt_X", source="manual", title="x", status="triaged", triage_verdict=verdict
    )
    wid = st.insert_work_order(
        ticket_id="tkt_X",
        shard_id="X",
        files=["superset/models/helpers.py"],
        tests=["t"],
        acceptance=verdict["acceptance_cmd"],
    )
    wo = st.get_work_order(wid)
    prompt = build_repair_prompt(wo, st.get_ticket("tkt_X"), CFG, "required")
    assert "Prescribed fix from triage: Do NOT take utc=True" in prompt
    assert "(semantic)" in prompt and "Triage verdict: One site" in prompt
    assert (
        "Review is required for this shard" in prompt
        and "did not fail on the new one" not in prompt
    )
    assert (
        "gate only" in prompt
        and gate_only(verdict["acceptance_cmd"]["detector"])
        and not gate_only("true")
    )


def test_absolutize_command_runs_our_tooling_with_the_gate_interpreter():
    import sys

    from swe_loop.gate import absolutize_command

    real = absolutize_command(
        "REPO_ROOT=$PWD python -m swe_loop.detect.pandas_warnings", Path("/repo")
    )
    assert sys.executable in real and "swe_loop.detect.pandas_warnings" in real
    assert absolutize_command(".venv-p3/bin/python -m pytest x", Path("/repo")).startswith(
        "/repo/.venv-p3/bin/python"
    )
