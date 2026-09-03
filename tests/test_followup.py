"""Review remarks go back into the session that wrote the PR; the loop waits for a new claim."""

import json
from pathlib import Path

from swe_loop.config import TargetConfig
from swe_loop.devin import DevinClient, FakeTransport
from swe_loop.followup import compose_message, fetch_review_remarks, review_followup
from swe_loop.store import Store

ROOT = Path(__file__).resolve().parents[1]
CFG = TargetConfig.load(ROOT / "configs" / "superset-pandas3.yaml")
BOT = "devin-ai-integration[bot]"


class _Revises(FakeTransport):
    """After a message, the session works again and delivers a changed claim."""

    def send_message(self, session_id, text):
        out = super().send_message(session_id, text)
        s = self._sessions[session_id]
        last = dict(s["fixture"]["timeline"][-1])
        new = dict(last)
        new["structured_output"] = {
            **last["structured_output"],
            "notes": "revised after review",
            "tests_run": 40,
        }
        s["fixture"]["timeline"] += [
            {"status": "running", "status_detail": "working", "acus_consumed": 2.5},
            new,
        ]
        s["i"] = len(s["fixture"]["timeline"]) - 2
        return out


def _seed(tmp_path, transport):
    st = Store(tmp_path / "t.sqlite")
    st.set_budget(acu_cap=40, per_session_cap=6)
    st.upsert_ticket(
        id="tkt_B",
        source="inventory",
        title="b",
        status="triaged",
        triage_verdict={"acceptance_cmd": {"p3": "true"}, "sites": [], "split": "one"},
    )
    wid = st.insert_work_order(
        ticket_id="tkt_B", shard_id="B", files=["a.py"], tests=["t"], acceptance={"p3": "true"}
    )
    from swe_loop.dispatch import dispatch
    from swe_loop.poll import Poller

    client = DevinClient(transport)
    sid = dispatch(st, client, st.get_work_order(wid), CFG)
    Poller(st, client, CFG, sleep=lambda s: None).wait(sid)
    st.insert_verdict(
        session_id=sid,
        gate_result="pass",
        review_severity="completed:3 comment(s)",
        decision="pass",
        reason="ok",
    )
    st.set_ticket_status("tkt_B", "reviewed")
    return st, client, sid


def test_remarks_are_fetched_and_composed():
    pages = {
        "https://api.github.com/repos/o/r/pulls/8/comments": [
            {
                "user": {"login": BOT},
                "path": "utils.py",
                "line": 222,
                "body": "<b>Integer results wrap</b><p>narrows without range checks</p>",
            },
            {"user": {"login": "someone"}, "path": "x", "line": 1, "body": "ignore me"},
        ]
    }
    remarks = fetch_review_remarks("https://github.com/o/r/pull/8", "tok", fetch=lambda u: pages[u])
    assert len(remarks) == 1 and remarks[0]["path"] == "utils.py" and "<" not in remarks[0]["body"]
    msg = compose_message("https://github.com/o/r/pull/8", "swe-loop/B", remarks)
    assert (
        "1 remark(s)" in msg
        and "swe-loop/B" in msg
        and "utils.py:222" in msg
        and "is_final=true" in msg
    )
    assert fetch_review_remarks("https://github.com/o/r/pull/8", "", fetch=None) == []


def test_followup_messages_the_session_and_waits_for_a_new_claim(tmp_path):
    t = _Revises()
    st, client, sid = _seed(tmp_path, t)
    pr = st.get_session(sid)["pull_request_url"]
    pages = {
        pr.replace("https://github.com/", "https://api.github.com/repos/").replace(
            "/pull/", "/pulls/"
        )
        + "/comments": [
            {"user": {"login": BOT}, "path": "a.py", "line": 3, "body": "wraps"},
            {"user": {"login": BOT}, "path": "a.py", "line": 9, "body": "order varies"},
        ]
    }
    out = review_followup(
        st, client, CFG, "tkt_B", "tok", fetch=lambda u: pages[u], log=lambda m: None
    )
    assert out["kind"] == "revised_unverified" and out["remarks"] == 2  # fake gate is skipped
    sent = [c for c in t.calls if c[0] == "send_message"]
    assert len(sent) == 1 and "2 remark(s)" in sent[0][1][1] and "a.py:9" in sent[0][1][1]
    s = st.get_session(sid)
    assert (
        s["terminal_at"]
        and json.loads(s["structured_output_json"])["notes"] == "revised after review"
    )
    assert st.get_ticket("tkt_B")["status"] == "reviewed"
    assert any(
        e["event"].endswith("sent back to the session")
        for e in st.timeline(ticket_id="tkt_B", limit=50)
    )


def test_followup_without_remarks_does_nothing(tmp_path):
    t = _Revises()
    st, client, _sid = _seed(tmp_path, t)
    out = review_followup(st, client, CFG, "tkt_B", "tok", fetch=lambda u: [], log=lambda m: None)
    assert out["kind"] == "nothing_to_address" and not [
        c for c in t.calls if c[0] == "send_message"
    ]
