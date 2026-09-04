"""Recording a run captures every table the pages read and leaves no local path behind."""

import json
import os
import tempfile
from pathlib import Path

from swe_loop.replay import TABLES, record, restore
from swe_loop.store import Store


def test_record_covers_triage_and_redacts_local_paths(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    st.upsert_ticket(id="tkt_D", source="inventory", title="d", status="triaged")
    tid = st.insert_triage_session(
        ticket_id="tkt_D",
        devin_session_id="dev-t",
        url="u",
        status="exit",
        status_detail="finished",
        playbook_id=None,
        tags=["triage"],
    )
    wid = st.insert_work_order(
        ticket_id="tkt_D", shard_id="D", files=["a.py"], tests=["t"], acceptance={"p3": "true"}
    )
    sid = st.reserve_session(work_order_id=wid, playbook_id=None, tags=["x"], attempt=1)
    root = Path(__file__).resolve().parents[1]
    fork = (root.parent / "superset-fork").resolve()
    st.insert_evidence(
        session_id=sid,
        tier="T1",
        command=f"lint: {fork}/.venv-p2/bin/ruff check a.py",
        cwd=f"{tempfile.gettempdir()}/swe-loop-gate-abc",
        tree_hash="abc",
        exit_code=0,
        output="ok",
        output_path=f"{root}/data/live/evidence/x.log",
        passed=True,
    )
    st.log(
        "triage",
        "answered by a person",
        ticket_id="tkt_D",
        detail=f"see {os.path.expanduser('~')}/notes",
    )
    out = tmp_path / "run.json"
    counts = record(st, out)
    assert (
        "triage_sessions" in TABLES and counts["triage_sessions"] == 1 and counts["timeline"] >= 1
    )
    text = out.read_text()
    assert str(root) not in text and str(fork) not in text and os.path.expanduser("~") not in text
    assert "../superset-fork/.venv-p2/bin/ruff" in text and "./data/live/evidence/x.log" in text
    assert tempfile.gettempdir() not in text and "<tmp>/swe-loop-gate-abc" in text
    st2 = Store(tmp_path / "r.sqlite")
    restore(st2, out)
    assert st2.get_triage_session(tid)["ticket_id"] == "tkt_D"
    assert st2.timeline(ticket_id="tkt_D", limit=5)


def test_a_session_can_be_given_its_structured_output_by_name(tmp_path):
    """update_session offers `structured_output` as a convenience and stores it as JSON. The
    name check used to run before the conversion, so the convenience could never be used: the
    caller-facing name is not in the allow-list and every call with it was rejected."""
    st = Store(tmp_path / "s.sqlite")
    st.upsert_ticket(id="tkt_A", source="github", title="t", status="new")
    wo = st.insert_work_order(
        ticket_id="tkt_A", shard_id="A", files=["a.py"], tests=[], acceptance={}
    )
    sid = st.reserve_session(work_order_id=wo, playbook_id=None, tags=[])
    st.update_session(sid, structured_output={"pr_url": "u", "self_reported_done": True})
    assert json.loads(st.get_session(sid)["structured_output_json"])["pr_url"] == "u"
