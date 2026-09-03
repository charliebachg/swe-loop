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
    assert "Needs you" in html and "What just happened" in html
    assert "tkt_E" in html and "needs your team" in html  # the escalation
    assert "ready to ship" in html  # C and D passed and were reviewed
    assert "RECORDED RUN" in html and "charliebachg/superset" in html
    assert "\u2014" not in html  # no em dashes
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
    assert html.count("/tracker?open=tkt_") >= 5


def test_tracker_rows_stages_and_merge(client):
    c, st = client
    html = c.get("/tracker?open=tkt_A,tkt_B,tkt_D,tkt_E").text
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


def test_sessions_page_eta_parent_and_drawer(client):
    c, st = client
    html = c.get("/devin/sessions").text
    assert "fake-" in html and "Issues on the fork" in html
    assert "not exercised in this run" in html  # Managed Devins, honestly
    assert "single" in html and "done" in html
    # a live session gets an estimate from the finished ones (give them a real elapsed time)
    st.conn.execute(
        "UPDATE sessions SET terminal_at = datetime(created_at, '+25 minutes') WHERE terminal_at IS NOT NULL"
    )
    wo = st.work_orders_for("tkt_D")[0]
    sid = st.reserve_session(work_order_id=wo["id"], playbook_id=None, tags=["swe-loop"])
    st.bind_devin_session(
        sid, devin_session_id="devin-live", url="https://app.devin.ai/sessions/x", status="running"
    )
    html = c.get("/partials/sessions").text
    assert "est." in html and "left" in html
    first = st._all("SELECT id FROM sessions ORDER BY rowid LIMIT 1")[0]["id"]
    d = c.get(f"/partials/session/{first}").text
    assert "Timeline" in d and "Structured output" in d and "self_reported_done" in d
    assert c.get("/partials/session/nope").status_code == 404


def test_automations_run_now_dispatches_a_routed_ticket(client):
    c, st = client
    html = c.get("/automations").text
    assert "Repair" in html and "Scan" in html and "schedule:recurring" in html
    assert "Run now" in html and "replay" in html
    # a fresh routed ticket, then run now dispatches it on the fake transport
    st.upsert_ticket(
        id="tkt_X",
        source="manual",
        title="x",
        status="triaged",
        triage_verdict={"acceptance_cmd": {"p3": "true"}, "sites": [], "split": "one"},
    )
    st.insert_work_order(
        ticket_id="tkt_X",
        shard_id="X",
        files=["superset/x.py"],
        tests=["t"],
        acceptance={"p3": "true"},
    )
    before = len(st._all("SELECT id FROM sessions"))
    r = c.post("/automations/auto_repair/run")
    assert r.status_code == 200
    th = c.app.state.run_thread
    th.join(timeout=30)
    assert not th.is_alive()
    assert len(st._all("SELECT id FROM sessions")) == before + 1
    assert st.get_automation("auto_repair")["last_run"]
    assert "gate" in " ".join(e["layer"] for e in st.timeline(ticket_id="tkt_X", limit=50))
    assert st.get_ticket("tkt_X")["status"] == "gated"


def test_capability_pages_render_real_state(client):
    c, _ = client
    html = c.get("/devin/playbooks").text
    assert "Repair one shard" in html and "Triage a dependency-upgrade ticket" in html
    repair = c.get("/partials/playbook/pb_repair").text
    triage = c.get("/partials/playbook/pb_triage").text
    assert "<h4>Forbidden Actions</h4>" in repair and "self_reported_done" in repair
    assert "<h4>Forbidden Actions</h4>" in triage and "acceptance_cmd" in triage
    html = c.get("/devin/knowledge").text
    assert html.count("<tr>") >= 7 and "ruff" in html and "oxlint" in html and "lower bound" in html
    html = c.get("/devin/insights").text
    assert "Session size" in html and "per verified change" in html
    html = c.get("/devin/review").text
    assert "requested" in html and "devin-ai-integration[bot]" in html
    html = c.get("/devin/integrations").text
    assert "only charliebachg/superset" in html and "not checked" in html
    html = c.get("/devin/next").text
    for name in (
        "Computer Use",
        "DeepWiki",
        "Security Swarm",
        "Scan session",
        "Evaluator session",
        "Devin MCP",
    ):
        assert name in html
    for path in (
        "/devin/playbooks",
        "/devin/knowledge",
        "/devin/insights",
        "/devin/review",
        "/devin/integrations",
        "/devin/next",
    ):
        assert "\u2014" not in c.get(path).text
