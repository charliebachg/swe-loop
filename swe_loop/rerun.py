"""Put one shard back to its broken state so the loop can run on it again, live, as often as
wanted.

Two things make a shard "done": the fix on the repository's base branch and the rows in the
store. A reset undoes both. On the repository, the shard's files go back to the baseline commit
named in the seam (the base branch before any fix landed), the change is committed and pushed,
and the old repair branch is deleted so the next session starts clean. In the store, every row
about the ticket is snapshotted to a file and forgotten. The issue stays open on the repository,
so the next Run picks it up as new."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from swe_loop.config import Settings, TargetConfig
from swe_loop.store import Store, now

ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "data" / "inventory" / "2026-09-03"
Runner = Callable[..., subprocess.CompletedProcess]


def shard_files(shard: str, tickets_json: Path = INVENTORY / "tickets.json") -> list[str]:
    d = json.loads(Path(tickets_json).read_text())
    for sh in d.get("shards", []):
        if sh["id"] == shard:
            return list(sh.get("files") or [])
    return []


def issue_number(
    shard: str, repo: str, tickets_json: Path = INVENTORY / "tickets.json"
) -> int | None:
    """The issue this shard was filed as, when the target is the one the inventory describes."""
    if not Path(tickets_json).exists():
        return None
    d = json.loads(Path(tickets_json).read_text())
    if d.get("repo") and d["repo"] != repo:
        return None
    n = (d.get("numbers") or {}).get(shard)
    return int(n) if n is not None else None


def reopen_issue(repo: str, number: int, token: str, patch: Any = None) -> str:
    """A merge closes the issue. A reset undoes the merge, so the issue comes back, otherwise the
    next run has nothing to find."""
    import httpx

    headers = {"Accept": "application/vnd.github+json", "User-Agent": "swe-loop"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"https://api.github.com/repos/{repo}/issues/{number}"
    call = patch or (lambda u, b: httpx.patch(u, headers=headers, json=b, timeout=20).json())
    try:
        d = call(url, {"state": "open"})
    except Exception as ex:  # noqa: BLE001 - reported on the page, never silent
        return f"could not reopen issue #{number}: {type(ex).__name__}"
    return "reopened" if (d or {}).get("state") == "open" else "already open"


def shards(tickets_json: Path = INVENTORY / "tickets.json") -> list[dict[str, Any]]:
    """The shards a reset can target: the ones routed to Devin, with their files."""
    d = json.loads(Path(tickets_json).read_text())
    return [
        {"id": sh["id"], "files": list(sh.get("files") or []), "title": sh.get("title", "")}
        for sh in d.get("shards", [])
        if sh.get("route") == "devin" and sh.get("files")
    ]


class Repo:
    """The local clone the gate also uses. Every command is one subprocess; the runner is a
    parameter so the tests can watch it."""

    def __init__(self, root: Path, token: str = "", runner: Runner = subprocess.run):
        self.root = Path(root)
        self.token = token
        self.runner = runner

    def git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        cmd = ["git"]
        if self.token and args and args[0] in ("push", "fetch"):
            helper = "!f() { echo username=x-access-token; echo password=" + self.token + "; }; f"
            cmd += ["-c", "credential.helper=", "-c", f"credential.helper={helper}"]
        r = self.runner(
            [*cmd, *args], cwd=str(self.root), capture_output=True, text=True, timeout=120
        )
        if check and r.returncode != 0:
            raise RuntimeError(
                f"git {' '.join(args[:2])} failed: {(r.stderr or r.stdout).strip()[:300]}"
            )
        return r


def reoffer_shard(
    settings: Settings,
    cfg: TargetConfig,
    store: Store,
    shard: str,
    *,
    repo_root: Path | None = None,
    files: list[str] | None = None,
    runner: Runner = subprocess.run,
    open_pr: Callable[..., Any] | None = None,
    log: Callable[[str], None] = lambda m: None,
) -> dict[str, Any]:
    """Put a merged change back in front of a person, unchanged, as a new pull request.

    This spends no session and asks the AI for nothing. It takes the work already verified, puts
    the base branch back to where it was before that work landed, and offers the same commit
    again. It exists so the merge step can be shown or rehearsed without waiting for a whole run.
    """
    tid = f"tkt_{shard}"
    files = files if files is not None else shard_files(shard)
    baseline = str(cfg.rerun.get("baseline") or "")
    prefix = str(cfg.rerun.get("branch_prefix") or "swe-loop/")
    base = cfg.base_branch
    out: dict[str, Any] = {"shard": shard, "ticket": tid, "at": now()}
    if not files or not baseline:
        raise ValueError(f"shard {shard} has no files or no baseline in the seam")
    root = (
        repo_root
        if repo_root is not None
        else (ROOT / cfg.gate.get("repo_root", "../superset-fork")).resolve()
    )
    if not (root / ".git").exists():
        raise RuntimeError(f"no clone at {root}")
    repo = Repo(root, settings.github_token, runner)
    if repo.git("status", "--porcelain").stdout.strip():
        raise RuntimeError(f"the clone at {root} has local changes; refusing")

    repo.git("fetch", "--quiet", "origin", base)
    repo.git("checkout", "--quiet", base)
    repo.git("reset", "--quiet", "--hard", f"origin/{base}")
    fix = repo.git("rev-parse", "HEAD").stdout.strip()
    out["fix_commit"] = fix[:10]
    # the branch carries the work exactly as it was verified
    repo.git("push", "--quiet", "--force", "origin", f"{fix}:refs/heads/{prefix}{shard}")
    # the base branch goes back to before it landed
    repo.git("checkout", "--quiet", baseline, "--", *files)
    if repo.git("diff", "--cached", "--quiet", check=False).returncode != 0:
        repo.git(
            "-c",
            "user.name=swe-loop",
            "-c",
            "user.email=swe-loop@users.noreply.github.com",
            "commit",
            "--quiet",
            "-m",
            f"chore: offer shard {shard} again, unchanged, for a walk-through",
        )
        repo.git("push", "--quiet", "origin", f"{base}:{base}")
        out["base_restored"] = True
    else:
        out["base_restored"] = False

    body = (
        "The same change that was verified earlier, offered again so the merge step can be shown."
        f" No session was spent: this is commit {fix[:10]} exactly as it was checked."
    )
    make = open_pr or _create_pr
    pr = make(cfg.repo, f"{prefix}{shard}", base, settings.github_token, body, shard)
    out["pr"] = pr
    if pr.startswith("http"):
        store.conn.execute("DELETE FROM human_actions WHERE ticket_id=? AND kind='merge'", (tid,))
        store.conn.execute(
            "UPDATE sessions SET pull_request_url=?, pr_state=NULL WHERE work_order_id IN "
            "(SELECT id FROM work_orders WHERE ticket_id=?)",
            (pr, tid),
        )
        store.conn.execute(
            "UPDATE work_orders SET status='devin' WHERE ticket_id=? AND status='merged'", (tid,)
        )
        store.set_ticket_status(tid, "reviewed")
        store.conn.commit()
        store.log(
            "merge",
            f"shard {shard} offered again, unchanged",
            ticket_id=tid,
            detail=f"{pr} · commit {fix[:10]} · no session spent",
        )
    log(f"offered again: {pr}")
    store.set_setting("rerun.last", json.dumps(out))
    return out


def _create_pr(repo: str, head: str, base: str, token: str, body: str, shard: str) -> str:
    import httpx

    headers = {"Accept": "application/vnd.github+json", "User-Agent": "swe-loop"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        r = httpx.post(
            f"https://api.github.com/repos/{repo}/pulls",
            headers=headers,
            json={
                "title": f"fix(pandas): shard {shard}, offered again for a walk-through",
                "head": head,
                "base": base,
                "body": body,
            },
            timeout=30,
        )
        d = r.json()
    except Exception as ex:  # noqa: BLE001 - reported on the page
        return f"could not open a pull request: {type(ex).__name__}"
    return d.get("html_url") or str(d.get("message", "GitHub refused"))[:160]


def reset_shard(
    settings: Settings,
    cfg: TargetConfig,
    store: Store,
    shard: str,
    *,
    repo_root: Path | None = None,
    files: list[str] | None = None,
    touch_repo: bool | None = None,
    push: bool = True,
    runner: Runner = subprocess.run,
    snapshot_dir: Path | None = None,
    log: Callable[[str], None] = lambda m: None,
) -> dict[str, Any]:
    tid = f"tkt_{shard}"
    files = files if files is not None else shard_files(shard)
    baseline = str(cfg.rerun.get("baseline") or "")
    prefix = str(cfg.rerun.get("branch_prefix") or "swe-loop/")
    base = cfg.base_branch
    out: dict[str, Any] = {
        "shard": shard,
        "ticket": tid,
        "files": files,
        "baseline": baseline,
        "repo": "skipped",
        "pushed": False,
        "branch_deleted": False,
        "issue": "not touched",
        "store_rows": 0,
        "snapshot": None,
        "at": now(),
    }
    if not files:
        raise ValueError(f"shard {shard} has no files to restore")

    # 1. the store: snapshot, then forget
    dump = store.ticket_dump(tid)
    n = sum(len(v) for v in dump.values())
    if n:
        sdir = snapshot_dir or (ROOT / "data" / "live" / "resets")
        sdir.mkdir(parents=True, exist_ok=True)
        path = sdir / f"{now().replace(':', '').replace('+00:00', 'Z')}-{shard}.json"
        path.write_text(json.dumps(dump, indent=1, default=str))
        out["snapshot"] = str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)
        out["store_rows"] = store.forget_ticket(tid)
        log(f"store: {out['store_rows']} row(s) about {tid} snapshotted and forgotten")
    else:
        log(f"store: nothing about {tid}")

    # 2. the repository
    root = (
        repo_root
        if repo_root is not None
        else (ROOT / cfg.gate.get("repo_root", "../superset-fork")).resolve()
    )
    if touch_repo is None:
        touch_repo = settings.live
    if not touch_repo:
        out["repo"] = "replay: the repository is not touched"
    elif not baseline:
        out["repo"] = "no baseline in the seam; repository untouched"
    elif not (root / ".git").exists():
        out["repo"] = f"no clone at {root}; repository untouched"
    else:
        repo = Repo(root, settings.github_token, runner)
        dirty = repo.git("status", "--porcelain").stdout.strip()
        if dirty:
            raise RuntimeError(f"the clone at {root} has local changes; refusing to reset")
        repo.git("fetch", "--quiet", "origin", base)
        repo.git("checkout", "--quiet", base)
        repo.git("reset", "--quiet", "--hard", f"origin/{base}")
        repo.git("checkout", "--quiet", baseline, "--", *files)
        staged = repo.git("diff", "--cached", "--quiet", check=False).returncode != 0
        if staged:
            repo.git(
                "-c",
                "user.name=swe-loop",
                "-c",
                "user.email=swe-loop@users.noreply.github.com",
                "commit",
                "--quiet",
                "-m",
                f"chore: put shard {shard} back to its state before the fix, for a live rerun",
            )
            out["repo"] = "restored"
            if push:
                repo.git("push", "--quiet", "origin", f"{base}:{base}")
                out["pushed"] = True
        else:
            out["repo"] = "already at baseline"
        if push:
            r = repo.git("push", "--quiet", "origin", "--delete", f"{prefix}{shard}", check=False)
            out["branch_deleted"] = r.returncode == 0
            n = issue_number(shard, cfg.repo)
            if n is not None:
                out["issue"] = reopen_issue(cfg.repo, n, settings.github_token)
        log(f"repository: {out['repo']}" + (", pushed" if out["pushed"] else ""))

    store.log(
        "intake",
        f"shard {shard} reset for a rerun",
        detail=f"{out['repo']} · {out['store_rows']} store row(s) forgotten · issue {out['issue']}",
    )
    store.set_setting("rerun.last", json.dumps(out))
    return out
