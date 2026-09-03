"""The list pages: automations as add-able configs, playbooks as a list with a detail panel,
tickets with a summary strip and a detail panel."""

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
FORM = {"Content-Type": "application/x-www-form-urlencoded"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("DEVIN_API_KEY", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    st = Store(tmp_path / "t.sqlite")
    synthesise(st, CFG, INVENTORY / "tickets.json", tmp_path)
    app = build_app(Settings.from_env(), st, seed_replay=False)
    with TestClient(app) as c:
        yield c, st, app


def test_automations_seeded_as_a_list(client):
    c, st, _ = client
    html = c.get("/automations").text
    ids = [a["id"] for a in st.list_automations()]
    assert ids == ["auto_repair", "auto_scan"]
    assert "Issues from the fork" in html and "on a schedule" in html
    assert "Add automation" in html and ">Run<" in html
    assert "\u2014" not in html
    # seeding is idempotent
    c.get("/automations")
    assert len(st.list_automations()) == 2


def test_automation_add_toggle_delete(client):
    c, st, _ = client
    body = (
        "name=Repair+on+check+failure&trigger=github%3Acheck_run&playbook=repair-pandas3"
        "&target=x%2Fy&match=author%3Ddependabot%5Bbot%5D%3B+label%3Dswe-loop&max_acu=6"
        "&concurrency=2&notes=test"
    )
    r = c.post("/automations", content=body, headers=FORM)
    assert r.status_code == 200 and "Repair on check failure" in r.text
    custom = [a for a in st.list_automations() if a["kind"] == "custom"]
    assert len(custom) == 1
    a = custom[0]
    assert (
        a["enabled"] == 0 and a["target"] == "x/y" and a["max_acu"] == 6 and a["concurrency"] == 2
    )
    assert a["trigger"]["source"] == "github" and a["trigger"]["event"] == "check_run"
    assert a["trigger"]["match"] == {"author": "dependabot[bot]"}
    assert a["trigger"]["issue_label"] == "swe-loop"
    assert c.post(f"/automations/{a['id']}/toggle").status_code == 200
    assert st.get_automation(a["id"])["enabled"] == 1
    assert c.post("/automations/auto_scan/toggle").status_code == 409  # next, not live
    assert c.post("/automations/auto_repair/delete").status_code == 409  # built in
    assert c.post(f"/automations/{a['id']}/delete").status_code == 200
    assert len(st.list_automations()) == 2
    r = c.post("/automations", content="name=", headers=FORM)
    assert r.status_code == 200 and "name is required; nothing was saved" in r.text
    assert c.post("/automations/nope/toggle").status_code == 404


def test_automation_run_now_records_last_result(client):
    c, st, app = client
    assert c.post("/automations/auto_scan/run").status_code == 409
    c.post("/automations/auto_repair/toggle")  # disable
    assert c.post("/automations/auto_repair/run").status_code == 409
    c.post("/automations/auto_repair/toggle")  # enable
    assert c.post("/automations/auto_repair/run").status_code == 200
    app.state.run_thread.join(60)
    a = st.get_automation("auto_repair")
    assert a["last_run"] and "dispatched" in a["last_result"]
    assert "last run" in c.get("/automations").text
    app.state.run_thread.join(60)


def test_playbooks_list_detail_and_add(client):
    c, st, _ = client
    html = c.get("/devin/playbooks").text
    names = [p["name"] for p in st.list_playbooks()]
    assert names[:2] == ["triage-pandas3", "repair-pandas3"] and len(names) == 3
    assert "Add a playbook" in html and "Forbidden Actions" in html  # first playbook opens
    d = c.get("/partials/playbook/pb_repair").text
    assert "Procedure" in d and "structured output schema" in d
    assert c.get("/partials/playbook/nope").status_code == 404
    body = (
        "name=qa-in-app&agent=QA+session&max_acu=4"
        "&body=%23+QA+in+the+running+app%0A%0A%23%23+Overview%0A%0Ax%0A"
        "&schema=%7B%22type%22%3A%22object%22%2C%22properties%22%3A%7B%22ok%22%3A%7B%22type%22%3A%22boolean%22%7D%7D%7D"
    )
    r = c.post("/devin/playbooks", content=body, headers=FORM)
    assert r.status_code == 200 and "QA in the running app" in r.text
    added = next(p for p in st.list_playbooks() if p["name"] == "qa-in-app")
    assert added["source"] == "user" and added["schema"]["properties"] == {
        "ok": {"type": "boolean"}
    }
    assert (
        c.post(
            "/devin/playbooks", content="name=bad&body=%23+x&schema=%7Bnope", headers=FORM
        ).status_code
        == 400
    )
    r = c.post("/devin/playbooks", content="name=&body=", headers=FORM)
    assert r.status_code == 200 and "name is required; nothing was saved" in r.text


def test_tickets_summary_filters_and_detail(client):
    c, _st, _ = client
    html = c.get("/tickets-page").text
    assert "awaiting triage" in html and "to Devin" in html and "General" in html
    assert 'hx-get="/tickets-page?open=tkt_D"' in html
    assert "to a person" in c.get("/tickets-page?f=human").text
    d = c.get("/partials/ticket/tkt_D").text
    assert "Sites" in d and "Acceptance" in d and "routed to Devin" in d
    e = c.get("/partials/ticket/tkt_E").text
    assert "routed to a person" in e
    assert c.get("/partials/ticket/nope").status_code == 404
    assert "\u2014" not in html + d + e
