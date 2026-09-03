"""A run waits for Devin Review and reads it back itself; with followup: auto it sends remarks
back to the session and re-gates, once, so a ticket reaches "ready for you" unattended."""

import dataclasses
from pathlib import Path

from swe_loop import cli
from swe_loop.config import Settings, TargetConfig
from swe_loop.store import Store

ROOT = Path(__file__).resolve().parents[1]
CFG = TargetConfig.load(ROOT / "configs" / "superset-pandas3.yaml")
INVENTORY = ROOT / "data" / "inventory" / "2026-09-03"


class _T:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = 0

    def get_pr_review(self, pr_url):
        self.calls += 1
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


class _Client:
    is_fake = False

    def __init__(self, answers):
        self.t = _T(answers)


def _store_with_requested_review(tmp_path):
    """The smallest store the settle step can act on: one gated ticket whose PR is under
    review."""
    st = Store(tmp_path / "s.sqlite")
    st.upsert_ticket(id="tkt_D", source="fork", title="D", status="gated")
    st.insert_work_order(
        ticket_id="tkt_D",
        shard_id="D",
        files=["superset/models/helpers.py"],
        tests=[],
        acceptance={"p3": "true"},
    )
    wo = st.work_orders_for("tkt_D")[0]
    sid = st.reserve_session(work_order_id=wo["id"], playbook_id=None, tags=["swe-loop"])
    st.bind_devin_session(
        sid, devin_session_id="dev-d", url="https://app.devin.ai/sessions/dev-d", status="running"
    )
    st.update_session(sid, pull_request_url="https://github.com/x/y/pull/7")
    st.insert_verdict(
        session_id=sid,
        gate_result="pass",
        decision="pass",
        reason="T0 clean; every acceptance command exited 0",
        tree_hash="t1",
        review_severity="requested:r1",
    )
    return st


def test_waits_then_reads_back_and_follows_up_once(tmp_path, monkeypatch):
    st = _store_with_requested_review(tmp_path)
    client = _Client(
        [{"status": "running"}, {"status": "running"}, {"status": "completed", "commit_sha": "abc"}]
    )
    monkeypatch.setattr(cli, "refresh_reviews", _refresh_with(monkeypatch, outcome="2 comment(s)"))
    calls = []

    def fake_followup(store, client_, cfg, tid, token, **kw):
        calls.append(tid)
        # the follow-up re-gates: the latest verdict is a fresh request, which then completes
        store.insert_verdict(
            session_id=store._one(
                "SELECT s.id FROM sessions s JOIN work_orders w ON w.id=s.work_order_id WHERE w.ticket_id=?",
                tid,
            )["id"],
            gate_result="pass",
            decision="pass",
            reason="re-gated after the review",
            tree_hash="t2",
            review_severity="requested:r2",
        )
        return {"kind": "resent"}

    import swe_loop.followup as fu

    monkeypatch.setattr(fu, "review_followup", fake_followup)
    slept = []
    cfg = dataclasses.replace(CFG, review={"wait_s": 600, "followup": "auto", "max_rounds": 1})
    out = cli.settle_reviews(
        Settings(mode="live", devin_api_key="x"),
        cfg,
        st,
        client,
        log=lambda m: None,
        sleep=slept.append,
        clock=_Clock(),
    )
    assert calls == ["tkt_D"]  # remarks went back once
    assert out["followups"] == 1 and out["read_back"] == 2 and not out["timed_out"]
    assert slept and all(s == 30 for s in slept)
    assert cli._pending_reviews(st) == 0


def test_no_issues_means_no_followup(tmp_path, monkeypatch):
    st = _store_with_requested_review(tmp_path)
    client = _Client([{"status": "completed", "commit_sha": "abc"}])
    monkeypatch.setattr(cli, "refresh_reviews", _refresh_with(monkeypatch, outcome="no issues"))
    import swe_loop.followup as fu

    monkeypatch.setattr(
        fu,
        "review_followup",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not follow up")),
    )
    cfg = dataclasses.replace(CFG, review={"wait_s": 600, "followup": "auto", "max_rounds": 1})
    out = cli.settle_reviews(
        Settings(mode="live", devin_api_key="x"),
        cfg,
        st,
        client,
        log=lambda m: None,
        sleep=lambda s: None,
    )
    assert out["followups"] == 0 and out["read_back"] == 1


def test_times_out_and_says_so(tmp_path, monkeypatch):
    st = _store_with_requested_review(tmp_path)
    client = _Client([{"status": "running"}])
    monkeypatch.setattr(cli, "refresh_reviews", _refresh_with(monkeypatch, outcome=None))
    cfg = dataclasses.replace(CFG, review={"wait_s": 60, "followup": "auto"})
    clock = _Clock(step=40)
    out = cli.settle_reviews(
        Settings(mode="live", devin_api_key="x"),
        cfg,
        st,
        client,
        log=lambda m: None,
        sleep=lambda s: None,
        clock=clock,
    )
    assert out["timed_out"] and cli._pending_reviews(st) == 1
    assert "read back on the next run" in " ".join(e["event"] for e in st.timeline(limit=5))


class _Clock:
    def __init__(self, step=1.0):
        self.t = 0.0
        self.step = step

    def __call__(self):
        self.t += self.step
        return self.t


def _refresh_with(monkeypatch, outcome):
    """A refresh_reviews that completes requested reviews when the client says completed."""

    def refresh(store, client, token="", fetch=None):
        n = 0
        for r in store._all(
            "SELECT v.id, s.pull_request_url AS pr FROM verdicts v JOIN sessions s ON s.id=v.session_id "
            "WHERE v.review_severity LIKE 'requested%' AND s.pull_request_url IS NOT NULL"
        ):
            if client.t.get_pr_review(r["pr"]).get("status") == "completed":
                store.conn.execute(
                    "UPDATE verdicts SET review_severity=? WHERE id=?",
                    (f"completed:{outcome}", r["id"]),
                )
                n += 1
        return n

    return refresh
