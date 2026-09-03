import json
from pathlib import Path

from swe_loop.config import TargetConfig
from swe_loop.devin import DevinClient, FakeTransport
from swe_loop.dispatch import dispatch
from swe_loop.poll import Poller
from swe_loop.router import route_all
from swe_loop.store import Store, load_tickets

ROOT = Path(__file__).resolve().parents[1]
CFG = TargetConfig.load(ROOT / "configs" / "superset-pandas3.yaml")
TICKETS = ROOT / "data" / "inventory" / "2026-09-03" / "tickets.json"

GOOD_OUT = {
    "shard": "D",
    "self_reported_done": True,
    "files_changed": ["superset/models/helpers.py"],
    "call_sites_fixed": [{"file": "superset/models/helpers.py", "line": 345, "change": "utc=True"}],
    "tests_run": 1,
    "tests_passed": 1,
    "pr_url": "https://github.com/charliebachg/superset/pull/7",
    "needs_human": [],
}


def fixture(tmp_path, timeline, tags=("shard:D",), insights=None, sid="devin-fx"):
    (tmp_path / "sessions").mkdir(exist_ok=True)
    (tmp_path / "sessions" / f"{sid}.json").write_text(
        json.dumps(
            {
                "session_id": sid,
                "url": f"https://app.devin.ai/sessions/{sid}",
                "match_tags": list(tags),
                "timeline": timeline,
                "insights": insights or {"session_size": "S"},
            }
        )
    )


def setup(tmp_path, timeline=None, **fx):
    st = Store(tmp_path / "t.sqlite")
    load_tickets(st, TICKETS)
    route_all(st, CFG)
    if timeline:
        fixture(tmp_path, timeline, **fx)
    client = DevinClient(FakeTransport(tmp_path))
    ticks = {"t": 0.0}
    poller = Poller(
        st,
        client,
        CFG,
        sleep=lambda s: ticks.__setitem__("t", ticks["t"] + s),
        clock=lambda: ticks["t"],
        wall_clock=100,
    )
    wo = st.work_orders_for("tkt_D")[0]
    return st, client, poller, wo, ticks


def test_happy_path_records_the_claim_and_hands_to_the_gate(tmp_path):
    st, client, poller, wo, ticks = setup(tmp_path)  # synthesised 4-step timeline
    sid = dispatch(st, client, wo, CFG)
    out = poller.wait(sid)
    assert out.kind == "finished"
    s = st.get_session(sid)
    assert s["terminal_at"] and s["self_reported_done"] == 1 and s["pull_request_url"]
    assert s["session_size"] == "S" and s["acus_consumed"] == 2.1
    assert st.get_ticket("tkt_D")["status"] == "gated"
    # backoff 5 then 10: two waits before the terminal poll (create consumed the first state)
    assert ticks["t"] == 15.0


def test_too_large_escalates_and_never_retries(tmp_path):
    st, client, poller, wo, _ = setup(
        tmp_path,
        [
            {"status": "running", "status_detail": "working", "acus_consumed": 1.0},
            {"status": "exit", "status_detail": "usage_limit_exceeded", "acus_consumed": 6.0},
        ],
        insights={"session_size": "L"},
    )
    sid = dispatch(st, client, wo, CFG)
    assert poller.wait(sid).kind == "too_large"
    esc = st.list_escalations()
    assert esc[-1]["kind"] == "usage_limit" and "too large" in esc[-1]["reason"]
    assert st.get_ticket("tkt_D")["status"] == "escalated"
    assert st.get_session(sid)["session_size"] == "L"
    assert not any(k == "send_message" for k, _ in client.t.calls)


def test_waiting_for_user_answered_once_then_escalated(tmp_path):
    st, client, poller, wo, _ = setup(
        tmp_path,
        [
            {"status": "running", "status_detail": "working"},
            {"status": "running", "status_detail": "waiting_for_user"},
            {"status": "running", "status_detail": "working"},
            {"status": "running", "status_detail": "waiting_for_user"},
            {"status": "exit", "status_detail": "finished", "structured_output": GOOD_OUT},
        ],
    )
    sid = dispatch(st, client, wo, CFG)
    out = poller.wait(sid)
    assert out.kind == "needs_human" and "twice" in out.detail
    msgs = [t for k, (_, t) in [(c[0], c[1]) for c in client.t.calls if c[0] == "send_message"]]
    assert (
        len(msgs) == 1
        and "superset/models/helpers.py" in msgs[0]
        and "Do not modify tests" in msgs[0]
    )
    kinds = [e["kind"] for e in st.list_escalations(unresolved_only=False)]
    assert kinds.count("waiting_for_user") == 2
    assert st.get_ticket("tkt_D")["status"] == "escalated"


def test_waiting_for_approval_goes_straight_to_a_person(tmp_path):
    st, client, poller, wo, _ = setup(
        tmp_path,
        [
            {"status": "running", "status_detail": "waiting_for_approval"},
        ],
    )
    sid = dispatch(st, client, wo, CFG)
    assert poller.wait(sid).kind == "needs_human"
    assert not any(k == "send_message" for k, _ in client.t.calls)


def test_finished_without_structured_output_is_a_failure(tmp_path):
    st, client, poller, wo, _ = setup(
        tmp_path,
        [
            {"status": "exit", "status_detail": "finished", "acus_consumed": 1.2},
        ],
    )
    sid = dispatch(st, client, wo, CFG)
    assert poller.wait(sid).kind == "failed_no_output"
    v = st.latest_verdict(sid)
    assert v["gate_result"] == "missing_evidence" and v["decision"] == "escalate"
    assert st.get_session(sid)["self_reported_done"] == 0


def test_wall_clock_terminates_and_archives(tmp_path):
    st, client, poller, wo, _ticks = setup(
        tmp_path, [{"status": "running", "status_detail": "working"}] * 50
    )
    sid = dispatch(st, client, wo, CFG)
    assert poller.wait(sid).kind == "timeout"
    term = [c for c in client.t.calls if c[0] == "terminate"]
    assert term and term[0][1][1] is True  # archive=True
    assert st.get_session(sid)["status_detail"] == "terminated"
    assert st.list_escalations()[-1]["reason"].startswith("wall clock")


def test_retry_with_failure_text_is_capped_at_two(tmp_path):
    st, client, poller, wo, _ = setup(tmp_path)
    sid = dispatch(st, client, wo, CFG)
    poller.wait(sid)
    assert poller.retry_with_failure(sid, "FAILED tests/x.py::t - AssertionError")
    assert st.get_session(sid)["attempt"] == 2 and st.get_session(sid)["terminal_at"] is None
    assert poller.retry_with_failure(sid, "still failing")
    assert not poller.retry_with_failure(sid, "third time")  # attempt would be 4 > MAX_RETRIES + 1
    sent = [t for c in client.t.calls if c[0] == "send_message" for t in [c[1][1]]]
    assert len(sent) == 2 and "AssertionError" in sent[0] and "clean checkout" in sent[0]


def test_budget_cap_terminates_live_sessions(tmp_path):
    st, client, poller, wo, _ = setup(
        tmp_path,
        [
            {"status": "running", "status_detail": "working", "acus_consumed": 5.0},
            {"status": "running", "status_detail": "working", "acus_consumed": 5.0},
        ],
    )
    st.set_budget(acu_cap=4, per_session_cap=6)
    sid = dispatch(st, client, wo, CFG)
    poller.poll_once(sid)
    stopped = poller.enforce_budget()
    assert stopped == [sid]
    assert st.list_escalations()[-1]["kind"] == "budget"


def test_dispatch_adopts_a_live_session_with_the_same_tags(tmp_path):
    st, client, _poller, wo, _ = setup(tmp_path)
    first = dispatch(st, client, wo, CFG)
    # simulate a crash after the API call: forget our row's binding by making a fresh store view
    st2 = Store(tmp_path / "t2.sqlite")
    load_tickets(st2, TICKETS)
    route_all(st2, CFG)
    wo2 = st2.work_orders_for("tkt_D")[0]
    second = dispatch(st2, client, wo2, CFG)
    creates = [c for c in client.t.calls if c[0] == "create_session"]
    assert len(creates) == 1  # the second dispatch adopted the live session instead of creating one
    assert st2.get_session(second)["devin_session_id"] == st.get_session(first)["devin_session_id"]


def test_reconcile_reserved_rows(tmp_path):
    st, _client, poller, wo, _ = setup(tmp_path)
    sid = st.reserve_session(
        work_order_id=wo["id"], playbook_id=None, tags=["swe-loop", "shard:ZZ"]
    )
    assert poller.reconcile_reserved() == [sid]
    assert st.get_session(sid)["status"] == "orphaned"
