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


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("DEVIN_API_KEY", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    st = Store(tmp_path / "t.sqlite")
    synthesise(st, CFG, INVENTORY / "tickets.json", tmp_path)
    app = build_app(Settings.from_env(), st, seed_replay=False)
    with TestClient(app) as c:
        yield c, st


def test_home_shows_now_needs_you_and_recent(client):
    c, _st = client
    r = c.get("/")
    assert r.status_code == 200
    html = r.text
    assert "Needs you" in html and "What just happened" in html
    assert "tkt_E" in html and "needs you" in html  # the escalation
    assert "ready to merge" in html  # C and D passed and were reviewed
    assert "RECORDED RUN" not in html and "charliebachg/superset" in html and "Backstop" in html
    assert "\u2014" not in html  # no em dashes


def test_settings_shows_checks_seam_and_budget(client):
    c, st = client
    html = c.get("/settings").text
    assert "checking the connection" in html  # the checks arrive after the page is drawn
    assert "not checked" in c.get("/settings/checks").text  # replay: no tokens, nothing is called
    assert "configs/superset-pandas3.yaml" in html
    assert "tests/" in html and "the lower bound does not move" in html
    r = c.post(
        "/settings/budget",
        content="acu_cap=120&per_session_cap=4",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    b = st.budget_state()
    assert b["cap"] == 120 and b["per_session_cap"] == 4
    assert "120" in c.get("/settings").text
    r = c.post(
        "/settings/budget",
        content="acu_cap=-1&per_session_cap=4",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 400


def test_sidebar_links_every_module(client):
    c, _ = client
    html = c.get("/").text
    for href in (
        "/automations",
        "/tickets-page",
        "/report",
        "/devin/sessions",
        "/devin/playbooks",
        "/devin/knowledge",
        "/devin/insights",
        "/settings",
    ):
        assert f'href="{href}"' in html
    # the pages that left the sidebar are gone, not merely hidden
    for href in ("/devin/review", "/devin/integrations", "/devin/next"):
        assert href not in html
        assert c.get(href).status_code == 404


def test_settings_store_helpers(tmp_path):
    st = Store(tmp_path / "s.sqlite")
    assert st.get_setting("x") is None and st.get_setting("x", "d") == "d"
    st.set_setting("x", "1")
    st.set_setting("x", "2")
    assert st.get_setting("x") == "2"


def test_tickets_page_groups_by_source(client):
    c, _ = client
    html = c.get("/tickets-page").text
    # the board says who found a ticket, and Devin's own scanner is its own source
    for group in ("General", "Found by a session", "Found by Devin"):
        assert group in html, group  # the apostrophe is escaped in the markup
    assert "Needs you:" in html and "never edits tests" in html
    assert "oracle" not in html and "site(s)" not in html  # no internal vocabulary
    assert 'href="https://github.com/charliebachg/superset/issues/4"' in html
    assert html.count("/tickets-page?open=tkt_") >= 5
    assert "Merged by your team." in html  # the one-line summary per ticket
    pipe = c.get("/tickets-page?view=pipeline").text
    assert "right now" in pipe and 'title="Scoped:' in pipe


def test_tracker_rows_stages_and_merge(client):
    c, st = client
    html = c.get("/tracker?open=tkt_A,tkt_B,tkt_D,tkt_E").text
    assert 'id="tkt_D"' in html and "the session said" in html and "the checks found" in html
    assert "retries 1" in html  # D failed T1 once and passed on retry
    assert "Merged by a person" in html  # A and B
    assert "Ready to merge" in html  # C and D
    assert "Routed to a person" in html  # E
    # the strip: A has every stage done including merge
    a = html.split('id="tkt_A"')[1].split("</summary>")[0]
    assert a.count("done") >= 3  # four steps, and the last is the merge
    # record a merge through the form
    r = c.post(
        "/tickets/tkt_D/merge-form",
        content="actor=someone",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 200 and st.get_ticket("tkt_D")["status"] == "merged"
    # not ready: E cannot be merged
    c.post(
        "/tickets/tkt_E/merge-form",
        content="actor=someone",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert st.get_ticket("tkt_E")["status"] == "escalated"


def test_sessions_page_eta_parent_and_drawer(client):
    c, st = client
    html = c.get("/devin/sessions").text
    assert "fake-" in html and "wrote the fix and opened a pull request" in html
    assert "ticket" in html and "checks" in html  # the columns that remain say something
    # a live session gets an estimate from the finished ones (give them a real elapsed time)
    st.conn.execute(
        "UPDATE sessions SET terminal_at = datetime(created_at, '+25 minutes') WHERE terminal_at IS NOT NULL"
    )
    wo = st.work_orders_for("tkt_D")[0]
    sid = st.reserve_session(work_order_id=wo["id"], playbook_id=None, tags=["swe-loop"])
    st.bind_devin_session(
        sid, devin_session_id="devin-live", url="https://app.devin.ai/sessions/x", status="running"
    )
    html = c.get("/devin/sessions").text
    assert "devin-live" in html
    first = st._all("SELECT id FROM sessions ORDER BY rowid LIMIT 1")[0]["id"]
    d = c.get(f"/devin/sessions?drawer={first}").text
    assert "timeline" in d and "structured output" in d and "self_reported_done" in d
    assert c.get("/devin/sessions?drawer=nope").status_code == 200  # unknown id: no drawer


def test_automations_run_now_dispatches_a_routed_ticket(client):
    c, st = client
    html = c.get("/automations").text
    assert "Issues from repo" in html and "Scan" in html and "Add automation" in html
    assert ">Run<" in html and "replay" in html
    # a fresh routed ticket, then run now dispatches it on the fake transport
    st.upsert_ticket(
        id="tkt_X",
        source="manual",
        title="x",
        status="triaged",
        triage_verdict={"acceptance_cmd": {"p3": "true"}, "sites": [], "split": "one"},
    )
    st.insert_work_order(
        ticket_id="tkt_X",
        shard_id="X",
        files=["superset/x.py"],
        tests=["t"],
        acceptance={"p3": "true"},
    )
    before = len(st._all("SELECT id FROM sessions"))
    r = c.post("/automations/auto_repair/run")
    assert r.status_code == 200
    th = c.app.state.run_thread
    th.join(timeout=30)
    assert not th.is_alive()
    assert len(st._all("SELECT id FROM sessions")) == before + 1
    assert st.get_automation("auto_repair")["last_run"]
    assert "gate" in " ".join(e["layer"] for e in st.timeline(ticket_id="tkt_X", limit=50))
    assert st.get_ticket("tkt_X")["status"] == "gated"


def test_capability_pages_render_real_state(client):
    c, _ = client
    html = c.get("/devin/playbooks").text
    assert "Repair one shard" in html and "Triage a dependency-upgrade ticket" in html
    repair = c.get("/partials/playbook/pb_repair").text
    triage = c.get("/partials/playbook/pb_triage").text
    assert "<h4>Forbidden Actions</h4>" in repair and "self_reported_done" in repair
    assert "<h4>Forbidden Actions</h4>" in triage and "acceptance_cmd" in triage
    html = c.get("/devin/knowledge").text
    assert html.count("read when") == 7 and "ruff" in html and "oxlint" in html
    # the badge states a fact we can check, never a signal Devin does not send us
    assert "lower bound" in html and "not sent yet" in html
    assert "not yet used" not in html
    html = c.get("/devin/insights").text
    # Insights mirrors Devin's own record; with no insights fetched it says so rather
    # than inventing numbers of ours
    assert "read from Devin's own record and not measured by us" in html
    # every session the loop started is listed, with Generate where Devin's analysis is not written
    assert (
        "0 of" in html and ">Generate<" in html and ">View<" not in html.split("Every session")[-1]
    )
    body = html.split("<main", 1)[-1]
    assert "ACU per session" not in body and ">ACU<" not in body  # nothing this plan cannot report
    for path in (
        "/devin/playbooks",
        "/devin/knowledge",
        "/devin/insights",
    ):
        assert "\u2014" not in c.get(path).text


def test_insights_mirrors_devin_and_invents_nothing(tmp_path):
    """The Insights page reads Devin's own record. With a payload stored it shows the fields
    that vary and states the ones that do not; with none it says so rather than filling in."""
    from swe_loop import insights as ins

    st = Store(tmp_path / "s.sqlite")
    st.put_insight(
        "abc123def456",
        {
            "session_id": "abc123def456",
            "url": "https://app.devin.ai/sessions/abc123def456",
            "title": "scope a ticket",
            "session_size": "xs",
            "num_user_messages": 1,
            "num_devin_messages": 2,
            "playbook_id": None,
            "origin": "api",
            "analysis_status": "completed",
            "analysis": {
                "issues": [],
                "action_items": [],
                "suggested_prompt": None,
                "note_usage": None,
                "classification": {
                    "category": "migrations_and_upgrades",
                    "confidence": 0.95,
                    "tools_and_frameworks": ["pytest", "pandas"],
                },
            },
        },
    )
    rows = st.insights()
    assert ins.turns(rows) == {
        "one": 1,
        "total": 1,
        "replies": [{"n": 2, "sessions": 1}],
    }
    assert ins.tools(rows) == [{"name": "pandas", "n": 1}, {"name": "pytest", "n": 1}]
    # a session with no playbook is a configuration gap the page must surface
    assert [s["session"] for s in ins.no_playbook(rows)] == ["abc123de"]
    # a classification alone is not an analysis: nothing is counted as analysed until Devin
    # has written its issues, timeline or action items
    adv = ins.advice(rows)
    assert adv["issues"] == [] and adv["actions"] == [] and adv["analysed"] == 0
    assert ins.written(rows[0]) is False
    # a field with one value everywhere is stated once, not given a column
    assert {"field": "origin", "value": "api"} in ins.constants(rows)


def test_a_designed_page_returns_the_content_block_to_htmx(client):
    """An HTMX request swaps into #page, so the response must not carry the frame with it.
    Insights once returned the whole shell and drew a second sidebar inside the first."""
    c, _st = client
    for path in ("/", "/automations", "/tickets-page", "/report", "/devin/insights"):
        whole = c.get(path).text
        assert whole.count("<aside") == 1, path
        fragment = c.get(path, headers={"HX-Request": "true"}).text
        assert "<aside" not in fragment, path
        assert "<!doctype html>" not in fragment.lower(), path


def test_the_switch_on_a_devin_held_schedule_moves_devin_and_reads_back(client):
    """A row that says Devin runs the schedule must not have a switch that only moves ours.

    The page tells a reader "every weekday at 06:00 on a schedule Devin runs". The button beside
    that sentence has to move Devin's automation, and what it then shows has to come from Devin
    rather than from what we assumed the click did.
    """
    c, st = client
    st.set_automation("auto_codescan", devin_automation_id="auto-abc123")
    transport = c.app.state.client.t
    transport.automations = [
        {"automation_id": "auto-abc123", "name": "Scan new commits", "enabled": False}
    ]

    r = c.post("/automations/auto_codescan/schedule")
    assert r.status_code == 200
    assert ("update_automation", ("auto-abc123", {"enabled": True})) in transport.calls

    assert "switched on" in r.text and "Switch it off" in r.text

    # Devin can still answer a list read with the value from before the change, so the line has
    # to come from what Devin said to the change itself
    transport.lag_automation_reads = True
    r2 = c.post("/automations/auto_codescan/schedule")
    assert ("update_automation", ("auto-abc123", {"enabled": False})) in transport.calls
    assert "switched off" in r2.text and "Switch it on" in r2.text


def test_our_switch_does_not_touch_devins_schedule(client):
    """The two switches answer different questions: ours is whether this app may run it."""
    c, st = client
    st.set_automation("auto_codescan", devin_automation_id="auto-abc123")
    transport = c.app.state.client.t
    before = len([x for x in transport.calls if x[0] == "update_automation"])
    c.post("/automations/auto_codescan/toggle")
    after = len([x for x in transport.calls if x[0] == "update_automation"])
    assert after == before


def test_a_replayed_store_has_numbered_tickets_before_a_page_is_drawn(tmp_path, monkeypatch):
    """The recording carries the columns that existed when it was made.

    Anything added since arrives empty, and a ticket with no number renders as "#-----" on every
    badge in the app. Numbering on the next Store construction is too late: the container seeds
    at startup and serves from that same store, so the first thing a reader saw was blanks.
    """
    monkeypatch.setenv("SWE_LOOP_MODE", "replay")
    st = Store(tmp_path / "r.sqlite")
    app = build_app(Settings.from_env(), st)
    with TestClient(app) as c:
        r = c.get("/")

    numbers = [t.get("number") for t in st.list_tickets()]
    assert numbers and all(n for n in numbers), f"a ticket arrived with no number: {numbers}"
    assert "#-----" not in r.text


def test_no_page_or_drawer_shows_a_machine_path(tmp_path, monkeypatch):
    """The checks run on somebody's machine and their output carries that machine's home
    directory. Every page and every session drawer shortens such paths to their tail; a home
    directory on a shared screen is a leak, whatever else it says."""
    import json

    from fastapi.testclient import TestClient

    from swe_loop.app import build_app
    from swe_loop.config import Settings
    from swe_loop.store import Store

    monkeypatch.delenv("DEVIN_API_KEY", raising=False)
    st = Store(tmp_path / "p.sqlite")
    app = build_app(Settings.from_env(), st)
    with TestClient(app) as c:
        # plant a machine path where the audits found one: a verdict's reason and an escalation
        sid = st._all("SELECT id FROM sessions LIMIT 1")[0]["id"]
        st.conn.execute(
            "UPDATE verdicts SET reason=? WHERE session_id=?",
            (
                'File "/Users/someone/Desktop/private/superset-fork/.venv-p3/lib/x.py", line 1800',
                sid,
            ),
        )
        tid = st.list_tickets()[0]["id"]
        st.insert_escalation(
            tid,
            sid,
            "review_blocked",
            "Traceback in /Users/someone/Desktop/private/superset-fork/a.py",
        )
        st.conn.commit()
        pages = [
            "/",
            "/tickets-page",
            f"/tickets-page?open={tid}",
            "/report",
            "/report?checks=1&log=1",
            "/automations",
            "/devin/sessions",
            "/devin/insights",
            "/settings",
        ]
        pages += [f"/devin/sessions?drawer={r['id']}" for r in st._all("SELECT id FROM sessions")]
        for path in pages:
            html = c.get(path).text
            assert "/Users/" not in html, path
            assert "someone/Desktop" not in html, path
