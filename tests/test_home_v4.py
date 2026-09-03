"""Home as an operator's console: numbers against comparisons, an inbox with actions, the
in-flight list, and the two actions a person takes from it."""

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
FORM = {"Content-Type": "application/x-www-form-urlencoded", "HX-Request": "true"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("DEVIN_API_KEY", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    st = Store(tmp_path / "t.sqlite")
    synthesise(st, CFG, INVENTORY / "tickets.json", tmp_path)
    app = build_app(Settings.from_env(), st, seed_replay=False)
    with TestClient(app) as c:
        yield c, st


def test_home_tiles_inbox_and_next_trigger(client):
    c, _st = client
    html = c.get("/").text
    assert "finished without a question" in html and "decided" in html
    assert "Needs you" in html and ">Merge<" in html and ">Dismiss<" in html
    assert "nothing running · next trigger: github:pull_request on charliebachg/superset" in html
    assert html.count("<svg") == 3  # the trends live inside the tiles
    assert "—" not in html


def test_dismiss_resolves_the_escalation(client):
    c, st = client
    e = st.list_escalations()[0]
    r = c.post(f"/escalations/{e['id']}/resolve", content="note=a+known+limitation", headers=FORM)
    assert r.status_code == 200 and e["kind"] not in r.text.split("Needs you")[-1][:400]
    assert all(x["id"] != e["id"] for x in st.list_escalations())
    assert any(
        ev["event"] == "escalation dismissed by a person"
        for ev in st.timeline(ticket_id=e["ticket_id"], limit=10)
    )
    assert c.post("/escalations/nope/resolve", content="note=x", headers=FORM).status_code == 404


def test_answer_wakes_the_triage_session(client):
    c, st = client
    st.upsert_ticket(id="tkt_Q", source="inventory", title="a question", status="escalated")
    st.insert_triage_session(
        ticket_id="tkt_Q",
        devin_session_id="dev-q",
        url="u",
        status="running",
        status_detail="waiting_for_user",
        playbook_id=None,
        tags=["swe-loop", "triage", "tkt_Q"],
    )
    st.insert_escalation("tkt_Q", None, "waiting_for_user", "which branch?")
    assert "/tickets/tkt_Q/answer" in c.get("/").text
    assert c.post("/tickets/tkt_Q/answer", content="text=", headers=FORM).status_code == 400
    r = c.post("/tickets/tkt_Q/answer", content="text=work+from+master", headers=FORM)
    assert r.status_code == 200
    assert (
        st.get_ticket("tkt_Q")["status"] == "triaged"
    )  # the fake session re-delivered its verdict
    assert any(
        ev["event"] == "answered by a person" for ev in st.timeline(ticket_id="tkt_Q", limit=20)
    )
