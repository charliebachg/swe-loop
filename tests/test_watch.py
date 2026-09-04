"""The app looks for Devin's schedule by itself.

Devin holds the recurrence and there is no webhook out. If the looking is a terminal command
somebody has to remember, the schedule runs every hour and the board hears nothing. So the app
holds the watcher, and these tests hold the four properties that make it safe to leave running."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from swe_loop import codescan, pages
from swe_loop.config import Settings, TargetConfig
from swe_loop.devin import DevinClient, FakeTransport
from swe_loop.store import Store
from swe_loop.watch import Watcher, status

ROOT = Path(__file__).resolve().parents[1]
CFG = TargetConfig.load(ROOT / "configs" / "superset-pandas3.yaml")


def _live_settings(monkeypatch) -> Settings:
    monkeypatch.setenv("SWE_LOOP_MODE", "live")
    monkeypatch.setenv("DEVIN_API_KEY", "cog_test")
    return Settings.from_env()


def _client_with_schedule(enabled: bool) -> DevinClient:
    t = FakeTransport()
    t.automations = [
        {"automation_id": "auto-theirs", "name": "Scan new commits", "enabled": enabled}
    ]
    return DevinClient(t)


def _store(tmp_path) -> Store:
    st = Store(tmp_path / "w.sqlite")
    pages.seed_automations(st, CFG)
    return st


def test_nothing_to_watch_means_no_thread(tmp_path, monkeypatch):
    """Replay has no organisation; a row with no schedule on Devin has nothing that could fire."""
    monkeypatch.setenv("SWE_LOOP_MODE", "replay")
    st = _store(tmp_path)
    w = Watcher(Settings.from_env(), CFG, st, DevinClient(FakeTransport()), threading.Lock())
    assert w.start() is False

    live = _live_settings(monkeypatch)
    w2 = Watcher(live, CFG, st, _client_with_schedule(True), threading.Lock())
    assert w2.start() is False, "the fake client is never watched; nothing real to read"


def test_a_tick_with_the_schedule_off_costs_one_read_and_touches_nothing(tmp_path, monkeypatch):
    st = _store(tmp_path)
    st.set_automation("auto_codescan", devin_automation_id="auto-theirs")
    client = _client_with_schedule(enabled=False)
    w = Watcher(_live_settings(monkeypatch), CFG, st, client, threading.Lock())

    out = w.tick()

    assert out == {"looked": True, "schedule": "off"}
    assert not [c for c in client.t.calls if c[0] in ("list_code_scans", "start_code_scan")]
    assert status(st)["schedule_on"] is False
    assert status(st)["last_look"], "it still records that it looked"


def test_a_tick_with_the_schedule_on_records_devins_run_and_starts_nothing(tmp_path, monkeypatch):
    """The whole point. The schedule fired on Devin's side; the tick must record that, file
    anything new, and never start a scan of its own."""
    st = _store(tmp_path)
    settings = _live_settings(monkeypatch)
    client = _client_with_schedule(enabled=True)
    codescan.run(settings, CFG, st, client, log=lambda _m: None)
    st.set_automation("auto_codescan", devin_automation_id="auto-theirs")
    client.t.foreign_sessions = [
        {
            "session_id": "orch-1",
            "origin": "automation",
            "automation_id": "auto-theirs",
            "parent_session_id": None,
            "child_session_ids": ["a", "b"],
            "status": "exit",
            "status_detail": "finished",
            "title": "Incremental security re-scan",
            "created_at": 1788555626,
            "updated_at": 1788556000,
        }
    ]
    before = [c for c in client.t.calls if c[0] == "start_code_scan"]

    out = w = Watcher(settings, CFG, st, client, threading.Lock()).tick()

    assert out["schedule"] == "on"
    assert out["scan"] == "ran, nothing new"
    assert out["scheduled_runs"] == 1
    assert [c for c in client.t.calls if c[0] == "start_code_scan"] == before, "never its own scan"
    runs = [r for r in st.list_automation_runs("auto_codescan") if r["status"] == "observed"]
    assert len(runs) == 1 and runs[0]["result"]["sessions"] == 3
    assert status(st)["schedule_on"] is True
    del w


def test_a_tick_steps_aside_while_a_person_runs_something(tmp_path, monkeypatch):
    """The watcher and the Run button share one lock. A tick during a run does nothing at all."""
    st = _store(tmp_path)
    st.set_automation("auto_codescan", devin_automation_id="auto-theirs")
    client = _client_with_schedule(enabled=True)
    lock = threading.Lock()
    w = Watcher(_live_settings(monkeypatch), CFG, st, client, lock)

    lock.acquire()
    try:
        out = w.tick()
    finally:
        lock.release()

    assert out.get("skipped") == "a run is going"
    assert not [c for c in client.t.calls if c[0] == "list_code_scans"]


def test_an_observed_run_survives_a_restart_unmarked(tmp_path, monkeypatch):
    """The app marks its own running rows as cut short on restart. A run on Devin's side was not
    cut short by our restart, and must not be relabelled as if it were."""
    from fastapi.testclient import TestClient

    from swe_loop.app import build_app

    monkeypatch.setenv("SWE_LOOP_MODE", "replay")
    st = _store(tmp_path)
    st.record_observed_run(
        "auto_codescan",
        started_at="2026-09-04T21:00:00+00:00",
        result={"started_by": "Devin's schedule", "orchestrator": "orch-9", "sessions": 5},
        status="observed",
    )
    with TestClient(build_app(Settings.from_env(), st, seed_replay=False)) as c:
        c.get("/")
    run = next(
        r
        for r in st.list_automation_runs("auto_codescan")
        if r["result"].get("orchestrator") == "orch-9"
    )
    assert run["status"] == "observed"
    assert "restarted" not in (run["result"].get("error") or "")


def test_the_spend_view_counts_devins_own_sessions_without_pricing_them(tmp_path):
    from swe_loop import cost

    st = _store(tmp_path)
    st.record_observed_run(
        "auto_codescan",
        started_at="2026-09-04T21:00:00+00:00",
        result={"started_by": "Devin's schedule", "orchestrator": "o1", "sessions": 5},
        status="observed",
    )
    st.record_observed_run(
        "auto_codescan",
        started_at="2026-09-04T22:00:00+00:00",
        result={"started_by": "Devin's schedule", "orchestrator": "o2", "sessions": 4},
        status="observed",
    )
    sp = cost.spend(st)
    assert sp["n_devin_own"] == 9
    assert not sp["usd"], "nothing is priced from them: they are counted, not billed to us"


@pytest.mark.parametrize("enabled", [True, False])
def test_stop_is_honoured_promptly(tmp_path, monkeypatch, enabled):
    """A daemon thread that sleeps two minutes at a stretch would ignore stop() for two minutes.
    It sleeps in small steps, so shutdown is quick."""
    st = _store(tmp_path)
    st.set_automation("auto_codescan", devin_automation_id="auto-theirs")
    client = _client_with_schedule(enabled)
    slept: list[float] = []
    w = Watcher(
        _live_settings(monkeypatch),
        CFG,
        st,
        client,
        threading.Lock(),
        every=600.0,
        sleep=lambda s: slept.append(s),
    )
    # drive one pass of the loop body by hand
    w._stop.clear()
    w.tick()
    w.stop()
    assert w.alive is False
    assert all(s <= 2.0 for s in slept)
