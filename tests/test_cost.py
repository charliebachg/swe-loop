"""Cost on a plan that reports no ACU: minutes the AI was working, from our own polls, priced by
the credits figure a person read from the console."""

from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from swe_loop import cost
from swe_loop.app import build_app
from swe_loop.config import Settings
from swe_loop.store import Store


def _session_with_polls(st, working_gaps=(20, 20, 20), waiting_gaps=(30,)):
    st.upsert_ticket(id="tkt_D", source="inventory", title="d", status="dispatched")
    wid = st.insert_work_order(
        ticket_id="tkt_D", shard_id="D", files=["a.py"], tests=["t"], acceptance={"p3": "true"}
    )
    sid = st.reserve_session(work_order_id=wid, playbook_id=None, tags=["x"], attempt=1)
    st.bind_devin_session(sid, devin_session_id="dev-1", url="u", status="running")
    t0 = datetime.fromisoformat(st.get_session(sid)["created_at"])
    t = t0
    for g in working_gaps:
        t += timedelta(seconds=g)
        st.conn.execute(
            "INSERT INTO timeline (at, layer, event, ticket_id, session_id, detail) VALUES (?,?,?,?,?,?)",
            (t.isoformat(), "poll", "running/working", "tkt_D", sid, ""),
        )
    for g in waiting_gaps:
        t += timedelta(seconds=g)
        st.conn.execute(
            "INSERT INTO timeline (at, layer, event, ticket_id, session_id, detail) VALUES (?,?,?,?,?,?)",
            (t.isoformat(), "poll", "running/waiting_for_user", "tkt_D", sid, ""),
        )
    st.conn.commit()
    st.mark_terminal(sid, status="exit", status_detail="finished", acus_consumed=0.0)
    st.conn.execute(
        "UPDATE sessions SET terminal_at=? WHERE id=?",
        ((t + timedelta(seconds=1)).isoformat(), sid),
    )
    st.conn.commit()
    return sid


def test_active_seconds_count_only_working_gaps(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    sid = _session_with_polls(st)
    s = st.get_session(sid)
    secs = cost.repair_active_seconds(st, s)
    # created -> poll1 (20 s, working), poll1 -> poll2 (20), poll2 -> poll3 (20), poll3 -> waiting (30, still working before it)
    assert 85 <= secs <= 95
    sp = cost.spend(st)
    assert sp["metered"] is False and sp["acu"] == 0 and 1.4 <= sp["active_min"] <= 1.6
    assert (
        sp["usd"] is not None
        and abs(sp["usd"] - sp["active_min_raw"] * cost.DEFAULT_RATES["rep"]) < 0.02
    )


def test_long_gaps_are_capped(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    sid = _session_with_polls(st, working_gaps=(600,), waiting_gaps=())
    assert cost.repair_active_seconds(st, st.get_session(sid)) <= 2 * cost.GAP_CAP_S


def test_credits_calibrate_minutes_into_dollars(tmp_path, monkeypatch):
    monkeypatch.delenv("DEVIN_API_KEY", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    st = Store(tmp_path / "t.sqlite")
    st.set_budget(acu_cap=40, per_session_cap=6)
    _session_with_polls(st)
    st.set_session_cost("dev-1", 3.0)
    sp = cost.spend(st)
    assert (
        sp["source"] == "console"
        and abs(sp["usd"] - 3.0) < 0.01
        and sp["rates"]["rep"] > cost.DEFAULT_RATES["rep"]
    )
    app = build_app(Settings.from_env(), st, seed_replay=False)
    with TestClient(app) as c:
        html = c.get("/settings").text
        assert "per active minute" in html and "Calibrate" in html
        r = c.post(
            "/settings/credits",
            content="credits_usd=4.5",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert r.status_code == 303 and cost.calibration(st)["credits_usd"] == 4.5
        assert (
            c.post(
                "/settings/credits",
                content="credits_usd=abc",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            ).status_code
            == 400
        )
        home = c.get("/").text
        assert "AI cost" in home and "$" in home and " est." not in home
        report = c.get("/report").text
        assert "58 min of AI work" in report or "min of AI work" in report


def test_the_consoles_figure_for_a_scan_is_not_discarded(tmp_path):
    """A scan is a session and is billed like one. It was left out of the lookup, so entering
    its figure silently did nothing and the scan stayed priced at our own guess."""
    st = Store(tmp_path / "s.sqlite")
    sid = st.insert_scan_session(
        devin_session_id="abc123def456",
        url="",
        playbook_id=None,
        tags=["scan"],
        status="running",
        status_detail="working",
    )
    assert st.set_session_cost("abc123", 3.19) == "scan_sessions"
    assert st.list_scan_sessions()[0]["cost_usd"] == 3.19
    assert st.get_session(sid) is None  # it is not a repair session, and was never treated as one
