"""Resetting a shard for a live rerun: the repository's files return to the baseline and the
change is pushed, the old repair branch goes, the store forgets the ticket after a snapshot.
Running it twice is safe."""

import dataclasses
import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from swe_loop.app import build_app
from swe_loop.config import Settings, TargetConfig
from swe_loop.replay import synthesise
from swe_loop.rerun import reset_shard, shard_files, shards
from swe_loop.store import Store

ROOT = Path(__file__).resolve().parents[1]
CFG = TargetConfig.load(ROOT / "configs" / "superset-pandas3.yaml")
INVENTORY = ROOT / "data" / "inventory" / "2026-09-03"


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


@pytest.fixture
def fork(tmp_path):
    """A bare origin and a clone: baseline commit with a broken file, then a merged fix and the
    repair branch that carried it."""
    origin = tmp_path / "origin.git"
    git("init", "--bare", "--quiet", "-b", "master", str(origin), cwd=tmp_path)
    clone = tmp_path / "clone"
    git("clone", "--quiet", str(origin), str(clone), cwd=tmp_path)
    git("config", "user.email", "t@example.com", cwd=clone)
    git("config", "user.name", "t", cwd=clone)
    (clone / "superset" / "models").mkdir(parents=True)
    f = clone / "superset" / "models" / "helpers.py"
    f.write_text("def parse(x):\n    return to_datetime(x, format='mixed')\n")
    git("add", ".", cwd=clone)
    git("commit", "--quiet", "-m", "chore: baseline", cwd=clone)
    baseline = git("rev-parse", "HEAD", cwd=clone).stdout.strip()
    f.write_text("def parse(x):\n    return to_datetime(x, format='mixed', utc=True)\n")
    git("commit", "--quiet", "-am", "fix(pandas): utc=True (#7)", cwd=clone)
    git("push", "--quiet", "origin", "master", cwd=clone)
    git("push", "--quiet", "origin", "master:refs/heads/swe-loop/D", cwd=clone)
    cfg = dataclasses.replace(CFG, rerun={"baseline": baseline, "branch_prefix": "swe-loop/"})
    return origin, clone, cfg, baseline


def test_shards_and_files_come_from_the_drafts():
    assert shard_files("D") == ["superset/models/helpers.py"]
    ids = [s["id"] for s in shards()]
    assert "D" in ids and "E" not in ids  # E is a person's; nothing to reset


def test_reset_restores_the_file_deletes_the_branch_and_forgets_the_ticket(fork, tmp_path):
    origin, clone, cfg, _baseline = fork
    st = Store(tmp_path / "s.sqlite")
    synthesise(st, CFG, INVENTORY / "tickets.json", tmp_path)
    assert st.get_ticket("tkt_D") and st.work_orders_for("tkt_D")
    settings = Settings(mode="live", devin_api_key="x", github_token="")
    logs = []
    out = reset_shard(
        settings, cfg, st, "D", repo_root=clone, snapshot_dir=tmp_path / "snap", log=logs.append
    )
    assert out["repo"] == "restored" and out["pushed"] and out["branch_deleted"]
    # origin master carries the restore; the file is the baseline's again
    shown = subprocess.run(
        ["git", "show", "origin/master:superset/models/helpers.py"],
        cwd=str(clone),
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    assert "utc=True" not in shown
    heads = subprocess.run(
        ["git", "ls-remote", "--heads", str(origin)], capture_output=True, text=True, check=False
    ).stdout
    assert "swe-loop/D" not in heads
    # the store forgot D and only D, after a snapshot
    assert st.get_ticket("tkt_D") is None and st.work_orders_for("tkt_D") == []
    assert st.get_ticket("tkt_A") and st.get_ticket("tkt_E")
    assert out["store_rows"] > 5
    snap = json.loads(Path(tmp_path / "snap").glob("*-D.json").__next__().read_text())
    assert snap["tickets"][0]["id"] == "tkt_D" and snap["work_orders"]
    assert "shard D reset for a rerun" in " ".join(e["event"] for e in st.timeline(limit=5))
    # second time: nothing to restore, nothing to forget, no error
    out2 = reset_shard(
        settings, cfg, st, "D", repo_root=clone, snapshot_dir=tmp_path / "snap", log=logs.append
    )
    assert out2["repo"] == "already at baseline" and not out2["pushed"] and out2["store_rows"] == 0


def test_reset_refuses_a_dirty_clone(fork, tmp_path):
    _origin, clone, cfg, _baseline = fork
    (clone / "scratch.txt").write_text("x")
    st = Store(tmp_path / "s.sqlite")
    with pytest.raises(RuntimeError, match="local changes"):
        reset_shard(Settings(mode="live", devin_api_key="x"), cfg, st, "D", repo_root=clone)


def test_replay_reset_never_touches_the_repository(tmp_path, monkeypatch):
    """The page says the repository is not touched in replay. Prove it: a runner that would
    raise if any git command ran at all."""
    st = Store(tmp_path / "s.sqlite")
    synthesise(st, CFG, INVENTORY / "tickets.json", tmp_path)

    def no_git(*a, **k):
        raise AssertionError("git ran in replay")

    out = reset_shard(
        Settings(mode="replay"), CFG, st, "D", runner=no_git, snapshot_dir=tmp_path / "snap"
    )
    assert out["repo"] == "replay: the repository is not touched" and not out["pushed"]
    assert st.get_ticket("tkt_D") is None and out["store_rows"] > 0


def test_settings_card_and_route_in_replay(tmp_path, monkeypatch):
    monkeypatch.delenv("DEVIN_API_KEY", raising=False)
    st = Store(tmp_path / "t.sqlite")
    synthesise(st, CFG, INVENTORY / "tickets.json", tmp_path)
    app = build_app(Settings.from_env(), st, seed_replay=False)
    with TestClient(app) as c:
        html = c.get("/settings").text
        assert (
            "Rerun a shard" in html and "never reset" in html and "only the store is reset" in html
        )
        r = c.post(
            "/settings/reset-shard",
            content="shard=D",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert r.status_code == 200 and r.url.path == "/settings"
        assert st.get_ticket("tkt_D") is None and st.get_ticket("tkt_A")
        assert "shard D at" in c.get("/settings").text
        assert (
            c.post(
                "/settings/reset-shard",
                content="shard=E",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            ).status_code
            == 400
        )
        # Run picks D up again: it is the one new ticket
        c.post("/automations/auto_repair/run")
        c.app.state.run_thread.join(120)
        res = st.get_automation("auto_repair")["last_result"]
        assert res["new_tickets"] == ["tkt_D"] and res["triaged"] == 1
        assert st.get_ticket("tkt_D")["status"] != "new"


def test_reset_reopens_the_issue_the_merge_closed(fork, tmp_path):
    """Merging closes the issue. A reset undoes the merge, so it must reopen it, or the next run
    finds nothing to do."""
    _origin, clone, cfg, _baseline = fork
    st = Store(tmp_path / "s.sqlite")
    calls = []

    def patch(url, body):
        calls.append((url, body))
        return {"state": "open"}

    from swe_loop import rerun as rr

    assert rr.issue_number("D", CFG.repo) == 4
    assert rr.reopen_issue(CFG.repo, 4, "tok", patch=patch) == "reopened"
    assert calls[0][0].endswith("/issues/4") and calls[0][1] == {"state": "open"}
    out = rr.reset_shard(
        Settings(mode="live", devin_api_key="x"),
        cfg,
        st,
        "D",
        repo_root=clone,
        snapshot_dir=tmp_path / "snap",
    )
    # the real call is attempted with no token, and whatever it answers is reported, never hidden
    assert out["issue"] != "not touched"


def test_offer_it_again_reopens_the_same_verified_change(fork, tmp_path):
    """The merge step has to be showable without paying for a whole run. Offering again puts the
    base branch back and opens the same commit as a new pull request, spending nothing."""
    from swe_loop import rerun as rr

    _origin, clone, cfg, _baseline = fork
    st = Store(tmp_path / "s.sqlite")
    synthesise(st, CFG, INVENTORY / "tickets.json", tmp_path)
    st.record_human_action("tkt_D", "merge", "someone")
    st.set_ticket_status("tkt_D", "merged")
    seen = {}

    def fake_pr(repo, head, base, token, body, shard):
        seen.update(repo=repo, head=head, base=base, body=body)
        return "https://github.com/o/r/pull/99"

    out = rr.reoffer_shard(
        Settings(mode="live", devin_api_key="x"),
        cfg,
        st,
        "D",
        repo_root=clone,
        open_pr=fake_pr,
    )
    assert out["pr"] == "https://github.com/o/r/pull/99" and out["base_restored"]
    assert seen["head"] == "swe-loop/D" and seen["base"] == "master"
    assert "No session was spent" in seen["body"]
    # the branch is the base plus the change, so GitHub has something to open
    ahead = subprocess.run(
        ["git", "rev-list", "--count", "origin/master..origin/swe-loop/D"],
        cwd=str(clone),
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    assert ahead == "1", f"the branch must be ahead of the base, was {ahead}"
    # the branch carries the fix, the base no longer does
    branch = subprocess.run(
        ["git", "show", "origin/swe-loop/D:superset/models/helpers.py"],
        cwd=str(clone),
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    base = subprocess.run(
        ["git", "show", "origin/master:superset/models/helpers.py"],
        cwd=str(clone),
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    assert "utc=True" in branch and "utc=True" not in base
    # the ticket is offered to a person again, and no session was created
    assert st.get_ticket("tkt_D")["status"] == "reviewed"
    assert st._all("SELECT * FROM human_actions WHERE ticket_id='tkt_D'") == []
    assert (
        st._one(
            "SELECT pull_request_url AS u FROM sessions WHERE work_order_id IN "
            "(SELECT id FROM work_orders WHERE ticket_id='tkt_D')"
        )["u"]
        == "https://github.com/o/r/pull/99"
    )
    assert "offered again" in " ".join(e["event"] for e in st.timeline(ticket_id="tkt_D"))


def test_offering_a_shard_that_is_not_merged_says_why(fork, tmp_path):
    from swe_loop import rerun as rr

    _origin, clone, cfg, _baseline = fork
    st = Store(tmp_path / "s.sqlite")
    synthesise(st, CFG, INVENTORY / "tickets.json", tmp_path)  # D is reviewed, not merged
    with pytest.raises(ValueError, match="nothing to offer again"):
        rr.reoffer_shard(Settings(mode="live", devin_api_key="x"), cfg, st, "D", repo_root=clone)
