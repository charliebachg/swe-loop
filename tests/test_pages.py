from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from swe_loop.app import build_app
from swe_loop.config import Settings, TargetConfig
from swe_loop.replay import synthesise
from swe_loop.store import Store

ROOT = Path(__file__).resolve().parents[1]
CFG = TargetConfig.load(ROOT / "configs" / "superset-pandas3.yaml")
INVENTORY = ROOT / "data" / "inventory" / "2026-09-03"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("DEVIN_API_KEY", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    st = Store(tmp_path / "t.sqlite")
    synthesise(st, CFG, INVENTORY / "tickets.json", tmp_path)
    app = build_app(Settings.from_env(), st, seed_replay=False)
    with TestClient(app) as c:
        yield c, st


def test_home_shows_now_needs_you_and_recent(client):
    c, _st = client
    r = c.get("/")
    assert r.status_code == 200
    html = r.text
    assert "Needs you" in html and "Recent" in html
    assert "tkt_E" in html and "human_only" in html  # the escalation
    assert "ready to merge" in html  # C and D passed and were reviewed
    assert "REPLAY" in html and "charliebachg/superset" in html
    assert "—" not in html
    assert c.get("/partials/home").status_code == 200


def test_settings_shows_checks_seam_and_budget(client):
    c, st = client
    html = c.get("/settings").text
    assert "not checked" in html  # replay: no tokens, nothing is called
    assert "configs/superset-pandas3.yaml" in html
    assert "tests/" in html and "the lower bound does not move" in html
    r = c.post(
        "/settings/budget",
        content="acu_cap=120&per_session_cap=4",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    b = st.budget_state()
    assert b["cap"] == 120 and b["per_session_cap"] == 4
    assert "120" in c.get("/settings").text
    r = c.post(
        "/settings/budget",
        content="acu_cap=-1&per_session_cap=4",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 400


def test_sidebar_links_every_module(client):
    c, _ = client
    html = c.get("/").text
    for href in (
        "/automations",
        "/tickets-page",
        "/tracker",
        "/report",
        "/devin/sessions",
        "/devin/playbooks",
        "/devin/knowledge",
        "/devin/insights",
        "/devin/review",
        "/devin/integrations",
        "/devin/next",
        "/settings",
    ):
        assert f'href="{href}"' in html


def test_settings_store_helpers(tmp_path):
    st = Store(tmp_path / "s.sqlite")
    assert st.get_setting("x") is None and st.get_setting("x", "d") == "d"
    st.set_setting("x", "1")
    st.set_setting("x", "2")
    assert st.get_setting("x") == "2"


def test_tickets_page_groups_by_source(client):
    c, _ = client
    html = c.get("/tickets-page").text
    assert "Issues on the fork" in html and "Scan" in html
    assert "human-only" in html and "sessions never edit tests" in html
    assert 'href="https://github.com/charliebachg/superset/issues/4"' in html
    assert html.count("track</a>") == 5


def test_tracker_rows_stages_and_merge(client):
    c, st = client
    html = c.get("/tracker").text
    assert 'id="tkt_D"' in html and "the session said" in html and "the gate found" in html
    assert "retries 1" in html  # D failed T1 once and passed on retry
    assert "Merged by a person" in html  # A and B
    assert "Ready to merge" in html  # C and D
    assert "Routed to a person" in html  # E
    # the strip: A has every stage done including merge
    a = html.split('id="tkt_A"')[1].split("</summary>")[0]
    assert a.count("done") >= 7
    # record a merge through the form
    r = c.post(
        "/tickets/tkt_D/merge-form",
        content="actor=someone",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 200 and st.get_ticket("tkt_D")["status"] == "merged"
    # not ready: E cannot be merged
    c.post(
        "/tickets/tkt_E/merge-form",
        content="actor=someone",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert st.get_ticket("tkt_E")["status"] == "escalated"
    assert c.get("/partials/tracker").status_code == 200
