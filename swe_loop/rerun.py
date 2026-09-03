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


def reset_shard(
    settings: Settings,
    cfg: TargetConfig,
    store: Store,
    shard: str,
    *,
    repo_root: Path | None = None,
    files: list[str] | None = None,
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
    if not baseline:
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
        log(f"repository: {out['repo']}" + (", pushed" if out["pushed"] else ""))

    store.log(
        "intake",
        f"shard {shard} reset for a rerun",
        detail=f"{out['repo']} · {out['store_rows']} store row(s) forgotten · the issue stays open",
    )
    store.set_setting("rerun.last", json.dumps(out))
    return out
