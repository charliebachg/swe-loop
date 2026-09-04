import re
from pathlib import Path

from fastapi.testclient import TestClient

from swe_loop.app import build_app
from swe_loop.cli import main
from swe_loop.config import Settings, TargetConfig
from swe_loop.replay import record, seed, synthesise
from swe_loop.report import build
from swe_loop.store import Store

ROOT = Path(__file__).resolve().parents[1]
CFG = TargetConfig.load(ROOT / "configs" / "superset-pandas3.yaml")
INVENTORY = ROOT / "data" / "inventory" / "2026-09-03"


def seeded(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    out = synthesise(st, CFG, INVENTORY / "tickets.json", tmp_path)
    assert out["sessions"] == 4 and out["merged"] == 2
    return st


def test_view_model_has_the_six_rows_with_denominators(tmp_path):
    st = seeded(tmp_path)
    vm = build(st, INVENTORY)
    h = vm["headline"]
    assert h["verified"] == {"n": 2, "of": 5, "sql": h["verified"]["sql"]}
    assert h["acu"]["n"] == 4 and h["acu"]["median"] == 2.1 and h["acu"]["usd_low"] == 4.2
    assert (
        h["claims"]["said_done"] == 4
        and h["claims"]["passed_gate"] == 4
        and h["claims"]["gap"] == 0
    )
    assert h["budget"]["cap"] == 300 and h["budget"]["spent"] > 0
    names = [n for n, _, _ in vm["funnel"]]
    assert "refused or human-only" in names and "human-merged" in names
    assert vm["burndown"]["product"] == 10 and vm["burndown"]["test_only"] == 15
    assert vm["burndown"]["fixed"] > 0 and vm["burndown"]["human"] >= 15
    assert len(vm["receipts"]) == 4
    r = {x["ticket"]: x for x in vm["receipts"]}
    assert r["tkt_A"]["merged_by"] == "human" and r["tkt_D"]["merged_by"] == "no"
    assert r["tkt_D"]["retries"] == 1 and r["tkt_D"]["gate"] == "pass"  # failed once, then passed
    assert all(x["t0"] is True for x in vm["receipts"])
    tw = {t["name"]: t for t in vm["tripwires"]}
    assert tw["oracle touched by a session"]["status"] == "PASS"
    assert tw["merged with zero human review"]["status"] == "PASS"
    assert tw["survived 30 days"]["status"] == "n/a"
    verdicts = {r["class"]: r["verdict"] for r in vm["routing"]}
    assert verdicts["to_datetime-mixed-tz"] == "autonomous"
    assert any(v == "human-only" for v in verdicts.values())
    assert any(e["kind"] == "human_only" for e in vm["escalations"])
    assert len(vm["banned"]) == 6


def test_dashboard_renders_and_shows_sql_on_request(tmp_path, monkeypatch):
    monkeypatch.delenv("DEVIN_API_KEY", raising=False)
    st = seeded(tmp_path)
    app = build_app(Settings.from_env(), st, seed_replay=False)
    with TestClient(app) as c:
        r = c.get("/report")
        assert r.status_code == 200
        html = r.text
        for must in (
            "Verification pass rate",
            "Human intervention rate",
            "Acceptance rate",
            "Checks we ran ourselves",
            "Where the work went",
            "The log",
            "This page cannot tell you",
            "<svg",
        ):
            assert must in html
        # the three rates read as a count over its denominator, never a bare percentage
        assert ">4</span>" in html and ">5</span>" in html
        visible = re.sub(
            r"(?s)<[^>]+>", " ", re.sub(r"(?is)<(script|style|svg).*?</\1>", " ", html)
        )
        assert "%" not in visible  # percentages live in the CSS, never in the reader's text
        # the checks are one click away, with the log behind each one
        opened = c.get("/report?checks=1").text
        assert "code it ran on" in opened and "log fingerprint" in opened
        # a person merges: the endpoint refuses an unready ticket and records a ready one
        assert c.post("/tickets/tkt_C/merge", json={"actor": "someone"}).status_code == 200
        assert c.post("/tickets/tkt_E/merge", json={"actor": "someone"}).status_code == 409
        assert c.post("/tickets/tkt_D/merge", json={}).status_code == 400
        s = c.get("/reduce").json()
        assert "tkt_C" in s["merged"]
        assert "\u2014" not in html  # no em dashes anywhere on the page


def test_replay_seed_is_idempotent_and_prefers_a_recorded_run(tmp_path, monkeypatch):
    st = Store(tmp_path / "a.sqlite")
    out = seed(st, CFG, tickets_json=INVENTORY / "tickets.json", replay_dir=tmp_path)
    assert out["seeded"] and out["recorded"] is False
    assert (
        seed(st, CFG, tickets_json=INVENTORY / "tickets.json", replay_dir=tmp_path)["seeded"]
        is False
    )
    counts = record(st, tmp_path / "run.json")
    assert counts["sessions"] == 4 and counts["verdicts"] >= 4
    st2 = Store(tmp_path / "b.sqlite")
    out2 = seed(st2, CFG, tickets_json=INVENTORY / "tickets.json", replay_dir=tmp_path)
    assert out2["recorded"] is True and out2["sessions"] == 4
    assert st2.metrics() == st.metrics()


def test_app_seeds_replay_on_startup_when_empty(tmp_path, monkeypatch):
    monkeypatch.delenv("DEVIN_API_KEY", raising=False)
    monkeypatch.setenv("SWE_LOOP_REPLAY_DIR", str(tmp_path))
    st = Store(tmp_path / "t.sqlite")
    app = build_app(Settings.from_env(), st)
    with TestClient(app) as c:
        assert c.get("/metrics").json()["funnel"]["sessions_created"] == 4
        assert "tkt_A" in c.get("/report").text


def test_cli_seed_and_record(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("DEVIN_API_KEY", raising=False)
    monkeypatch.setenv("SWE_LOOP_DB", str(tmp_path / "cli.sqlite"))
    monkeypatch.setenv("SWE_LOOP_REPLAY_DIR", str(tmp_path))
    assert main(["seed"]) == 0
    assert '"seeded": true' in capsys.readouterr().out
    assert main(["record", str(tmp_path / "run.json")]) == 0
    assert (tmp_path / "run.json").exists()
    assert main(["apply-config"]) == 2  # refuses outside live mode


def test_ops_page_lists_sessions_and_the_feed(tmp_path, monkeypatch):
    monkeypatch.delenv("DEVIN_API_KEY", raising=False)
    from swe_loop import ops

    st = seeded(tmp_path)
    o = ops.build(st)
    assert len(o["sessions"]) == 4 and o["counts"]["merged"] == 2 and o["counts"]["passed"] == 4
    d = {s["ticket"]: s for s in o["sessions"]}
    assert d["tkt_D"]["retries"] == 1 and d["tkt_A"]["gate"] == "pass"
    assert [s["name"] for s in d["tkt_A"]["steps"]][-1] == "merge"
    assert "merge" in [s["name"] for s in d["tkt_A"]["steps"] if "done" in s["cls"]]
    assert o["feed"] and o["feed"][0]["at"] >= o["feed"][-1]["at"]
    layers = {e["layer"] for e in st.timeline(limit=500)}
    assert {"route", "dispatch", "poll", "gate", "merge", "escalate"} <= layers
    app = build_app(Settings.from_env(), st, seed_replay=False)
    with TestClient(app) as c:
        html = c.get("/devin/sessions").text
        assert "Sessions" in html and "Cost is what the console charged" in html and "fake-" in html
        assert "Verification pass rate" in c.get("/report").text
        sid = o["sessions"][0]["id"]
        det = c.get(f"/sessions/{sid}").json()
        assert det["timeline"] and det["work_order"]["files"]
        assert c.get("/timeline?limit=5").json()
        assert c.get("/devin/sessions").status_code == 200


def test_a_refusal_is_not_counted_as_a_person_stepping_in(tmp_path):
    """The loop declined the work on a rule it already had. Nobody was asked anything and no
    time was spent, so counting it as intervention would make the loop look needier the better
    its rules got."""
    from swe_loop import rates
    from swe_loop.store import Store as S

    st = S(tmp_path / "s.sqlite")
    st.upsert_ticket(id="t1", source="scan", title="ran clean", status="merged")
    st.set_router_decision("t1", "devin", "took it on")
    st.upsert_ticket(id="t2", source="scan", title="handed over", status="escalated")
    st.set_router_decision("t2", "human_only", "a person decides")
    st.upsert_ticket(id="t3", source="scan", title="declined", status="refused")
    st.set_router_decision("t3", "refuse", "a change is already open on that file")

    i = rates.intervention(st)
    assert i["tickets"] == 2  # the refused one leaves the denominator
    assert i["untouched"] == 1 and i["handed_back"] == 1 and i["refused"] == 3 - 2
    labels = {r["label"]: r["n"] for r in i["rows"]}
    assert labels["refused, so never taken on"] == 1
    assert "handed back to your team" not in labels
