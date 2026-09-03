import json
from pathlib import Path

from swe_loop.config import TargetConfig
from swe_loop.devin import DevinClient, FakeTransport
from swe_loop.dispatch import dispatch, identity_tags
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


def calls(client, kind):
    return [c[1] for c in client.t.calls if c[0] == kind]


def test_happy_path_records_the_claim_and_hands_to_the_gate(tmp_path):
    st, client, poller, wo, ticks = setup(tmp_path)
    sid = dispatch(st, client, wo, CFG)
    out = poller.wait(sid)
    assert out.kind == "finished"
    s = st.get_session(sid)
    assert s["terminal_at"] and s["self_reported_done"] == 1 and s["pull_request_url"]
    assert s["session_size"] == "S" and s["acus_consumed"] == 2.1
    assert st.get_ticket("tkt_D")["status"] == "gated"
    assert ticks["t"] == 15.0  # backoff 5 then 10 before the terminal poll
    # insights were fetched for this session only, not the whole org
    assert calls(client, "list_insights") == [[s["devin_session_id"]]]


def test_terminal_row_is_never_processed_twice(tmp_path):
    st, client, poller, wo, _ = setup(tmp_path)
    sid = dispatch(st, client, wo, CFG)
    poller.wait(sid)
    n_esc = len(st.list_escalations(unresolved_only=False))
    n_verdicts = len(st._all("SELECT id FROM verdicts"))
    again = poller.poll_once(sid)
    assert again.kind == "already_terminal"
    assert len(st.list_escalations(unresolved_only=False)) == n_esc
    assert len(st._all("SELECT id FROM verdicts")) == n_verdicts
    assert st.get_ticket("tkt_D")["status"] == "gated"


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
    assert not calls(client, "send_message")


def test_waiting_for_user_answered_once_from_the_seam_then_escalated(tmp_path):
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
    msgs = [t for _, t in calls(client, "send_message")]
    assert len(msgs) == 1 and "superset/models/helpers.py" in msgs[0]
    assert "tests/, .github/" in msgs[0]  # the seam's forbidden paths, not a hardcoded list
    kinds = [e["kind"] for e in st.list_escalations(unresolved_only=False)]
    assert kinds.count("waiting_for_user") == 2
    assert st.get_session(sid)["terminal_at"] is None  # parked, not dead


def test_waiting_for_approval_goes_straight_to_a_person(tmp_path):
    st, client, poller, wo, _ = setup(
        tmp_path, [{"status": "running", "status_detail": "waiting_for_approval"}]
    )
    sid = dispatch(st, client, wo, CFG)
    assert poller.wait(sid).kind == "needs_human"
    assert not calls(client, "send_message")


def test_finished_without_structured_output_is_a_failure(tmp_path):
    st, client, poller, wo, _ = setup(
        tmp_path, [{"status": "exit", "status_detail": "finished", "acus_consumed": 1.2}]
    )
    sid = dispatch(st, client, wo, CFG)
    assert poller.wait(sid).kind == "failed_no_output"
    v = st.latest_verdict(sid)
    assert v["gate_result"] == "missing_evidence" and v["decision"] == "escalate"
    assert st.get_session(sid)["self_reported_done"] == 0


def test_wall_clock_terminates_archives_and_the_fake_shows_terminated(tmp_path):
    st, client, poller, wo, _ = setup(
        tmp_path, [{"status": "running", "status_detail": "working"}] * 50
    )
    sid = dispatch(st, client, wo, CFG)
    assert poller.wait(sid).kind == "timeout"
    term = calls(client, "terminate")
    assert term and term[0][1] is True  # archive=True
    s = st.get_session(sid)
    assert s["status_detail"] == "terminated" and s["terminal_at"]
    assert client.status(s["devin_session_id"]).status_detail == "terminated"  # not "finished"
    assert st.list_escalations()[-1]["reason"].startswith("wall clock")


def test_terminate_is_best_effort(tmp_path):
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
    client.t.fail_terminate.add(st.get_session(sid)["devin_session_id"])
    stopped = poller.enforce_budget()
    assert stopped == [sid]
    e = st.list_escalations()[-1]
    assert e["kind"] == "budget" and "terminate call failed: 404" in e["reason"]
    assert st.get_session(sid)["terminal_at"]  # marked terminal locally regardless
    assert poller.enforce_budget() == []  # nothing live is left to terminate


def test_retry_uses_its_own_counter_and_rejects_the_stale_claim(tmp_path):
    st, client, poller, wo, _ = setup(tmp_path)
    sid = dispatch(st, client, wo, CFG)
    assert poller.wait(sid).kind == "finished"
    assert poller.retry_with_failure(sid, "FAILED tests/x.py::t - AssertionError")
    s = st.get_session(sid)
    assert s["attempt"] == 1 and s["retries"] == 1 and s["terminal_at"] is None
    assert s["rejected_output_digest"]
    # the fake still returns the old terminal state: the poller must not accept it again
    out = poller.poll_once(sid)
    assert out.kind == "running" and "rejected" in out.detail
    # a new claim arrives
    dev = s["devin_session_id"]
    tl = client.t._sessions[dev]["fixture"]["timeline"]
    tl.append({**tl[-1], "structured_output": {**GOOD_OUT, "tests_passed": 2}})
    client.t._sessions[dev]["i"] = len(tl) - 1
    assert poller.poll_once(sid).kind == "finished"
    assert poller.retry_with_failure(sid, "still failing")
    assert not poller.retry_with_failure(sid, "third time")
    sent = [t for _, t in calls(client, "send_message")]
    assert len(sent) == 2 and "AssertionError" in sent[0] and "clean checkout" in sent[0]


def test_adoption_is_keyed_by_work_order_not_shard(tmp_path):
    st, client, _poller, wo, _ = setup(tmp_path)
    # a second ticket whose work order reuses the shard letter D
    st.upsert_ticket(id="tkt_other", source="manual", title="other", status="routed")
    wo2_id = st.insert_work_order(
        ticket_id="tkt_other", shard_id="D", files=["x.py"], tests=["t.py"], acceptance={"p3": "x"}
    )
    wo2 = st.get_work_order(wo2_id)
    a = dispatch(st, client, wo, CFG)
    b = dispatch(st, client, wo2, CFG)
    assert a != b
    assert len(calls(client, "create_session")) == 2
    assert st.get_session(a)["devin_session_id"] != st.get_session(b)["devin_session_id"]


def test_reserved_row_is_reconciled_then_a_waiting_session_is_adopted(tmp_path):
    st, client, _poller, wo, _ = setup(tmp_path)
    # crash after create, before bind: a reserved row exists and a session parked on a question
    # carries the work order's tag on the org
    tags = identity_tags(CFG, wo) + ["repair", "shard:D"]
    fixture(
        tmp_path,
        [
            {"status": "running", "status_detail": "waiting_for_user"},
            {"status": "exit", "status_detail": "finished", "structured_output": GOOD_OUT},
        ],
        tags=tuple(tags),
        sid="devin-parked",
    )
    client = DevinClient(FakeTransport(tmp_path))
    client.t.create_session({"tags": tags, "repos": [CFG.repo]})
    reserved = st.reserve_session(work_order_id=wo["id"], playbook_id=None, tags=tags)
    sid = dispatch(st, client, wo, CFG)
    assert sid == reserved  # the reserved row was bound, not returned unbound
    assert st.get_session(sid)["devin_session_id"] == "devin-parked"
    assert len(calls(client, "create_session")) == 1  # nothing new was created


def test_reconcile_adopts_a_session_that_finished_during_the_outage(tmp_path):
    st, client, poller, wo, _ = setup(tmp_path)
    tags = identity_tags(CFG, wo) + ["repair", "shard:D"]
    fixture(
        tmp_path,
        [
            {
                "status": "exit",
                "status_detail": "finished",
                "structured_output": GOOD_OUT,
                "acus_consumed": 1.5,
            }
        ],
        tags=tuple(tags),
        sid="devin-done",
    )
    client = DevinClient(FakeTransport(tmp_path))
    poller = Poller(st, client, CFG, sleep=lambda s: None, clock=lambda: 0.0)
    client.t.create_session({"tags": tags, "repos": [CFG.repo]})
    reserved = st.reserve_session(work_order_id=wo["id"], playbook_id=None, tags=tags)
    assert poller.reconcile_reserved() == {reserved: "bound"}
    assert poller.poll_once(reserved).kind == "finished"  # its claim is recovered, not lost


def test_reconcile_orphans_when_nothing_matches(tmp_path):
    st, _client, poller, wo, _ = setup(tmp_path)
    sid = st.reserve_session(work_order_id=wo["id"], playbook_id=None, tags=["swe-loop", "wo:nope"])
    assert poller.reconcile_reserved() == {sid: "orphaned"}
    assert st.get_session(sid)["status"] == "orphaned" and st.get_session(sid)["terminal_at"]


def test_adoption_never_takes_a_session_bound_elsewhere(tmp_path):
    st, client, poller, wo, _ = setup(
        tmp_path, [{"status": "running", "status_detail": "working"}] * 50
    )
    first = dispatch(st, client, wo, CFG)
    poller.wait(first)  # times out and terminates it locally
    second = dispatch(st, client, wo, CFG, attempt=2)
    assert second != first
    assert st.get_session(second)["devin_session_id"] != st.get_session(first)["devin_session_id"]


def test_per_session_cap_from_the_budget_is_honoured(tmp_path):
    st, client, _poller, wo, _ = setup(tmp_path)
    st.set_budget(acu_cap=300, per_session_cap=3)
    dispatch(st, client, wo, CFG)
    assert calls(client, "create_session")[0]["max_acu_limit"] == 3
