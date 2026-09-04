"""The Run button does the whole loop: the repository's issues become tickets, a triage session
scopes each, code routes, repair sessions run, the run is recorded with the tickets it created.
In replay the sessions are simulated and the gate is skipped; the shape is the same."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from swe_loop import runner
from swe_loop.app import build_app
from swe_loop.config import Settings, TargetConfig
from swe_loop.store import Store

ROOT = Path(__file__).resolve().parents[1]
CFG = TargetConfig.load(ROOT / "configs" / "superset-pandas3.yaml")


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    monkeypatch.delenv("DEVIN_API_KEY", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    st = Store(tmp_path / "fresh.sqlite")
    app = build_app(Settings.from_env(), st, seed_replay=False)
    with TestClient(app) as c:
        yield c, st


def test_run_pulls_issues_triages_and_dispatches(fresh):
    c, st = fresh
    assert st.list_tickets() == []
    assert c.post("/automations/auto_repair/run").status_code == 200
    c.app.state.run_thread.join(120)
    assert not c.app.state.run_thread.is_alive()
    ids = {t["id"] for t in st.list_tickets()}
    assert ids == {"tkt_A", "tkt_B", "tkt_C", "tkt_D", "tkt_E"}  # the fork's five issues
    res = st.get_automation("auto_repair")["last_result"]
    assert res["issues"] == 5 and sorted(res["new_tickets"]) == sorted(ids)
    assert res["triaged"] == 5 and "dispatched" in res
    runs = st.list_automation_runs("auto_repair")
    assert len(runs) == 1 and runs[0]["status"] == "done" and runs[0]["finished_at"]
    # every ticket left `new`: a triage session read each one
    assert all(t["status"] != "new" for t in st.list_tickets())
    assert len(st.list_triage_sessions()) == 5
    # the page shows the run and the tickets it created
    html = c.get("/automations?open=auto_repair").text
    assert "5 issues found, 5 new tickets" in html and "5 tickets scoped" in html
    assert "tkt_A" in html and "tickets it created" in html
    # a second run finds nothing new and touches no ticket
    before = {t["id"]: t["status"] for t in st.list_tickets()}
    c.post("/automations/auto_repair/run")
    c.app.state.run_thread.join(120)
    res2 = st.get_automation("auto_repair")["last_result"]
    assert res2["known"] == 5 and res2["new_tickets"] == []
    assert {t["id"]: t["status"] for t in st.list_tickets()} == before
    assert len(st.list_automation_runs("auto_repair")) == 2


def test_live_fetch_maps_issue_numbers_to_shard_letters(tmp_path):
    st = Store(tmp_path / "s.sqlite")
    issues = [
        {"number": 4, "title": "D", "body": "", "labels": [{"name": "swe-loop"}], "html_url": "u4"},
        {
            "number": 42,
            "title": "X",
            "body": "",
            "labels": [{"name": "swe-loop"}],
            "html_url": "u42",
        },
        {
            "number": 5,
            "title": "PR",
            "body": "",
            "labels": [{"name": "swe-loop"}],
            "pull_request": {},
        },
    ]
    calls = []

    def fetch(url, headers):
        calls.append((url, headers))
        return issues

    got = runner.fetch_issues("charliebachg/superset", "swe-loop", "tok", fetch)
    assert len(got) == 2 and "labels=swe-loop" in calls[0][0]
    assert calls[0][1]["Authorization"] == "Bearer tok"
    out = runner.intake_issues(st, CFG, got, source_repo="charliebachg/superset")
    assert sorted(out["new"]) == ["tkt_D", "tkt_is42"]  # a known number keeps its letter
    assert st.get_ticket("tkt_D")["status"] == "new"
    # the same issues again: nothing new
    again = runner.intake_issues(st, CFG, got, source_repo="charliebachg/superset")
    assert again["new"] == [] and again["known"] == 2


def test_pages_poll_while_something_runs(fresh):
    c, st = fresh
    assert "hx-trigger" not in c.get("/").text
    st.upsert_ticket(id="tkt_Z", source="manual", title="z", status="dispatched")
    st.insert_work_order(
        ticket_id="tkt_Z", shard_id="Z", files=["a.py"], tests=[], acceptance={"p": "true"}
    )
    wo = st.work_orders_for("tkt_Z")[0]
    sid = st.reserve_session(work_order_id=wo["id"], playbook_id=None, tags=["swe-loop"])
    st.bind_devin_session(
        sid, devin_session_id="d1", url="https://app.devin.ai/sessions/d1", status="running"
    )
    for path in ("/", "/tickets-page", "/automations", "/devin/sessions"):
        html = c.get(path).text
        assert 'hx-trigger="every 1s"' in html, path  # a session is working: follow it closely
    # the tickets page says what the ticket is doing, and links the live session
    html = c.get("/tickets-page").text
    assert "The AI is working on the fix" in html and "open the session" in html
    assert 'href="https://app.devin.ai/sessions/d1"' in html
