import json
import subprocess
from pathlib import Path

from swe_loop.config import TargetConfig
from swe_loop.devin import DevinClient, FakeTransport
from swe_loop.gate import Gate, PullRequest, absolutize_command, apply_result
from swe_loop.poll import Poller
from swe_loop.store import Store

ROOT = Path(__file__).resolve().parents[1]
CFG = TargetConfig.load(ROOT / "configs" / "superset-pandas3.yaml")
PR = "https://github.com/charliebachg/superset/pull/7"


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def make_repo(tmp_path):
    """A tiny target: helpers.py (the site), tests/test_x.py (the oracle), .github/ci.yml."""
    repo = tmp_path / "target"
    repo.mkdir()
    git("init", "-q", "-b", "master", cwd=repo)
    git("config", "user.email", "t@example.com", cwd=repo)
    git("config", "user.name", "t", cwd=repo)
    (repo / "superset").mkdir()
    (repo / "superset" / "helpers.py").write_text("def parse(s):\n    return to_datetime(s)\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_x.py").write_text("def test():\n    assert True\n")
    (repo / ".github").mkdir()
    (repo / ".github" / "ci.yml").write_text("name: ci\n")
    (repo / "other.py").write_text("x = 1\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "base", cwd=repo)

    def branch(name, edits):
        git("checkout", "-q", "-b", name, "master", cwd=repo)
        for f, content in edits.items():
            (repo / f).write_text(content)
        git("add", "-A", cwd=repo)
        git("commit", "-q", "-m", name, cwd=repo)
        git("checkout", "-q", "master", cwd=repo)

    good = "def parse(s):\n    return to_datetime(s, utc=True)\n"
    branch("fix-good", {"superset/helpers.py": good})
    branch(
        "fix-oracle", {"superset/helpers.py": good, "tests/test_x.py": "def test():\n    pass\n"}
    )
    branch("fix-outscope", {"superset/helpers.py": good, "other.py": "x = 2\n"})
    branch(
        "fix-wrong",
        {"superset/helpers.py": "def parse(s):\n    return to_datetime(s)  # touched\n"},
    )
    return repo


def seed(tmp_path, branch="fix-good", claim_files=None):
    st = Store(tmp_path / "t.sqlite")
    st.upsert_ticket(id="tkt_D", source="manual", title="D", status="routed")
    wid = st.insert_work_order(
        ticket_id="tkt_D",
        shard_id="D",
        files=["superset/helpers.py"],
        tests=["tests/test_x.py"],
        acceptance={
            "site_fixed": "sh -c 'grep -q utc=True superset/helpers.py'",
            "oracle": "sh -c 'test -f tests/test_x.py'",
        },
    )
    sid = st.reserve_session(work_order_id=wid, playbook_id=None, tags=["swe-loop", f"wo:{wid}"])
    st.bind_devin_session(sid, devin_session_id="devin-1", url="u")
    claim = {
        "shard": "D",
        "self_reported_done": True,
        "files_changed": claim_files or ["superset/helpers.py"],
        "call_sites_fixed": [],
        "tests_run": 1,
        "tests_passed": 1,
        "pr_url": PR,
        "branch": branch,
        "needs_human": [],
    }
    st.update_session(
        sid, structured_output_json=json.dumps(claim), self_reported_done=1, pull_request_url=PR
    )
    st.set_ticket_status("tkt_D", "gated")
    return st, sid


def make_gate(st, tmp_path, repo, branch):
    return Gate(
        st,
        CFG,
        repo_root=repo,
        evidence_dir=tmp_path / "evidence",
        resolver=lambda url: PullRequest(url, branch, "master"),
    )


def test_pass_binds_evidence_to_the_tree(tmp_path):
    repo = make_repo(tmp_path)
    st, sid = seed(tmp_path)
    res = make_gate(st, tmp_path, repo, "fix-good").run_gate(sid)
    assert res.gate_result == "pass" and res.tiers == {"T0": True, "T1": True}
    ev = st.evidence_for(sid, res.tree_hash)
    assert len(ev) == 3 and all(e["passed"] for e in ev)  # T0 + two acceptance commands
    assert st.evidence_for(sid, "some-other-tree") == []
    assert st.latest_verdict(sid)["gate_result"] == "pass"
    log = Path(ev[1]["output_path"]).read_text()
    assert "grep -q utc=True" in log and f"tree={res.tree_hash}" in log
    assert not list(Path(tmp_path).glob("swe-loop-gate-*"))  # worktree released


def test_a_change_to_the_tests_is_recorded_for_a_person_not_refused(tmp_path):
    """A test is the thing that decides whether a change works, so a change to one never merges
    on the machine's say-so. It is not refused either: a session that adds a test proving its own
    fix has raised the bar rather than dodged it. The rule is that a person sees it."""
    repo = make_repo(tmp_path)
    st, sid = seed(tmp_path, branch="fix-oracle")
    res = make_gate(st, tmp_path, repo, "fix-oracle").run_gate(sid)
    assert res.tests_touched == ["tests/test_x.py"]
    assert res.tiers.get("T0") is True  # the scope check no longer stops on it
    assert not any("oracle touched" in r for r in res.reasons)


def test_out_of_scope_change_fails_t0(tmp_path):
    repo = make_repo(tmp_path)
    st, sid = seed(tmp_path, branch="fix-outscope")
    res = make_gate(st, tmp_path, repo, "fix-outscope").run_gate(sid)
    assert res.gate_result == "fail" and "other.py" in res.failure_text


def test_acceptance_failure_carries_exact_output(tmp_path):
    repo = make_repo(tmp_path)
    st, sid = seed(tmp_path, branch="fix-wrong")
    res = make_gate(st, tmp_path, repo, "fix-wrong").run_gate(sid)
    assert res.gate_result == "fail" and res.tiers == {"T0": True, "T1": False}
    assert "site_fixed" in res.failure_text and "exit 1" in res.failure_text
    ev = st.evidence_for(sid, res.tree_hash)
    assert [e["passed"] for e in ev] == [1, 0, 1]
    assert st.latest_verdict(sid)["decision"] == "retry"


def test_missing_pr_is_missing_evidence(tmp_path):
    repo = make_repo(tmp_path)
    st, sid = seed(tmp_path)
    st.update_session(sid, pull_request_url=None, structured_output_json=json.dumps({"shard": "D"}))
    res = make_gate(st, tmp_path, repo, "fix-good").run_gate(sid)
    assert res.gate_result == "missing_evidence"
    assert st.latest_verdict(sid)["decision"] == "escalate"


def test_claimed_file_that_does_not_exist_fails_t0(tmp_path):
    repo = make_repo(tmp_path)
    st, sid = seed(tmp_path, claim_files=["superset/helpers.py", "superset/ghost.py"])
    res = make_gate(st, tmp_path, repo, "fix-good").run_gate(sid)
    assert res.gate_result == "fail" and any("do not exist" in r for r in res.reasons)


def test_absolutize_points_venvs_at_the_clone():
    cmd = absolutize_command(".venv-p3/bin/python -m pytest -q tests/x.py", Path("/repo"))
    assert cmd.startswith("/repo/.venv-p3/bin/python -m pytest")
    assert absolutize_command("sh -c 'grep -q x y'", Path("/repo")) == "sh -c 'grep -q x y'"


def test_apply_pass_requests_review_and_fail_retries_then_escalates(tmp_path):
    repo = make_repo(tmp_path)
    (tmp_path / "sessions").mkdir()
    (tmp_path / "sessions" / "devin-1.json").write_text(
        json.dumps(
            {
                "session_id": "devin-1",
                "url": "u",
                "timeline": [{"status": "running", "status_detail": "working"}],
            }
        )
    )
    client = DevinClient(FakeTransport(tmp_path))
    client.t.create_session({"tags": [], "repos": []})  # devin-1 now exists on the fake
    # pass path
    st, sid = seed(tmp_path)
    poller = Poller(st, client, CFG, sleep=lambda s: None, clock=lambda: 0.0)
    res = make_gate(st, tmp_path, repo, "fix-good").run_gate(sid)
    assert apply_result(res, st, client, poller) == "reviewed"
    assert next(c for c in client.t.calls if c[0] == "create_pr_review")[1] == PR
    assert st.get_ticket("tkt_D")["status"] == "reviewed"
    assert st.latest_verdict(sid)["review_severity"].startswith("requested:")
    # fail path: retried twice with the exact text, then escalated
    (tmp_path / "b").mkdir()
    st2, sid2 = seed(tmp_path / "b", branch="fix-wrong")
    poller2 = Poller(st2, client, CFG, sleep=lambda s: None, clock=lambda: 0.0)
    gate = make_gate(st2, tmp_path / "b", repo, "fix-wrong")
    assert apply_result(gate.run_gate(sid2), st2, client, poller2) == "retried"
    assert "grep -q utc=True" in [c for c in client.t.calls if c[0] == "send_message"][-1][1][1]
    assert apply_result(gate.run_gate(sid2), st2, client, poller2) == "retried"
    assert apply_result(gate.run_gate(sid2), st2, client, poller2) == "escalated"
    esc = st2.list_escalations()[-1]
    assert (
        esc["kind"] == "detector_still_fires" and st2.get_ticket("tkt_D")["status"] == "escalated"
    )


def test_a_command_that_cds_into_devins_own_checkout_still_runs_here():
    """A session sometimes writes `cd /home/ubuntu/repos/... && pytest ...`, naming the checkout
    it worked in. That path is not on this machine, and the check already runs in the worktree
    made for it. Left in, the cd fails, && short-circuits, and a good change is recorded as
    failing for a reason that has nothing to do with the change."""
    root = Path("/repo")
    got = absolutize_command(
        "cd /home/ubuntu/repos/superset && .venv-p3/bin/python -m pytest a.py", root
    )
    assert got == "/repo/.venv-p3/bin/python -m pytest a.py"
    # a cd in the middle is the ticket's own business and is left alone
    kept = absolutize_command("echo one && cd /elsewhere && echo two", root)
    assert kept == "echo one && cd /elsewhere && echo two"
    # a command that is only a cd is not emptied into nothing
    assert absolutize_command("cd /somewhere", root) == "cd /somewhere"


def test_where_devin_chose_the_file_itself_a_difference_is_confirmed_not_failed(tmp_path):
    """For work the loop scoped, a change outside the work order is a failure: the scope was
    agreed before the work started. For a fix Devin found and made itself, the file it chose can
    reasonably differ from the one the finding named, and often for the better. That is a
    difference a person should see, not a failure."""
    repo = make_repo(tmp_path)
    st, sid = seed(tmp_path, branch="fix-outscope")
    wo_id = st.get_session(sid)["work_order_id"]
    st.conn.execute("UPDATE work_orders SET shard_id='remediation' WHERE id=?", (wo_id,))
    st.conn.commit()
    res = make_gate(st, tmp_path, repo, "fix-outscope").run_gate(sid)
    assert res.scope_changed == ["other.py"]
    assert res.tiers.get("T0") is True
    assert not any("outside the work order" in r for r in res.reasons)


def test_the_base_branch_is_read_fresh_not_from_whatever_this_machine_last_saw(tmp_path):
    """The base moves whenever anything else is merged. A branch cut after that move looks, to a
    clone that has not fetched, like it introduced every commit the clone has not heard about,
    and good work is failed for changing files it never touched."""
    repo = make_repo(tmp_path)
    st, sid = seed(tmp_path, branch="fix-ok")
    calls: list[tuple[str, ...]] = []
    gate = make_gate(st, tmp_path, repo, "fix-ok")
    real = gate.ws.git

    def spy(*args, **kw):
        calls.append(args)
        return real(*args, **kw)

    gate.ws.git = spy
    gate.run_gate(sid)
    fetched = [i for i, c in enumerate(calls) if c[0] == "fetch"]
    assert fetched, "the clone was compared against a base it never refreshed"
    # and it happens before the base is resolved, which is what the comparison rests on
    resolved = [i for i, c in enumerate(calls) if c[0] == "rev-parse"]
    assert resolved and fetched[0] < resolved[0]
