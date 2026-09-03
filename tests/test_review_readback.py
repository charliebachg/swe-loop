"""The gate requests Devin Review; the loop reads the result back so the pages can show it."""

from swe_loop.devin import DevinClient, FakeTransport
from swe_loop.reduce import _github_review_outcome, refresh_reviews
from swe_loop.store import Store

BOT = "devin-ai-integration[bot]"


def _seed(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    st.upsert_ticket(id="tkt_D", source="inventory", title="d", status="reviewed")
    wid = st.insert_work_order(
        ticket_id="tkt_D", shard_id="D", files=["a.py"], tests=["t"], acceptance={"p3": "true"}
    )
    sid = st.reserve_session(work_order_id=wid, playbook_id=None, tags=["x"], attempt=1)
    st.bind_devin_session(
        sid, devin_session_id="dev-1", url="https://app.devin.ai/sessions/dev-1", status="exit"
    )
    st.update_session(sid, pull_request_url="https://github.com/o/r/pull/7")
    st.insert_verdict(
        session_id=sid,
        gate_result="pass",
        review_severity="requested:n/a",
        decision="pass",
        reason="ok",
    )
    return st, sid


def test_refresh_reviews_records_no_issues(tmp_path):
    st, sid = _seed(tmp_path)
    pages = {
        "https://api.github.com/repos/o/r/pulls/7/reviews": [
            {"user": {"login": BOT}, "body": "## Devin Review: No Issues Found"}
        ],
        "https://api.github.com/repos/o/r/pulls/7/comments": [],
    }
    n = refresh_reviews(st, DevinClient(FakeTransport()), "tok", fetch=lambda u: pages[u])
    assert n == 1
    assert st.latest_verdict(sid)["review_severity"] == "completed:no issues"
    assert any(
        e["layer"] == "L6 review" and "no issues" in e["event"]
        for e in st.timeline(ticket_id="tkt_D", limit=20)
    )
    assert (
        refresh_reviews(st, DevinClient(FakeTransport()), "tok", fetch=lambda u: pages[u]) == 0
    )  # once


def test_refresh_reviews_counts_comments_and_survives_no_token(tmp_path):
    st, sid = _seed(tmp_path)
    pages = {
        "https://api.github.com/repos/o/r/pulls/7/reviews": [
            {"user": {"login": BOT}, "body": "found things"}
        ],
        "https://api.github.com/repos/o/r/pulls/7/comments": [
            {"user": {"login": BOT}, "body": "x"},
            {"user": {"login": BOT}, "body": "y"},
        ],
    }
    assert (
        _github_review_outcome("https://github.com/o/r/pull/7", "tok", fetch=lambda u: pages[u])
        == "2 comment(s)"
    )
    assert (
        _github_review_outcome("https://github.com/o/r/pull/7", "", fetch=lambda u: pages[u])
        is None
    )
    assert refresh_reviews(st, DevinClient(FakeTransport()), "") == 1
    assert st.latest_verdict(sid)["review_severity"] == "completed:see the pull request"


def test_home_lists_the_mergers_notes(tmp_path, monkeypatch):
    import json

    from fastapi.testclient import TestClient

    from swe_loop.app import build_app
    from swe_loop.config import Settings
    from swe_loop.reduce import merge_notes

    monkeypatch.delenv("DEVIN_API_KEY", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    st, sid = _seed(tmp_path)
    st.set_budget(acu_cap=40, per_session_cap=6)
    st.update_session(
        sid,
        structured_output_json=json.dumps(
            {"needs_human": [{"site": "boxplot.py:138", "reason": "root cause outside the shard"}]}
        ),
    )
    st.conn.execute("UPDATE verdicts SET review_severity='completed:3 comment(s)'")
    st.conn.commit()
    mn = merge_notes(st, "tkt_D")
    assert mn["reviews"] == ["PR #7: 3 comment(s)"] and mn["notes"][0]["site"] == "boxplot.py:138"
    app = build_app(Settings.from_env(), st, seed_replay=False)
    with TestClient(app) as c:
        html = c.get("/").text
    assert (
        "Devin Review PR #7: 3 comment(s)" in html
        and "boxplot.py:138: root cause outside the shard" in html
    )
    assert "read first: the session left 1 note(s)" in html
