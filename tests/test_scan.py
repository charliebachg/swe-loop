"""The scan step: a session reads the repository and what it finds becomes tickets.

Nothing it reports is trusted beyond being written down. Each finding enters as a new ticket and
goes through the same scoping, routing, checks and review as work that arrived as an issue."""

from pathlib import Path

import pytest
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


def test_a_ticket_in_a_file_another_change_owns_is_refused_without_a_session(tmp_path):
    """The file is known from the finding, so working that out does not need a session. A
    ticket filed before the rule existed is refused on the next run, not scoped and then thrown
    away after the money is spent."""
    st = Store(tmp_path / "s.sqlite")
    cfg = TargetConfig.load(ROOT / "configs" / "superset-pandas3.yaml")
    reserved = cfg.scan["reserved_paths"][0]
    for tid, where in (("tkt_held", reserved), ("tkt_free", "superset/utils/core.py")):
        st.upsert_ticket(id=tid, source="scan", title=where, status="new")
        st.insert_event("scan", {"file": where, "line": 1}, ticket_id=tid)

    assert scan.refuse_reserved(st, cfg) == ["tkt_held"]
    held = st.get_ticket("tkt_held")
    assert held["status"] == "refused" and held["router_decision"] == "refuse"
    assert "waiting for the change already open on" in held["router_reason"]
    # the one on free ground is untouched and still waiting to be scoped
    assert st.get_ticket("tkt_free")["status"] == "new"
    # and it is idempotent: a second pass finds nothing left to refuse
    assert scan.refuse_reserved(st, cfg) == []


def test_the_prompt_names_an_area_and_never_the_defects_to_look_for():
    """Handing a scan the classes we already know about turns it into a grep for our own answer
    sheet. A finder that can only rediscover the inventory has found nothing."""
    cfg = TargetConfig.load(ROOT / "configs" / "superset-pandas3.yaml")
    p = scan.build_prompt(cfg, 5, ["superset/a.py:1"])
    assert "in the migration area" in p
    # the taxonomy our own inventory used must not appear anywhere in the instruction
    for defect in (
        "chained assignment",
        "copy-on-write",
        "string dtype",
        "stack and pivot",
        "downcasting on replace",
        "mixed-offset",
    ):
        assert defect not in p, f"the prompt names the defect {defect!r}"
    assert "Decide for yourself what kinds of thing to look for" in p
    # it is told what is already claimed, so it can tell new ground from old
    assert "superset/a.py:1" in p and "already on the board" in p


def test_the_board_a_scan_is_shown_is_every_place_already_claimed(tmp_path):
    st = Store(tmp_path / "s.sqlite")
    st.upsert_ticket(id="tkt_x", source="scan", title="t", status="new")
    st.insert_event("scan", {"file": "superset/a.py", "line": 12}, ticket_id="tkt_x")
    st.upsert_ticket(id="tkt_y", source="github", title="t", status="new")
    st.insert_work_order(
        ticket_id="tkt_y", shard_id="A", files=["superset/b.py"], tests=[], acceptance={}
    )
    assert scan.known_sites(st) == ["superset/a.py:12", "superset/b.py"]


# ---------------------------------------------------------------- Devin's own scanner
def test_devins_scanner_is_started_with_an_area_and_its_findings_become_tickets(tmp_path):
    """Devin ships a scanner. This runs it rather than describing one: the loop starts a scan
    with an area, waits, and files what came back."""
    from swe_loop import codescan

    st = Store(tmp_path / "s.sqlite")
    cfg = TargetConfig.load(ROOT / "configs" / "superset-pandas3.yaml")
    settings = Settings.from_env()
    client = DevinClient(FakeTransport())

    out = codescan.run(settings, cfg, st, client, log=lambda _m: None)
    assert out["kind"] == "filed" and out["findings"] == 1
    started = [c for c in client.t.calls if c[0] == "start_code_scan"]
    assert started and started[0][1] == {
        "repo_name": cfg.repo,
        "scan_type": "security",
    }, "an area, never a defect"

    t = st.get_ticket(out["new"][0])
    assert t["source"] == "code_scan"
    assert t["title"] == "Unvalidated redirect in the login flow"
    # the scan is on the board as a session, with the orchestrator session it ran under
    sc = st.list_scan_sessions()[0]
    assert sc["outcome"] == "filed" and sc["devin_session_id"] == "fake-scan-001"


def test_a_security_finding_is_a_question_for_a_person_not_a_job_for_a_session(tmp_path):
    """Superset requires an automated security finding to name the SECURITY.md capability row
    and the principal, and says one that cannot name both is a question, not a vulnerability.
    Devin's scanner returns neither, so none of these is handed to a session."""
    from swe_loop import codescan

    st = Store(tmp_path / "s.sqlite")
    cfg = TargetConfig.load(ROOT / "configs" / "superset-pandas3.yaml")
    out = codescan.run(
        Settings.from_env(), cfg, st, DevinClient(FakeTransport()), log=lambda _m: None
    )
    tid = out["new"][0]
    assert out["questions"] == [tid]
    t = st.get_ticket(tid)
    assert t["router_decision"] == "human_only" and t["status"] == "escalated"
    assert "SECURITY.md" in t["router_reason"] and "question" in t["router_reason"]
    assert st.list_escalations()


def test_devins_findings_respect_the_same_boundaries_as_our_own(tmp_path):
    """A finding in a forbidden path or a file another change owns never reaches the board,
    whichever scanner found it."""
    from swe_loop import codescan

    st = Store(tmp_path / "s.sqlite")
    cfg = TargetConfig.load(ROOT / "configs" / "superset-pandas3.yaml")

    def f(path, fid):
        return {
            "finding_id": fid,
            "title": path,
            "severity": "high",
            "category": "x",
            "reference_snippets": [{"file_path": path, "start_line": 1}],
        }

    out = codescan.file_findings(
        st,
        cfg,
        [
            f("tests/unit_tests/a_test.py", "a"),
            f(cfg.scan["reserved_paths"][0], "b"),
            f("superset/views/base.py", "c"),
        ],
        limit=5,
    )
    assert out["refused"] == ["tests/unit_tests/a_test.py"]
    assert out["taken"] == [cfg.scan["reserved_paths"][0]]
    assert len(out["new"]) == 1


def test_an_unconfirmed_security_finding_keeps_its_detail_off_a_shared_screen(tmp_path):
    """The dashboard goes on a screen other people can see. A file and a line read off it is an
    unreviewed vulnerability report about somebody else's software, with no disclosure process
    behind it, so the row says the kind and withholds the rest until someone confirms it."""
    from swe_loop import codescan

    st = Store(tmp_path / "s.sqlite")
    st.upsert_ticket(
        id="tkt_cs1",
        source="code_scan",
        title="IDOR in superset/views/sql_lab/views.py:164",
        status="escalated",
        cls="other-idor",
    )
    st.set_router_decision("tkt_cs1", "human_only", "... name the row in SECURITY.md ...")
    t = st.get_ticket("tkt_cs1")

    assert codescan.masked(st) is True  # withheld unless a person turns it off
    hidden = codescan.safe_title(t, True)
    assert "sql_lab" not in hidden and "164" not in hidden
    assert hidden == "insecure object reference, detail withheld until someone confirms it"
    # the detail is still there for whoever needs it
    assert codescan.safe_title(t, False) == t["title"]
    st.set_setting(codescan.MASK_SETTING, "1")
    assert codescan.masked(st) is False

    # a ticket from anywhere else is never touched by this
    st.upsert_ticket(id="tkt_x", source="scan", title="pandas thing at a.py:1", status="new")
    assert codescan.safe_title(st.get_ticket("tkt_x"), True) == "pandas thing at a.py:1"


def test_a_non_security_scan_says_what_it_needs_before_devin_refuses_it(tmp_path):
    """Only security runs without a scan profile. Devin answers 400 for the other nine, and a
    profile cannot be made through the API at all: every write on the profile endpoints is 405
    and the spec has no operation that creates one. Saying so here is cheaper than the round
    trip, and tells a person where to go."""
    from swe_loop import codescan

    st = Store(tmp_path / "s.sqlite")
    cfg = TargetConfig.load(ROOT / "configs" / "superset-pandas3.yaml")
    client = DevinClient(FakeTransport())

    with pytest.raises(ValueError) as e:
        codescan.run(Settings.from_env(), cfg, st, client, area="performance", log=lambda _m: None)
    assert "needs a scan profile" in str(e.value) and "Devin console" in str(e.value)
    assert not [c for c in client.t.calls if c[0] == "start_code_scan"]  # nothing was started

    # with a profile it goes through, and the profile travels with the request
    out = codescan.run(
        Settings.from_env(),
        cfg,
        st,
        client,
        area="performance",
        profile_id="prof-123",
        log=lambda _m: None,
    )
    assert out["kind"] == "filed"
    started = next(c for c in client.t.calls if c[0] == "start_code_scan")[1]
    assert started == {
        "repo_name": cfg.repo,
        "scan_type": "performance",
        "profile_id": "prof-123",
    }


def test_one_repository_can_be_searched_in_more_than_one_area(tmp_path):
    """The area is a run-time choice, so the same repository can be read for a migration one day
    and for performance the next, without a session ever being told which defects to find."""
    cfg = TargetConfig.load(ROOT / "configs" / "superset-pandas3.yaml")
    mig = scan.build_prompt(cfg, 5, [], area="migration")
    perf = scan.build_prompt(cfg, 5, [], area="performance")
    assert "in the migration area" in mig and "in the performance area" in perf
    assert "repeated queries where one would answer" in perf
    assert "Not a security review" in perf
    # neither one hands over a list of defect classes
    for p in (mig, perf):
        for defect in ("chained assignment", "string dtype", "N+1", "SELECT *"):
            assert defect not in p
    # the seam's own area is the default
    assert "in the migration area" in scan.build_prompt(cfg, 5, [])


def test_devin_fixes_its_own_finding_and_the_loop_checks_the_result(tmp_path):
    """Devin's scanner opens the pull request itself, which is its feature and not ours to
    rebuild. What the loop adds is the part Devin cannot do for itself: the change is re-checked
    here before anyone is asked to merge it."""
    from swe_loop import codescan

    st = Store(tmp_path / "s.sqlite")
    cfg = TargetConfig.load(ROOT / "configs" / "superset-pandas3.yaml")
    client = DevinClient(FakeTransport())
    codescan.run(Settings.from_env(), cfg, st, client, log=lambda _m: None)
    tid = st.list_tickets("escalated")[0]["id"]

    out = codescan.remediate(Settings.from_env(), cfg, st, client, tid, log=lambda _m: None)
    assert out["kind"] == "opened"
    assert out["pr"].startswith("https://github.com/")
    called = next(c for c in client.t.calls if c[0] == "remediate_finding")
    assert called[1] == ("fake-scan-001", "fake-finding-001")
    assert st.get_ticket(tid)["status"] == "dispatched"
    events = [e["event"] for e in st.timeline(ticket_id=tid)]
    assert "Devin is fixing its own finding" in events
    assert "a pull request was opened" in events


def test_a_fix_with_no_test_behind_it_is_not_called_verified(tmp_path):
    """A remediation arrives with no work order, so there are no acceptance commands to inherit.
    The linter always runs; a test runs only where one exists, and where none does the gate has
    nothing to record but that."""
    from swe_loop import codescan

    # a tree of its own, not the clone beside this repository: that one is a working copy on
    # whoever's machine, so a test that reads it passes here and fails on a fresh checkout
    root = tmp_path / "repo"
    (root / "tests/unit_tests/databases").mkdir(parents=True)
    (root / "tests/unit_tests/databases/api_test.py").write_text("")

    only_lint = codescan.acceptance_for(["superset/views/sql_lab/views.py"], root)
    assert list(only_lint) == ["lint"], "no test behind it, so nothing but the linter"
    assert codescan.acceptance_for([], root) == {}
    with_test = codescan.acceptance_for(["superset/databases/api.py"], root)
    assert any(k.startswith("tests ") for k in with_test), "a test exists, so it runs"


def test_a_remediation_is_scoped_by_the_finding_not_by_its_own_pull_request(tmp_path):
    """Building the work order from the pull request would make the scope check pass by
    construction, which is not a check. It is built from the file the finding named, so a fix
    that wandered somewhere else is caught the same as any other."""
    from swe_loop import codescan

    st = Store(tmp_path / "s.sqlite")
    cfg = TargetConfig.load(ROOT / "configs" / "superset-pandas3.yaml")
    client = DevinClient(FakeTransport())
    codescan.run(Settings.from_env(), cfg, st, client, log=lambda _m: None)
    tid = st.list_tickets("escalated")[0]["id"]

    sid = codescan.gate_remediation(
        Settings.from_env(),
        cfg,
        st,
        tid,
        "https://github.com/o/r/pull/91",
        "dev-remediation-1",
        log=lambda _m: None,
    )
    ses = st.get_session(sid)
    wo = st.get_work_order(ses["work_order_id"])
    # the fixture finding points at superset/views/base.py, and that is the whole scope
    assert wo["files"] == ["superset/views/base.py"]
    assert ses["pull_request_url"] == "https://github.com/o/r/pull/91"
    assert ses["devin_session_id"] == "dev-remediation-1"
    assert "lint" in wo["acceptance"]


def test_waiting_is_a_state_work_passes_through_not_where_it_dies(tmp_path):
    """A ticket set aside because another change had that file open goes back in the queue when
    that change lands. Without this it waits for good and somebody has to notice and push it,
    which is the sort of quiet chore this loop exists to remove."""
    from swe_loop import scan as scan_mod

    st = Store(tmp_path / "s.sqlite")
    cfg = TargetConfig.load(ROOT / "configs" / "superset-pandas3.yaml")
    where = "superset/utils/excel.py"  # free ground, not in the seam's reserved list

    # something else has that file open, so the finding is set aside
    st.upsert_ticket(id="tkt_open", source="github", title="in flight", status="routed")
    st.insert_work_order(ticket_id="tkt_open", shard_id="A", files=[where], tests=[], acceptance={})
    st.upsert_ticket(id="tkt_wait", source="scan", title="found later", status="new")
    st.insert_event("scan", {"file": where, "line": 3}, ticket_id="tkt_wait")
    st.set_router_decision("tkt_wait", "refuse", f"waiting for the change open on {where}")
    assert st.get_ticket("tkt_wait")["status"] == "refused"
    assert scan_mod.release_waiting(st, cfg) == []  # still blocked

    # the change lands, and the next run picks the waiting ticket up
    st.set_ticket_status("tkt_open", "merged")
    assert scan_mod.release_waiting(st, cfg) == ["tkt_wait"]
    freed = st.get_ticket("tkt_wait")
    assert freed["status"] == "new" and not freed["router_decision"]
    assert any(e["event"] == "back in the queue" for e in st.timeline(ticket_id="tkt_wait"))
    # a file the seam holds back is never released, however quiet the board goes
    st.upsert_ticket(id="tkt_res", source="scan", title="reserved", status="new")
    st.insert_event("scan", {"file": cfg.scan["reserved_paths"][0], "line": 1}, ticket_id="tkt_res")
    st.set_router_decision("tkt_res", "refuse", "held back")
    assert scan_mod.release_waiting(st, cfg) == []


def test_a_scan_devins_schedule_started_is_picked_up_without_starting_another(tmp_path):
    """The schedule fires on Devin's side and there is no webhook out.

    So a scan can exist that this loop never asked for. Finding it means looking: any scan on the
    organisation with no row in this store was started by something else, and on this
    organisation that means the schedule. It has to be followed and filed like any other, and it
    must not cause a second scan to be started beside it.
    """
    from swe_loop import codescan

    st = Store(tmp_path / "s.sqlite")
    cfg = TargetConfig.load(ROOT / "configs" / "superset-pandas3.yaml")
    settings = Settings.from_env()
    client = DevinClient(FakeTransport())
    # a scan that is simply there, the way one is after the schedule has fired
    client.t._scan = {
        "scan_id": "scan-fromtheschedule",
        "repo_name": cfg.repo,
        "status": "running",
        "scan_type": "security",
        "created_at": 0,
    }

    out = codescan.adopt(settings, cfg, st, client, log=lambda _m: None)
    assert out["kind"] == "adopted" and len(out["adopted"]) == 1
    assert out["adopted"][0]["kind"] == "filed"
    assert not [c for c in client.t.calls if c[0] == "start_code_scan"], "never a second scan"

    rows = st.list_scan_sessions()
    assert [r["devin_session_id"] for r in rows] == ["scan-fromtheschedule"]

    # and it is not picked up twice
    again = codescan.adopt(settings, cfg, st, client, log=lambda _m: None)
    assert again["kind"] == "none"
    assert len(st.list_scan_sessions()) == 1


def test_a_scan_this_loop_started_is_not_adopted_as_someone_elses(tmp_path):
    from swe_loop import codescan

    st = Store(tmp_path / "s.sqlite")
    cfg = TargetConfig.load(ROOT / "configs" / "superset-pandas3.yaml")
    settings = Settings.from_env()
    client = DevinClient(FakeTransport())

    codescan.run(settings, cfg, st, client, log=lambda _m: None)
    before = len(st.list_scan_sessions())
    out = codescan.adopt(settings, cfg, st, client, log=lambda _m: None)
    assert out["kind"] == "none"
    assert len(st.list_scan_sessions()) == before


def test_new_findings_on_a_scan_we_already_hold_are_picked_up_too(tmp_path):
    """A schedule bound to an existing scan re-runs that scan against the new commits.

    So the work can arrive as findings on a scan this store already has a row for, rather than as
    a scan it has never seen. That is the same event wearing different clothes, and missing it
    would mean the schedule fires, Devin finds something, and the board stays empty.
    """
    from swe_loop import codescan

    st = Store(tmp_path / "s.sqlite")
    cfg = TargetConfig.load(ROOT / "configs" / "superset-pandas3.yaml")
    settings = Settings.from_env()
    client = DevinClient(FakeTransport())

    codescan.run(settings, cfg, st, client, log=lambda _m: None)
    first = {t["id"] for t in st.list_tickets()}
    assert first, "the first scan filed something"

    # the schedule fires: the same scan, a finding that was not there before
    client.t._findings = [
        {
            "finding_id": "sfind-fromtheschedule",
            "title": "found after the new commits landed",
            "severity": "high",
            "file_path": "superset/models/core.py",
            "line": 12,
            "category": "other-info-disclosure",
        }
    ]
    out = codescan.adopt(settings, cfg, st, client, log=lambda _m: None)

    assert out["kind"] == "adopted"
    assert not [c for c in client.t.calls if c[0] == "start_code_scan"][1:], "no second scan"
    after = {t["id"] for t in st.list_tickets()}
    assert len(after) > len(first), "the new finding became a ticket"


def test_the_cap_counts_tickets_made_not_findings_read(tmp_path):
    """Capping before deduping spends the whole allowance on findings already on the board."""
    from swe_loop import codescan

    st = Store(tmp_path / "s.sqlite")
    cfg = TargetConfig.load(ROOT / "configs" / "superset-pandas3.yaml")
    already = [
        {
            "finding_id": f"sfind-old{i}",
            "title": f"old {i}",
            "severity": "critical",
            "file_path": f"superset/old{i}.py",
            "line": 1,
        }
        for i in range(3)
    ]
    fresh = [
        {
            "finding_id": "sfind-new",
            "title": "new one, lower severity",
            "severity": "low",
            "file_path": "superset/new.py",
            "line": 1,
        }
    ]
    codescan.file_findings(st, cfg, already, limit=3)
    assert len(st.list_tickets()) == 3

    out = codescan.file_findings(st, cfg, already + fresh, limit=3)
    assert out["new"], "the new finding is filed even though three older ones sort above it"
    assert len(out["known"]) == 3


def test_a_watcher_tick_never_starts_a_scan_of_its_own(tmp_path, monkeypatch):
    """Devin has no outbound webhook, so a schedule there needs something here to be looking.

    The point of looking is that Devin decides when work happens. A tick that started its own
    scan when it found nothing would make the schedule beside the point, and would spend money
    on every tick besides.
    """
    from swe_loop import pages, runner

    monkeypatch.setenv("SWE_LOOP_MODE", "replay")
    st = Store(tmp_path / "s.sqlite")
    cfg = TargetConfig.load(ROOT / "configs" / "superset-pandas3.yaml")
    settings = Settings.from_env()
    client = DevinClient(FakeTransport())
    pages.seed_automations(st, cfg)

    out = runner.run_automation(
        settings, cfg, st, client, "auto_codescan", log=lambda _m: None, only_if_scheduled=True
    )
    assert out["scan"] == "nothing scheduled"
    assert not [c for c in client.t.calls if c[0] == "start_code_scan"]
    assert not st.list_tickets()


def test_a_finding_we_read_before_is_not_reported_as_the_schedules_work(tmp_path):
    """A cap leaves findings read but unfiled. Those are not what a schedule produced.

    Treating them as new work puts a run on the board that nothing on Devin's side caused, logs
    "the schedule scanned the new commits" when it did not, and wakes the whole pipeline on a
    timer that never fired. It cost a real run before this test existed.
    """
    from swe_loop import codescan

    st = Store(tmp_path / "s.sqlite")
    cfg = TargetConfig.load(ROOT / "configs" / "superset-pandas3.yaml")
    settings = Settings.from_env()
    client = DevinClient(FakeTransport())
    two = [
        {
            "finding_id": f"sfind-{i}",
            "title": f"finding {i}",
            "severity": "high",
            "file_path": f"superset/f{i}.py",
            "line": 1,
        }
        for i in range(2)
    ]
    client.t._findings = two

    # a scan that read two findings but was only allowed to file one
    codescan.run(settings, cfg, st, client, limit=1, log=lambda _m: None)
    assert len(st.list_tickets()) == 1, "the cap held"

    # looking again finds nothing Devin has not already reported
    out = codescan.adopt(settings, cfg, st, client, limit=5, log=lambda _m: None)
    assert out["kind"] == "none", "an unfiled finding is not a scan result"
    assert len(st.list_tickets()) == 1

    # but a finding Devin had not reported before is
    client.t._findings = two + [
        {
            "finding_id": "sfind-actuallynew",
            "title": "after the new commits",
            "severity": "high",
            "file_path": "superset/new.py",
            "line": 1,
        }
    ]
    out2 = codescan.adopt(settings, cfg, st, client, limit=5, log=lambda _m: None)
    assert out2["kind"] == "adopted"
    assert len(st.list_tickets()) > 1


def test_a_schedule_that_ran_and_found_nothing_is_recorded_not_silent(tmp_path, monkeypatch):
    """A run that extends a finished scan and turns up nothing new changes no scan and no finding.

    So watching scans and findings cannot tell "the schedule ran and cleared the repository" from
    "the schedule never fired". For ninety minutes the board said nothing while Devin had run
    twice. The evidence is the sessions: origin automation, carrying the automation id Devin gave
    us, the one with no parent being the run. Those have to be recorded, and the quiet outcome
    has to be named.
    """
    from swe_loop import codescan, pages, runner

    monkeypatch.setenv("SWE_LOOP_MODE", "replay")
    st = Store(tmp_path / "s.sqlite")
    cfg = TargetConfig.load(ROOT / "configs" / "superset-pandas3.yaml")
    settings = Settings.from_env()
    client = DevinClient(FakeTransport())
    pages.seed_automations(st, cfg)
    codescan.run(settings, cfg, st, client, log=lambda _m: None)  # the scan the schedule extends
    st.set_automation("auto_codescan", devin_automation_id="auto-theirs")

    # the schedule fired: an orchestrator and three children, all Devin's, none ours
    client.t.foreign_sessions = [
        {
            "session_id": "orch-2100",
            "origin": "automation",
            "automation_id": "auto-theirs",
            "parent_session_id": None,
            "child_session_ids": ["c1", "c2", "c3"],
            "status": "running",
            "status_detail": "waiting_for_user",
            "title": "Incremental security re-scan",
            "created_at": 1788555626,
        },
        *[
            {
                "session_id": c,
                "origin": "automation",
                "automation_id": "auto-theirs",
                "parent_session_id": "orch-2100",
                "child_session_ids": [],
                "status": "running",
                "status_detail": "working",
                "created_at": 1788555700,
            }
            for c in ("c1", "c2", "c3")
        ],
        # a session from some other automation entirely: not ours to record
        {
            "session_id": "someone-elses",
            "origin": "automation",
            "automation_id": "auto-not-ours",
            "parent_session_id": None,
            "child_session_ids": [],
            "status": "exit",
            "status_detail": "finished",
            "created_at": 1788555000,
        },
    ]

    out = runner.run_automation(
        settings, cfg, st, client, "auto_codescan", log=lambda _m: None, only_if_scheduled=True
    )
    assert out["scan"] == "ran, nothing new", out
    assert out["scheduled_runs"] == 1

    runs = st.list_automation_runs("auto_codescan")
    theirs = [r for r in runs if r["result"].get("started_by") == "Devin's schedule"]
    assert len(theirs) == 1, "one run per orchestrator, children are not runs"
    assert theirs[0]["result"]["orchestrator"] == "orch-2100"
    assert theirs[0]["result"]["sessions"] == 4
    assert theirs[0]["started_at"].startswith("2026-09-04T21:00"), (
        "started when Devin says, not when we looked"
    )
    assert not [r for r in runs if r["result"].get("orchestrator") == "someone-elses"]

    # and it is written once, not once per tick
    runner.run_automation(
        settings, cfg, st, client, "auto_codescan", log=lambda _m: None, only_if_scheduled=True
    )
    again = [
        r
        for r in st.list_automation_runs("auto_codescan", limit=50)
        if r["result"].get("orchestrator") == "orch-2100"
    ]
    assert len(again) == 1

    # the log says it plainly
    events = [e["event"] for e in st.timeline(limit=50)]
    assert "Devin's schedule ran" in events
    assert "Devin's schedule ran and found nothing new" in events


def test_a_quiet_watcher_tick_leaves_no_run_behind(tmp_path, monkeypatch):
    """A watcher looks every minute. If each look that found nothing wrote a row, eighty five
    identical rows would bury the two runs that mattered. Looking is not a run."""
    from swe_loop import pages, runner

    monkeypatch.setenv("SWE_LOOP_MODE", "replay")
    st = Store(tmp_path / "s.sqlite")
    cfg = TargetConfig.load(ROOT / "configs" / "superset-pandas3.yaml")
    settings = Settings.from_env()
    client = DevinClient(FakeTransport())
    pages.seed_automations(st, cfg)
    before = len(st.list_automation_runs("auto_codescan", limit=100))
    for _ in range(3):
        out = runner.run_automation(
            settings, cfg, st, client, "auto_codescan", log=lambda _m: None, only_if_scheduled=True
        )
        assert out["scan"] == "nothing scheduled"
    assert len(st.list_automation_runs("auto_codescan", limit=100)) == before
