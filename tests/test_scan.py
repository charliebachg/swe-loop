"""The scan step: a session reads the repository and what it finds becomes tickets.

Nothing it reports is trusted beyond being written down. Each finding enters as a new ticket and
goes through the same scoping, routing, checks and review as work that arrived as an issue."""

from pathlib import Path

from fastapi.testclient import TestClient

from swe_loop import scan
from swe_loop.app import build_app
from swe_loop.config import Settings, TargetConfig
from swe_loop.devin import DevinClient, FakeTransport
from swe_loop.store import Store

ROOT = Path(__file__).resolve().parents[1]
CFG = TargetConfig.load(ROOT / "configs" / "superset-pandas3.yaml")

GOOD = {
    "searched": "ran the unit tests with FutureWarning promoted to errors over superset/",
    "findings": [
        {
            "title": "pandas 3: chained assignment in superset/x.py drops the write",
            "file": "superset/x.py",
            "line": 42,
            "class": "chained-assignment",
            "why": "pandas emits ChainedAssignmentError here under 3.0.5",
            "tests": ["tests/unit_tests/x_test.py"],
            "confidence": "certain",
        }
    ],
}


def test_a_finding_is_refused_when_it_names_a_place_sessions_may_not_touch(tmp_path):
    st = Store(tmp_path / "s.sqlite")
    out = {
        "searched": "x",
        "findings": [
            {**GOOD["findings"][0], "file": "tests/unit_tests/x_test.py"},
            GOOD["findings"][0],
        ],
    }
    filed = scan.file_findings(st, CFG, out)
    assert filed["refused"] == ["tests/unit_tests/x_test.py"]
    assert len(filed["new"]) == 1
    assert st.get_ticket(filed["new"][0])["source"] == "scan"


def test_the_same_place_is_only_filed_once(tmp_path):
    st = Store(tmp_path / "s.sqlite")
    first = scan.file_findings(st, CFG, GOOD)
    again = scan.file_findings(st, CFG, GOOD)
    assert len(first["new"]) == 1 and again["new"] == [] and len(again["known"]) == 1
    assert scan.finding_id(GOOD["findings"][0]) == first["new"][0]


def test_output_that_does_not_say_how_it_knows_is_rejected():
    assert scan.validate({"searched": "x", "findings": []}) == []
    bad = {"searched": "", "findings": [{"title": "t", "file": "a.py", "class": "c"}]}
    problems = scan.validate(bad)
    assert "searched is missing" in problems
    assert any("no why" in p for p in problems) and any("no line" in p for p in problems)
    assert scan.validate("nonsense") == ["output is not an object"]


def test_a_scan_files_its_findings_and_the_loop_picks_them_up(tmp_path, monkeypatch):
    monkeypatch.delenv("DEVIN_API_KEY", raising=False)
    st = Store(tmp_path / "s.sqlite")
    client = DevinClient(FakeTransport(tmp_path))
    out = scan.run_scan(
        Settings(mode="replay"), CFG, st, client, sleep=lambda s: None, log=lambda m: None
    )
    assert out["kind"] == "filed" and len(out["new"]) == 1
    t = st.get_ticket(out["new"][0])
    assert t["status"] == "new" and t["source"] == "scan" and t["number"] == 1
    assert "synthesised finding" in t["title"]
    # the finding itself is kept, so triage can read what the scan actually saw
    ev = st._all("SELECT payload_json FROM events WHERE ticket_id=?", t["id"])
    assert "synthesised" in ev[0]["payload_json"]
    assert "a scan found something" in " ".join(e["event"] for e in st.timeline(limit=20))


def test_output_that_is_not_the_agreed_shape_files_nothing(tmp_path):
    """A session can say anything. Only what matches the shape becomes a ticket."""
    st = Store(tmp_path / "s.sqlite")

    class Says:
        is_fake = True

        def start(self, spec):
            return type(
                "S",
                (),
                {
                    "session_id": "x",
                    "delivered": True,
                    "terminal": True,
                    "structured_output": {"findings": "not a list"},
                },
            )()

    out = scan.run_scan(
        Settings(mode="replay"), CFG, st, Says(), sleep=lambda s: None, log=lambda m: None
    )
    assert out["kind"] == "invalid" and st.list_tickets() == []


def test_only_the_most_important_findings_are_kept(tmp_path):
    """A scan is told how many tickets it may file. What survives the cap is the strongest
    evidence, not whatever the session listed first."""
    st = Store(tmp_path / "s.sqlite")
    out = {
        "searched": "x",
        "findings": [
            {**GOOD["findings"][0], "file": "superset/a.py", "confidence": "unsure"},
            {**GOOD["findings"][0], "file": "superset/b.py", "confidence": "certain"},
            {**GOOD["findings"][0], "file": "superset/c.py", "confidence": "likely"},
        ],
    }
    filed = scan.file_findings(st, CFG, out, limit=2)
    assert filed["dropped"] == 1 and len(filed["new"]) == 2
    kept = {st.get_ticket(t)["title"] for t in filed["new"]}
    titles = {
        st.get_ticket(t)["id"]: st._one(
            "SELECT payload_json AS p FROM events WHERE ticket_id=?", t
        )["p"]
        for t in filed["new"]
    }
    assert any("superset/b.py" in p for p in titles.values())  # certain survived
    assert not any("superset/a.py" in p for p in titles.values())  # unsure was dropped
    assert kept  # and the tickets themselves exist


def test_the_automation_carries_the_number(tmp_path, monkeypatch):
    monkeypatch.delenv("DEVIN_API_KEY", raising=False)
    st = Store(tmp_path / "s.sqlite")
    app = build_app(Settings.from_env(), st, seed_replay=False)
    with TestClient(app) as c:
        a = st.get_automation("auto_scan")
        assert a["max_findings"] == 3
        html = c.get("/automations?open=auto_scan").text
        assert "at most 3 tickets a run" in html and "tickets per run" in html
