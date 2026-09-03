"""The triage runner: one session per new ticket, the verdict validated by code before any
work order exists; a session without a verdict is an escalation, never a pass."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from swe_loop.app import build_app
from swe_loop.cli import main
from swe_loop.config import Settings, TargetConfig
from swe_loop.devin import DevinClient, FakeTransport
from swe_loop.store import Store
from swe_loop.triage import run_triage, triage_all

ROOT = Path(__file__).resolve().parents[1]
CFG = TargetConfig.load(ROOT / "configs" / "superset-pandas3.yaml")


def _store(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    st.set_budget(acu_cap=40, per_session_cap=6)
    st.upsert_ticket(
        id="tkt_D", source="github_issue", title="pandas 3: mixed-timezone parsing", status="new"
    )
    return st


def test_run_triage_happy_path_makes_a_work_order(tmp_path):
    st = _store(tmp_path)
    t = FakeTransport()
    r = run_triage(st, DevinClient(t), "tkt_D", CFG)
    assert r["kind"] == "triaged" and len(r["work_orders"]) == 1
    assert st.get_ticket("tkt_D")["status"] == "triaged"
    tr = st.get_triage_session(r["session"])
    assert tr["terminal_at"] and tr["outcome"] == "triaged" and tr["acus_consumed"] == 2.1
    assert tr["verdict"]["ticket_id"] == "tkt_D" and tr["verdict"]["split"] == "one"
    # the session was created with the contract, the cap and the identity tags
    created = next(c[1] for c in t.calls if c[0] == "create_session")
    assert created["structured_output_required"] is True
    assert (
        created["max_acu_limit"] == 3 and "triage" in created["tags"] and "tkt_D" in created["tags"]
    )
    # triage cost counts against the budget
    assert st.budget_state()["spent"] == pytest.approx(2.1)
    # a second call is a no-op: the ticket is no longer new
    assert run_triage(st, DevinClient(t), "tkt_D", CFG)["kind"] == "skipped"
    assert len(st.list_triage_sessions()) == 1


class _BadOutput(FakeTransport):
    def _synth(self, payload):
        self._counter += 1
        return self._timeline(f"{self._prefix}-{self._counter:03d}", {"bad": True}, pr_url=None)


class _NoOutput(FakeTransport):
    def _synth(self, payload):
        self._counter += 1
        fx = self._timeline(f"{self._prefix}-{self._counter:03d}", {}, pr_url=None)
        fx["timeline"][-1]["structured_output"] = None
        return fx


def test_invalid_verdict_is_escalated_not_applied(tmp_path):
    st = _store(tmp_path)
    r = run_triage(st, DevinClient(_BadOutput()), "tkt_D", CFG)
    assert r["kind"] == "invalid" and "verdict rejected" in r["detail"]
    assert st.get_ticket("tkt_D")["status"] == "escalated"
    assert st.work_orders_for("tkt_D") == []
    assert st.list_escalations()[0]["kind"] == "review_blocked"


def test_no_output_is_a_failure(tmp_path):
    st = _store(tmp_path)
    r = run_triage(st, DevinClient(_NoOutput()), "tkt_D", CFG)
    assert r["kind"] == "no_output"
    assert st.get_ticket("tkt_D")["status"] == "escalated"
    assert st.get_triage_session(r["session"])["outcome"] == "no_output"


def test_triage_all_takes_only_new_tickets(tmp_path):
    st = _store(tmp_path)
    st.upsert_ticket(id="tkt_Z", source="manual", title="already triaged", status="triaged")
    out = triage_all(st, DevinClient(FakeTransport()), CFG)
    assert [r["ticket_id"] for r in out] == ["tkt_D"]


def test_cli_triage_in_replay_on_an_empty_store(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("DEVIN_API_KEY", raising=False)
    monkeypatch.setenv("SWE_LOOP_MODE", "replay")
    monkeypatch.setenv("SWE_LOOP_DB", str(tmp_path / "cli.sqlite"))
    assert main(["triage"]) == 0
    assert "no tickets with status new" in capsys.readouterr().out


def test_sessions_page_lists_the_triage_session(tmp_path, monkeypatch):
    monkeypatch.delenv("DEVIN_API_KEY", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    st = _store(tmp_path)
    run_triage(st, DevinClient(FakeTransport()), "tkt_D", CFG)
    app = build_app(Settings.from_env(), st, seed_replay=False)
    with TestClient(app) as c:
        html = c.get("/devin/sessions").text
        assert "· triage" in html and ">triaged<" in html and "verdict</a>" in html
        assert "2.1" in html
        home = c.get("/").text
        assert "2.1" in home  # ACU spent on Home includes the triage session


class _DeliveredWhileWaiting(FakeTransport):
    """What the org did on 2026-09-03: the verdict is in structured_output, the turn ended, the
    session shows running/waiting_for_user rather than exit/finished."""

    def _synth(self, payload):
        fx = super()._synth(payload)
        fx["timeline"][-1].update(status="running", status_detail="waiting_for_user")
        return fx


class _AsksFirst(FakeTransport):
    """Asks a question (no output), then delivers after a message."""

    def _synth(self, payload):
        fx = super()._synth(payload)
        final = dict(fx["timeline"][-1])
        fx["timeline"] = fx["timeline"][:2] + [
            {"status": "running", "status_detail": "waiting_for_user", "acus_consumed": 0.5}
        ]
        self._final = final
        return fx

    def send_message(self, session_id, text):
        out = super().send_message(session_id, text)
        s = self._sessions[session_id]
        s["fixture"]["timeline"].append(
            {"status": "running", "status_detail": "working", "acus_consumed": 0.9}
        )
        s["fixture"]["timeline"].append(self._final)
        s["i"] = len(s["fixture"]["timeline"]) - 2
        return out


def test_verdict_delivered_while_waiting_counts(tmp_path):
    st = _store(tmp_path)
    r = run_triage(st, DevinClient(_DeliveredWhileWaiting()), "tkt_D", CFG)
    assert r["kind"] == "triaged" and st.get_ticket("tkt_D")["status"] == "triaged"


def test_question_then_answer_adopts_the_same_session(tmp_path):
    st = _store(tmp_path)
    t = _AsksFirst()
    c = DevinClient(t)
    r = run_triage(st, c, "tkt_D", CFG)
    assert r["kind"] == "waiting" and st.get_ticket("tkt_D")["status"] == "escalated"
    assert st.list_escalations()[0]["kind"] == "waiting_for_user"
    r2 = run_triage(st, c, "tkt_D", CFG, answer="Work from master. Preserve local wall clock.")
    assert r2["kind"] == "triaged" and r2["session"] == r["session"]
    assert len(st.list_triage_sessions()) == 1
    assert any(call[0] == "send_message" for call in t.calls)
    assert sum(1 for call in t.calls if call[0] == "create_session") == 1


def test_answer_wakes_a_suspended_session_instead_of_starting_a_new_one(tmp_path, monkeypatch):
    """The org reports a sleeping session as suspended and the live lookup no longer sees it; an
    answer must still go to that session, never to a fresh one."""
    st = _store(tmp_path)
    t = _AsksFirst()
    c = DevinClient(t)
    r = run_triage(st, c, "tkt_D", CFG)
    assert r["kind"] == "waiting"
    devin_id = st.get_triage_session(r["session"])["devin_session_id"]
    monkeypatch.setattr(c, "find_live", lambda tags, exclude=None: None)
    r2 = run_triage(st, c, "tkt_D", CFG, answer="work from master")
    assert r2["kind"] == "triaged" and r2["session"] == r["session"]
    assert sum(1 for call in t.calls if call[0] == "create_session") == 1
    assert [call for call in t.calls if call[0] == "send_message"][-1][1][0] == devin_id
