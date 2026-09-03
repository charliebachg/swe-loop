"""Live seeding never synthesises: tickets enter as new and the triage session decides."""

import json
from pathlib import Path

from swe_loop.cli import main
from swe_loop.config import TargetConfig
from swe_loop.store import Store, load_tickets

ROOT = Path(__file__).resolve().parents[1]
TICKETS = ROOT / "data" / "inventory" / "2026-09-03" / "tickets.json"


def test_load_tickets_as_new_has_no_verdict_and_no_work_order(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    ids = load_tickets(st, TICKETS, triaged=False)
    assert len(ids) == 5
    for tid in ids:
        t = st.get_ticket(tid)
        assert t["status"] == "new" and not t.get("triage_verdict_json")
        assert t["external_ref"].startswith("charliebachg/superset#")
        assert st.work_orders_for(tid) == []


def test_seed_as_new_and_budget_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("DEVIN_API_KEY", raising=False)
    monkeypatch.setenv("SWE_LOOP_MODE", "replay")
    monkeypatch.setenv("SWE_LOOP_DB", str(tmp_path / "live.sqlite"))
    assert main(["seed", "--as-new"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["as_new"] is True and len(out["tickets"]) == 5
    assert main(["seed", "--as-new"]) == 0
    assert json.loads(capsys.readouterr().out)["seeded"] is False  # never twice
    assert main(["budget", "--cap", "40", "--per-session", "6"]) == 0
    b = json.loads(capsys.readouterr().out)
    assert b["cap"] == 40 and b["per_session_cap"] == 6 and b["spent"] == 0
    assert main(["budget", "--cap", "0"]) == 2
    st = Store(tmp_path / "live.sqlite")
    assert all(t["status"] == "new" for t in st.list_tickets())
    assert st._one("SELECT COUNT(*) AS n FROM sessions")["n"] == 0


def test_seam_carries_the_inventory_url():
    cfg = TargetConfig.load(ROOT / "configs" / "superset-pandas3.yaml")
    assert cfg.triage["inventory_url"].startswith(
        "https://raw.githubusercontent.com/charliebachg/swe-loop/"
    )
