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
    assert "fixes that needed no help" in html and "given to the AI" in html
    assert (
        "Needs you" in html
        and ">Merge<" in html
        and ">Dismiss<" in html
        and "ready to merge" in html
    )
    assert "the AI is idle" not in html and "scoped by the AI" in html and "verified" in html
    assert html.count('class="bar"') >= 24  # the trends live inside the tiles as bars
    assert "\u2014" not in html


def test_dismiss_resolves_the_escalation(client):
    c, st = client
    e = st.list_escalations()[0]
    r = c.post(f"/escalations/{e['id']}/resolve", content="note=a+known+limitation", headers=FORM)
    assert r.status_code == 200 and e["kind"] not in r.text.split("Needs you")[-1][:400]
    assert all(x["id"] != e["id"] for x in st.list_escalations())
    assert any(
        ev["event"] == "dismissed by a person"
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
    # the answer alone carries the ticket on: verdict, route, session, gate, all unattended
    assert st.get_ticket("tkt_Q")["status"] == "gated"
    events = [ev["event"] for ev in st.timeline(ticket_id="tkt_Q", limit=40)]
    assert "continuing after your answer" in events and "dispatched" in events
    assert any(
        ev["event"] == "answered by a person" for ev in st.timeline(ticket_id="tkt_Q", limit=20)
    )


def test_dismissing_the_last_question_closes_the_ticket(client):
    """Dismiss answers the question with "no action". The ticket must not sit at `escalated`,
    a status no part of the loop reads; it is closed as refused, with the note as the reason."""
    c, st = client
    st.upsert_ticket(id="tkt_Z", source="github", title="a question", status="new")
    st.set_router_decision("tkt_Z", "human_only", "needs a person")
    eid = st.insert_escalation("tkt_Z", None, "human_only", "which rule does this break?")
    assert st.get_ticket("tkt_Z")["status"] == "escalated"
    r = c.post(f"/escalations/{eid}/resolve", data={"note": "not a defect, see SECURITY.md row 3"})
    assert r.status_code == 200
    t = st.get_ticket("tkt_Z")
    assert t["status"] == "refused" and "not a defect" in (t["router_reason"] or "")
    assert st.list_escalations() == [] or all(
        e["ticket_id"] != "tkt_Z" for e in st.list_escalations()
    )
