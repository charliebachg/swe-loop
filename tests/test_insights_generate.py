"""Session Insights has two halves. The classification arrives with every session; the analysis,
Devin's own account of what went wrong and what to change, is written only when asked. The page
offers Generate on a row without one, shows it as writing, then offers View, which opens the
analysis under the row. Asking is free on Devin's side and starts no session."""

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from swe_loop import insights as ins
from swe_loop.app import build_app
from swe_loop.config import Settings
from swe_loop.store import Store

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def ran(tmp_path, monkeypatch):
    monkeypatch.delenv("DEVIN_API_KEY", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    st = Store(tmp_path / "i.sqlite")
    app = build_app(Settings.from_env(), st, seed_replay=False)
    with TestClient(app) as c:
        c.post("/automations/auto_repair/run")
        c.app.state.run_thread.join(120)
        yield c, st


def _wait_idle(c, sid: str, seconds: float = 10.0) -> None:
    t0 = time.time()
    while sid in c.app.state.insight_jobs and time.time() - t0 < seconds:
        time.sleep(0.05)


def test_generate_then_view_opens_devins_analysis_in_a_modal(ran):
    c, st = ran
    ids = ins.known_ids(st)
    assert ids, "the replay run left sessions to analyse"
    sid = ids[0]
    before = len(c.app.state.client.t.calls)

    html = c.get("/devin/insights").text
    assert "Generate" in html and "View" not in html.split("Every session")[-1]
    assert not ins.written(st.insight(sid))

    r = c.post(f"/devin/insights/{sid}/generate")
    assert r.status_code == 200
    _wait_idle(c, sid)
    calls = c.app.state.client.t.calls[before:]
    assert ("generate_insights", sid) in calls
    assert not [x for x in calls if x[0] == "create_session"], "asking for analysis starts nothing"
    assert ins.written(st.insight(sid))
    assert any(e["event"].startswith("Devin's analysis") for e in st.timeline(limit=50))

    html = c.get("/devin/insights").text
    assert ">View<" in html and 'role="dialog"' not in html
    opened = c.get(f"/devin/insights?view={sid}").text
    assert 'role="dialog"' in opened, "View opens a modal over the list"
    assert "Detected issues" in opened and "Action items" in opened and "Timeline" in opened
    assert "Acceptance commands reference environments" in opened  # the fixture's analysis
    assert "Raw JSON" in opened and "&#34;action_items&#34;: [" in opened  # pretty-printed
    assert ">Close<" in opened


def test_generate_is_refused_for_a_session_this_loop_did_not_start(ran):
    c, _st = ran
    assert c.post("/devin/insights/not-ours/generate").status_code == 404


def test_generate_helper_reports_what_was_written(ran):
    c, st = ran
    ids = ins.known_ids(st)[:2]
    out = ins.generate(st, c.app.state.client, ids, sleep=lambda _s: None)
    assert out["written"] == ids and out["missing"] == []
    again = ins.generate(st, c.app.state.client, ids, sleep=lambda _s: None)
    assert again["already"] == ids, "a second ask is answered with already_exists"
