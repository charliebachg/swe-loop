"""The scan step: a session reads the repository and what it finds becomes tickets.

Nothing it reports is trusted beyond being written down. Each finding enters as a new ticket and
goes through the same scoping, routing, checks and review as work that arrived as an issue."""

from pathlib import Path

from fastapi.testclient import TestClient

from swe_loop import pages, scan
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
                    "url": "https://app.devin.ai/sessions/x",
                    "status": "exit",
                    "status_detail": "finished",
                    "acus_consumed": 0.0,
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
        assert a["max_findings"] == 5  # the seam says five; the page and the prompt follow it
        html = c.get("/automations?open=auto_scan").text
        assert "at most 5 tickets a run" in html and "tickets per run" in html


def test_the_scan_session_is_visible_and_counted(tmp_path, monkeypatch):
    """A scan spends money and does work, so it is a session like any other: on the page, with a
    link, a timeline of its own, and its cost in the total."""
    monkeypatch.delenv("DEVIN_API_KEY", raising=False)
    st = Store(tmp_path / "s.sqlite")
    client = DevinClient(FakeTransport(tmp_path))
    scan.run_scan(
        Settings(mode="replay"), CFG, st, client, sleep=lambda s: None, log=lambda m: None
    )
    rows = st.list_scan_sessions()
    assert len(rows) == 1 and rows[0]["outcome"] == "filed" and rows[0]["terminal_at"]
    assert rows[0]["url"].startswith("https://app.devin.ai/sessions/")
    events = [e["event"] for e in st.timeline(session_id=rows[0]["id"], limit=50)]
    assert "session started" in events and any("filed" in e for e in events)

    from swe_loop import cost as cost_mod

    assert cost_mod.spend(st)["n_sessions"] == 1  # it counts as a session, not as nothing
    app = build_app(Settings.from_env(), st, seed_replay=False)
    with TestClient(app) as c:
        html = c.get("/devin/sessions").text
        assert "read the repository and filed what it found" in html
        assert rows[0]["devin_session_id"][:12] in html


def test_a_restart_does_not_leave_a_run_looking_alive(tmp_path, monkeypatch):
    """Killing the app kills the thread, not the session. The page must say so rather than show
    a run that will never finish."""
    monkeypatch.delenv("DEVIN_API_KEY", raising=False)
    st = Store(tmp_path / "s.sqlite")
    rid = st.start_automation_run("auto_scan")
    st.set_setting("automation.running", "auto_scan")
    with TestClient(build_app(Settings.from_env(), st, seed_replay=False)):
        pass
    run = st.list_automation_runs("auto_scan")[0]
    assert run["status"] == "interrupted" and run["finished_at"]
    assert "restarted while this run was going" in run["result"]["error"]
    assert st.get_setting("automation.running") == ""
    assert rid == run["id"]


def test_a_lost_session_can_be_picked_back_up(tmp_path):
    """The session kept working; only our side of it went away."""

    class Alive:
        is_fake = True
        n = 0

        def status(self, sid):
            Alive.n += 1
            done = Alive.n > 1
            return type(
                "S",
                (),
                {
                    "session_id": sid,
                    "url": f"https://app.devin.ai/sessions/{sid}",
                    "status": "exit" if done else "running",
                    "status_detail": "finished" if done else "working",
                    "acus_consumed": 0.0,
                    "delivered": done,
                    "terminal": done,
                    "structured_output": GOOD if done else None,
                },
            )()

    st = Store(tmp_path / "s.sqlite")
    out = scan.adopt_scan(
        Settings(mode="replay"),
        CFG,
        st,
        Alive(),
        "lost-1",
        sleep=lambda s: None,
        log=lambda m: None,
    )
    assert out["kind"] == "filed" and len(out["new"]) == 1
    row = st.list_scan_sessions()[0]
    assert row["devin_session_id"] == "lost-1" and row["outcome"] == "filed"
    assert "picked the session back up" in " ".join(e["event"] for e in st.timeline(limit=20))


def test_a_finding_in_a_file_another_change_owns_is_refused(tmp_path):
    """Two open changes to one file collide at the merge. A scan that reports a file already
    being worked on is dropped here, not left for a person to notice at merge time."""
    st = Store(tmp_path / "s.sqlite")
    cfg = TargetConfig.load(ROOT / "configs" / "superset-pandas3.yaml")
    out = {
        "searched": "everything",
        "findings": [
            {
                "file": "superset/charts/client_processing.py",
                "line": 900,
                "class": "inplace",
                "why": "reserved: shard A is open against this file",
                "title": "in a file another change owns",
                "confidence": "certain",
                "tests": [],
            },
            {
                "file": "tests/unit_tests/x_test.py",
                "line": 1,
                "class": "inplace",
                "why": "forbidden path",
                "title": "under tests",
                "confidence": "certain",
                "tests": [],
            },
            {
                "file": "superset/utils/excel.py",
                "line": 42,
                "class": "downcasting",
                "why": "free ground",
                "title": "somewhere nobody is working",
                "confidence": "certain",
                "tests": [],
            },
        ],
    }
    filed = scan.file_findings(st, cfg, out, limit=5)
    assert filed["taken"] == ["superset/charts/client_processing.py"]
    assert filed["refused"] == ["tests/unit_tests/x_test.py"]
    assert len(filed["new"]) == 1
    titles = [t["title"] for t in st.list_tickets()]
    assert titles == ["somewhere nobody is working"]


def test_stopping_after_intake_files_tickets_and_spends_nothing_more(tmp_path, monkeypatch):
    """Finding out what a repository holds should not cost what fixing it costs."""
    from swe_loop import runner

    monkeypatch.delenv("DEVIN_API_KEY", raising=False)
    st = Store(tmp_path / "s.sqlite")
    cfg = TargetConfig.load(ROOT / "configs" / "superset-pandas3.yaml")
    settings = Settings.from_env()
    client = DevinClient.from_settings(settings)
    pages.seed_automations(st, cfg)
    pages.seed_playbooks(st, cfg)

    out = runner.run_automation(
        settings, cfg, st, client, "auto_scan", log=lambda _m: None, stop_after="intake"
    )
    assert out["stopped_after"] == "intake"
    assert out["new_tickets"]
    # a ticket exists, and nothing was scoped or dispatched against it
    assert st.list_tickets("new")
    assert st.list_triage_sessions() == []
    assert st._all("SELECT * FROM sessions") == []
    # the run is on the record like any other
    runs = st.list_automation_runs("auto_scan")
    assert runs and runs[0]["status"] == "done"
