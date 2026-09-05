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


def test_record_ships_the_check_logs_scrubbed_and_leaves_the_person_out(tmp_path):
    """A recording is for a fresh clone on another machine: the check logs travel with it under
    evidence/, with this machine's paths rewritten, and every output_path points at the copy.
    The name a person typed into a merge form and the watcher's timestamps stay behind. The
    automations, their run history and Devin's analyses are part of the run and come along."""
    st = Store(tmp_path / "t.sqlite")
    st.upsert_ticket(id="tkt_Q", source="github", title="q", status="reviewed")
    wid = st.insert_work_order(
        ticket_id="tkt_Q", shard_id="Q", files=["a.py"], tests=[], acceptance={}
    )
    sid = st.reserve_session(work_order_id=wid, playbook_id=None, tags=["x"], attempt=1)
    live = tmp_path / "live"
    (live / "evidence" / sid).mkdir(parents=True)
    home = os.path.expanduser("~")
    (live / "evidence" / sid / "01-T1.log").write_text(f"ran {home}/work/superset-fork/x.py\nok\n")
    st.insert_evidence(
        session_id=sid,
        tier="T1",
        command="pytest",
        cwd="/tmp/x",
        tree_hash="abc",
        exit_code=0,
        output="ok",
        output_path=str(live / "evidence" / sid / "01-T1.log"),
        passed=True,
    )
    st.set_setting("person.name", "Somebody Real")
    st.set_setting("watch.last_tick", "2026-09-05T00:00:00+00:00")
    st.set_setting("usd_cap", "150")
    st.put_insight("dev-q", {"session_id": "dev-q", "analysis": {"issues": [{"title": "x"}]}})
    st.upsert_automation(
        id="auto_q",
        name="Q",
        kind="repair",
        enabled=True,
        availability="live",
        trigger={"source": "github", "event": "issues"},
        target="o/r",
        playbook="p",
        max_acu=6,
        concurrency=1,
        schedule=None,
        notes=None,
    )
    rid = st.start_automation_run("auto_q")
    st.finish_automation_run(rid, {"issues": 1})

    out = tmp_path / "replay" / "run.json"
    counts = record(st, out, evidence_from=live / "evidence")
    assert (
        counts["evidence_files"] == 1 and counts["insights"] == 1 and counts["automation_runs"] == 1
    )
    copied = tmp_path / "replay" / "evidence" / sid / "01-T1.log"
    assert copied.exists() and home not in copied.read_text() and "~/work" in copied.read_text()
    text = out.read_text()
    assert "Somebody Real" not in text and "watch.last_tick" not in text and '"usd_cap"' in text
    assert home not in text
    st2 = Store(tmp_path / "r.sqlite")
    restore(st2, out)
    ev = st2.evidence_for(sid)[0]
    assert ev["output_path"] == f"data/replay/evidence/{sid}/01-T1.log"
    assert st2.insight("dev-q")["analysis"]["issues"][0]["title"] == "x"
    assert st2.list_automation_runs("auto_q")[0]["result"] == {"issues": 1}
    assert st2.get_setting("person.name") is None
