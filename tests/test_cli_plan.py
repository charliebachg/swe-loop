"""apply-config --dry-run prints the plan and creates nothing, in either mode."""

import json

from swe_loop.cli import main, plan_config
from swe_loop.devin import DevinClient, FakeTransport


def test_plan_config_reads_files_and_creates_nothing():
    t = FakeTransport()
    plan = plan_config(DevinClient(t))
    names = [p["file"] for p in plan["playbooks"]]
    assert names == ["triage-pandas3", "repair-pandas3"]
    assert all(p["action"] == "would create" for p in plan["playbooks"])
    assert all("Forbidden Actions" in p["sections"] for p in plan["playbooks"])
    assert "self_reported_done" in plan["playbooks"][1]["schema_fields"]
    assert all(p["schema_bytes"] < 64_000 for p in plan["playbooks"])
    assert len(plan["knowledge_notes"]) >= 6
    assert all(n["trigger_description"] for n in plan["knowledge_notes"])
    assert plan["creates"] == len(plan["playbooks"]) + len(plan["knowledge_notes"])
    # the fake client is treated as no client: nothing at all is called
    assert not any(c[0].startswith("create") for c in t.calls)


def test_apply_config_dry_run_in_replay(monkeypatch, capsys):
    monkeypatch.delenv("DEVIN_API_KEY", raising=False)
    monkeypatch.setenv("SWE_LOOP_MODE", "replay")
    assert main(["apply-config", "--dry-run"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "replay" and out["note"].startswith("dry run: nothing was created")
    assert out["creates"] >= 8 and out["secrets"].startswith("none")


def test_apply_config_refuses_in_replay(monkeypatch, capsys):
    monkeypatch.delenv("DEVIN_API_KEY", raising=False)
    monkeypatch.setenv("SWE_LOOP_MODE", "replay")
    assert main(["apply-config"]) == 2
    assert "refusing" in capsys.readouterr().err


def test_triage_and_run_default_to_the_stored_playbook_ids(tmp_path, monkeypatch, capsys):
    from swe_loop.store import Store

    monkeypatch.delenv("DEVIN_API_KEY", raising=False)
    monkeypatch.setenv("SWE_LOOP_MODE", "replay")
    db = tmp_path / "p.sqlite"
    monkeypatch.setenv("SWE_LOOP_DB", str(db))
    st = Store(db)
    st.set_budget(acu_cap=40, per_session_cap=6)
    st.set_setting("playbook_id.triage-pandas3", "playbook-triage-1")
    st.upsert_ticket(id="tkt_D", source="inventory", title="one site", status="new")
    assert main(["triage", "--ticket", "tkt_D"]) == 0
    out = capsys.readouterr().out
    assert '"kind": "triaged"' in out
    tr = Store(db).list_triage_sessions()[0]
    assert tr["playbook_id"] == "playbook-triage-1"
