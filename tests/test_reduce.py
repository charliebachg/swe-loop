from pathlib import Path

import pytest

from swe_loop.config import TargetConfig
from swe_loop.devin import DevinClient, FakeTransport
from swe_loop.dispatch import dispatch
from swe_loop.poll import Poller
from swe_loop.reduce import detect_conflicts, readiness, record_merge, summary
from swe_loop.router import route_all
from swe_loop.store import Store, load_tickets

ROOT = Path(__file__).resolve().parents[1]
CFG = TargetConfig.load(ROOT / "configs" / "superset-pandas3.yaml")
TICKETS = ROOT / "data" / "inventory" / "2026-09-03" / "tickets.json"


def run_to_gated(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    load_tickets(st, TICKETS)
    route_all(st, CFG)
    client = DevinClient(FakeTransport(tmp_path))
    poller = Poller(st, client, CFG, sleep=lambda s: None, clock=lambda: 0.0)
    sids = {}
    for t in ("tkt_A", "tkt_B", "tkt_C", "tkt_D"):
        wo = st.work_orders_for(t)[0]
        sids[t] = dispatch(st, client, wo, CFG)
        poller.wait(sids[t])
    return st, sids


def pass_and_review(st, sid, ticket, review="completed:no issues"):
    """What the gate and the review read-back leave behind: a passing verdict carrying the
    review's outcome, and the ticket marked reviewed."""
    st.insert_verdict(
        session_id=sid,
        gate_result="pass",
        decision="pass",
        reason="t",
        tree_hash="abc",
        review_severity=review,
    )
    st.set_ticket_status(ticket, "reviewed")


def test_readiness_needs_every_shard_verified_and_reviewed(tmp_path):
    st, sids = run_to_gated(tmp_path)
    r = readiness(st, "tkt_D")
    assert r.shards == 1 and r.verified == 0 and not r.ready
    pass_and_review(st, sids["tkt_D"], "tkt_D")
    r = readiness(st, "tkt_D")
    assert (
        r.ready
        and r.pr_urls
        and r.pr_urls[0].startswith("https://github.com/charliebachg/superset/pull/")
    )


def test_not_ready_while_the_review_is_still_running(tmp_path):
    """A passing gate is not enough: until Devin Review comes back, nobody is asked to merge."""
    st, sids = run_to_gated(tmp_path)
    pass_and_review(st, sids["tkt_D"], "tkt_D", review="requested:r1")
    assert readiness(st, "tkt_D").verified == 1
    assert not readiness(st, "tkt_D").ready
    with pytest.raises(ValueError):
        record_merge(st, "tkt_D", actor="someone")
    st.conn.execute("UPDATE verdicts SET review_severity='completed:no issues'")
    assert readiness(st, "tkt_D").ready


def test_merge_is_recorded_by_a_person_and_never_before_ready(tmp_path):
    st, sids = run_to_gated(tmp_path)
    with pytest.raises(ValueError):
        record_merge(st, "tkt_D", actor="someone")
    pass_and_review(st, sids["tkt_D"], "tkt_D")
    out = record_merge(st, "tkt_D", actor="someone@example.com")
    assert st.get_ticket("tkt_D")["status"] == "merged"
    assert st.work_orders_for("tkt_D")[0]["status"] == "merged"
    h = st._all("SELECT * FROM human_actions WHERE ticket_id='tkt_D'")
    assert len(h) == 1 and h[0]["kind"] == "merge"
    assert "someone" not in h[0]["actor_hash"]  # hashed, never the address
    assert st.metrics()["verified_changes"]["n"] == 1
    assert out["pr_urls"]


def test_cross_shard_conflict_is_its_own_escalation(tmp_path):
    st, _ = run_to_gated(tmp_path)
    assert detect_conflicts(st) == []  # the seeded shards are disjoint by construction
    st.upsert_ticket(id="tkt_X", source="manual", title="x", status="routed")
    st.insert_work_order(
        ticket_id="tkt_X",
        shard_id="X",
        files=["superset/models/helpers.py"],
        tests=["t"],
        acceptance={"p": "x"},
    )
    found = detect_conflicts(st)
    assert len(found) == 1 and found[0]["file"] == "superset/models/helpers.py"
    kinds = [(e["ticket_id"], e["kind"]) for e in st.list_escalations()]
    assert ("tkt_D", "conflict") in kinds and ("tkt_X", "conflict") in kinds
    detect_conflicts(st)  # idempotent
    assert sum(1 for e in st.list_escalations() if e["kind"] == "conflict") == 2
    assert (
        readiness(st, "tkt_D").conflicts == []
    )  # within-ticket conflicts only; cross-ticket is above


def test_summary_buckets(tmp_path):
    st, sids = run_to_gated(tmp_path)
    pass_and_review(st, sids["tkt_A"], "tkt_A")
    record_merge(st, "tkt_A", actor="a")
    pass_and_review(st, sids["tkt_B"], "tkt_B")
    s = summary(st)
    assert s["merged"] == ["tkt_A"] and s["ready"] == ["tkt_B"]
    assert {w["ticket_id"] for w in s["waiting"]} == {"tkt_C", "tkt_D"}


def test_merge_goes_to_github_then_is_recorded(tmp_path):
    """The button merges the pull request and records who asked. If GitHub refuses, nothing is
    recorded here either, so the two can never disagree."""
    from swe_loop import reduce as rd

    st, sids = run_to_gated(tmp_path)
    pass_and_review(st, sids["tkt_D"], "tkt_D")
    calls = []

    def ok(url, body):
        calls.append((url, body))
        return {"merged": True, "sha": "abc1234567890"}

    out = rd.merge_on_github(st, "tkt_D", "tok", request=ok)
    assert out and out[0]["merged"] and out[0]["sha"] == "abc1234567"
    assert calls[0][0].endswith("/merge") and "api.github.com/repos/" in calls[0][0]
    assert calls[0][1] == {"merge_method": "squash"}
    assert (
        st._one("SELECT pr_state FROM sessions WHERE id=?", sids["tkt_D"])["pr_state"] == "merged"
    )
    assert "merged on GitHub" in " ".join(e["event"] for e in st.timeline(ticket_id="tkt_D"))


def test_a_refused_merge_is_reported_and_changes_nothing(tmp_path):
    from swe_loop import reduce as rd

    st, sids = run_to_gated(tmp_path)
    pass_and_review(st, sids["tkt_D"], "tkt_D")
    out = rd.merge_on_github(
        st, "tkt_D", "tok", request=lambda u, b: {"message": "Pull Request is not mergeable"}
    )
    assert out[0]["merged"] is False and "not mergeable" in out[0]["why"]
    assert st.get_ticket("tkt_D")["status"] != "merged"
    assert not st._all("SELECT * FROM human_actions WHERE ticket_id='tkt_D'")


def test_the_change_itself_is_readable_before_merging(tmp_path):
    """Whoever merges should see the lines, not just that the checks passed."""
    from swe_loop import reduce as rd

    payload = [
        {
            "filename": "superset/models/helpers.py",
            "additions": 5,
            "deletions": 2,
            "patch": "@@ -1 +1 @@\n-old\n+new",
        }
    ]
    seen = []
    files = rd.pr_files(
        "https://github.com/o/r/pull/7", "tok", fetch=lambda u: (seen.append(u), payload)[1]
    )
    assert seen[0] == "https://api.github.com/repos/o/r/pulls/7/files?per_page=50"
    assert files[0]["name"].endswith("helpers.py") and files[0]["added"] == 5
    assert "+new" in files[0]["patch"]
    # a refusal is shown, never swallowed
    bad = rd.pr_files("https://github.com/o/r/pull/7", "", fetch=lambda u: {"message": "Not Found"})
    assert bad[0]["error"] == "Not Found"
