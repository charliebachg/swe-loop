"""L5: the gate. Deliberately not a session. No step verifies the work it produced.

Given a session whose claim the poller recorded, the gate checks out the PR head into a clean
worktree and produces evidence bound to that tree's hash:

- T0, free: the oracle was not touched (no change under the seam's forbidden paths), the
  session stayed in scope (every changed file is in the work order), and the claimed artefacts
  exist (the PR, the files).
- T1, cheap: every acceptance command from the work order is run by this process, in the clean
  worktree, and must exit 0. The detector is one of those commands.

The verdict is `pass` only when every tier passed on that tree; `missing_evidence` when
something could not be run (no PR, no checkout); `fail` otherwise, with the exact output of
what failed, which is what goes back into the session. Evidence from another tree hash is
never consulted. A session's own report is recorded next to the gate's result; the gate's
result is what counts.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from swe_loop.config import TargetConfig
from swe_loop.devin import DevinClient
from swe_loop.store import Store

PR_RE = re.compile(r"https://github\.com/([^/]+)/([^/]+)/pull/(\d+)$")


@dataclass
class GateResult:
    session_id: str
    gate_result: str  # pass | fail | missing_evidence
    tree_hash: str | None = None
    tiers: dict[str, bool] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    failure_text: str = ""
    evidence_ids: list[str] = field(default_factory=list)
    pr_url: str | None = None


@dataclass(frozen=True)
class PullRequest:
    url: str
    head_ref: str
    base_ref: str
    head_sha: str | None = None


def resolve_pr_via_github(pr_url: str, token: str) -> PullRequest:
    """Ask GitHub for the PR's head and base. Needs a token that can read the fork."""
    import httpx

    m = PR_RE.match(pr_url)
    if not m:
        raise ValueError(f"not a GitHub PR url: {pr_url}")
    owner, repo, num = m.groups()
    r = httpx.get(
        f"https://api.github.com/repos/{owner}/{repo}/pulls/{num}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        timeout=30,
    )
    r.raise_for_status()
    d = r.json()
    return PullRequest(pr_url, d["head"]["ref"], d["base"]["ref"], d["head"]["sha"])


class Workspace:
    """A clean, detached worktree of one ref from a local clone. Nothing the session wrote in
    its own VM is trusted; we check out what it pushed."""

    def __init__(
        self, repo_root: Path, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run
    ):
        self.repo_root = Path(repo_root).resolve()
        self.run = runner

    def git(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
        return self.run(
            ["git", *args], cwd=str(cwd or self.repo_root), capture_output=True, text=True
        )

    def fetch(self, ref: str) -> None:
        self.git("fetch", "--quiet", "origin", ref)

    def _resolve(self, ref: str) -> str:
        for cand in (f"origin/{ref}", ref):
            r = self.git("rev-parse", "--verify", "--quiet", f"{cand}^{{commit}}")
            if r.returncode == 0:
                return r.stdout.strip()
        raise RuntimeError(f"ref not found in {self.repo_root}: {ref}")

    def checkout(self, ref: str) -> Path:
        sha = self._resolve(ref)
        path = Path(tempfile.mkdtemp(prefix="swe-loop-gate-"))
        r = self.git("worktree", "add", "--detach", "--quiet", str(path), sha)
        if r.returncode != 0:
            raise RuntimeError(f"worktree add failed: {r.stderr.strip()}")
        return path

    def release(self, path: Path) -> None:
        self.git("worktree", "remove", "--force", str(path))

    def tree_hash(self, path: Path) -> str:
        return self.git("rev-parse", "HEAD^{tree}", cwd=path).stdout.strip()

    def head_sha(self, path: Path) -> str:
        return self.git("rev-parse", "HEAD", cwd=path).stdout.strip()

    def changed_files(self, path: Path, base_ref: str) -> list[str]:
        base = self._resolve(base_ref)
        mb = self.git("merge-base", base, "HEAD", cwd=path).stdout.strip() or base
        out = self.git("diff", "--name-only", f"{mb}..HEAD", cwd=path).stdout
        return [ln.strip() for ln in out.splitlines() if ln.strip()]


def absolutize_command(cmd: str, repo_root: Path) -> str:
    """Acceptance commands in tickets name interpreters relative to the target clone
    (`.venv-p3/bin/python ...`). In a worktree those paths do not exist; point them at the
    clone's environments."""
    parts = shlex.split(cmd)
    parts = [str(repo_root / p) if p.startswith(".venv") else p for p in parts]
    return shlex.join(parts)


class Gate:
    def __init__(
        self,
        store: Store,
        cfg: TargetConfig,
        *,
        repo_root: Path | str,
        evidence_dir: Path | str,
        resolver: Callable[[str], PullRequest] | None = None,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        timeout: int = 1800,
    ):
        self.store, self.cfg = store, cfg
        self.ws = Workspace(Path(repo_root), runner)
        self.evidence_dir = Path(evidence_dir)
        self.resolver = resolver or self._default_resolver
        self.run = runner
        self.timeout = timeout

    def _default_resolver(self, pr_url: str) -> PullRequest:
        token = os.environ.get("GITHUB_TOKEN", "")
        if not token:
            raise RuntimeError("no GITHUB_TOKEN; cannot resolve the PR head")
        return resolve_pr_via_github(pr_url, token)

    # ------------------------------------------------------------------ evidence
    def _record(
        self,
        sid: str,
        tier: str,
        command: str,
        cwd: Path,
        tree_hash: str,
        code: int,
        output: str,
        passed: bool,
    ) -> str:
        d = self.evidence_dir / sid
        d.mkdir(parents=True, exist_ok=True)
        n = len(list(d.glob("*.log"))) + 1
        p = d / f"{n:02d}-{tier}.log"
        p.write_text(f"$ {command}\n# cwd={cwd} tree={tree_hash} exit={code}\n\n{output}")
        return self.store.insert_evidence(
            session_id=sid,
            tier=tier,
            command=command,
            cwd=str(cwd),
            tree_hash=tree_hash,
            exit_code=code,
            output=output,
            output_path=str(p),
            passed=passed,
        )

    # ------------------------------------------------------------------ the run
    def run_gate(self, sid: str) -> GateResult:
        row = self.store.get_session(sid)
        wo = self.store.get_work_order(row["work_order_id"])
        claim = json.loads(row["structured_output_json"]) if row["structured_output_json"] else {}
        res = GateResult(sid, "missing_evidence")

        pr_url = row["pull_request_url"] or claim.get("pr_url")
        res.pr_url = pr_url
        if not pr_url:
            res.reasons.append("no pull request was claimed; nothing to verify")
            return self._finish(res, wo)
        try:
            pr = self.resolver(pr_url)
        except Exception as ex:  # noqa: BLE001 - any failure to resolve is missing evidence
            res.reasons.append(f"could not resolve the PR head: {ex}")
            return self._finish(res, wo)

        try:
            self.ws.fetch(pr.head_ref)
            path = self.ws.checkout(pr.head_ref)
        except Exception as ex:  # noqa: BLE001
            res.reasons.append(f"could not check out {pr.head_ref}: {ex}")
            return self._finish(res, wo)

        try:
            tree = self.ws.tree_hash(path)
            res.tree_hash = tree
            self.store.log(
                "L5 gate",
                "clean checkout",
                session_id=sid,
                detail=f"{pr.head_ref} tree {tree[:12]}",
            )
            base_ref = pr.base_ref or self.cfg.base_branch

            # ---------------- T0: oracle untouched, in scope, artefacts exist
            changed = self.ws.changed_files(path, base_ref)
            forbidden = [
                f for f in changed if any(f.startswith(p) for p in self.cfg.forbidden_paths)
            ]
            out_of_scope = [f for f in changed if f not in set(wo["files"]) and f not in forbidden]
            missing = [f for f in claim.get("files_changed", []) if not (path / f).exists()]
            t0_ok = not forbidden and not out_of_scope and not missing
            t0_report = (
                f"changed: {changed}\nforbidden touched: {forbidden}\nout of scope: {out_of_scope}\n"
                f"claimed but missing: {missing}"
            )
            res.evidence_ids.append(
                self._record(
                    sid,
                    "T0",
                    f"git diff --name-only {base_ref}..HEAD",
                    path,
                    tree,
                    0 if t0_ok else 1,
                    t0_report,
                    t0_ok,
                )
            )
            res.tiers["T0"] = t0_ok
            if forbidden:
                res.reasons.append(f"oracle touched: {forbidden}")
            if out_of_scope:
                res.reasons.append(f"changed files outside the work order: {out_of_scope}")
            if missing:
                res.reasons.append(f"claimed files do not exist on the branch: {missing}")
            if not t0_ok:
                res.gate_result = "fail"
                res.failure_text = "T0 failed.\n" + t0_report
                return self._finish(res, wo)

            # ---------------- T1: every acceptance command, run here, must exit 0
            env = {
                **os.environ,
                "PYTHONPATH": str(path),
                **{k: str(v) for k, v in self.cfg.detector.get("env", {}).items()},
            }
            failures: list[str] = []
            t1_ok = True
            for name, cmd in wo["acceptance"].items():
                real = absolutize_command(cmd, self.ws.repo_root)
                try:
                    r = self.run(
                        real,
                        shell=True,
                        cwd=str(path),
                        env=env,
                        capture_output=True,
                        text=True,
                        timeout=self.timeout,
                    )
                    code, output = r.returncode, (r.stdout or "") + (r.stderr or "")
                except subprocess.TimeoutExpired as ex:
                    code, output = 124, f"timed out after {self.timeout}s\n{ex.stdout or ''}"
                ok = code == 0
                res.evidence_ids.append(
                    self._record(sid, "T1", f"{name}: {real}", path, tree, code, output, ok)
                )
                if not ok:
                    t1_ok = False
                    failures.append(f"### {name} (exit {code})\n$ {cmd}\n{output[-4000:]}")
            res.tiers["T1"] = t1_ok
            if not t1_ok:
                res.gate_result = "fail"
                res.reasons.append(f"{len(failures)} acceptance command(s) failed")
                res.failure_text = "T1 failed.\n" + "\n\n".join(failures)
                return self._finish(res, wo)

            res.gate_result = "pass"
            res.reasons.append("T0 clean; every acceptance command exited 0 on the PR head")
            return self._finish(res, wo)
        finally:
            self.ws.release(path)

    def _finish(self, res: GateResult, wo: dict[str, Any]) -> GateResult:
        row = self.store.get_session(res.session_id)
        decision = {"pass": "pass", "fail": "retry", "missing_evidence": "escalate"}[
            res.gate_result
        ]
        if res.gate_result == "fail" and row["retries"] >= 2:
            decision = "escalate"
        self.store.insert_verdict(
            session_id=res.session_id,
            gate_result=res.gate_result,
            decision=decision,
            reason="; ".join(res.reasons)[:1000],
            tree_hash=res.tree_hash,
        )
        return res


# ---------------------------------------------------------------------- what happens next
def apply_result(res: GateResult, store: Store, client: DevinClient, poller: Any) -> str:
    """Pass: request Devin Review, ticket -> reviewed. Fail: exact text back into the session, or
    escalate when retries are spent. Missing evidence: escalate. Returns what was done."""
    row = store.get_session(res.session_id)
    wo = store.get_work_order(row["work_order_id"])
    ticket_id = wo["ticket_id"]
    if res.gate_result == "pass":
        review = client.review_pr(res.pr_url) if res.pr_url else {}
        store.conn.execute(
            "UPDATE verdicts SET review_severity=? WHERE id=(SELECT id FROM verdicts WHERE session_id=? ORDER BY created_at DESC LIMIT 1)",
            (f"requested:{review.get('review_id', 'n/a')}", res.session_id),
        )
        store.set_ticket_status(ticket_id, "reviewed")
        return "reviewed"
    if res.gate_result == "fail" and poller.retry_with_failure(res.session_id, res.failure_text):
        return "retried"
    kind = (
        "oracle_touched"
        if any(r.startswith("oracle touched") for r in res.reasons)
        else "detector_still_fires"
    )
    if res.gate_result == "missing_evidence":
        kind = "review_blocked"
    store.insert_escalation(ticket_id, res.session_id, kind, "; ".join(res.reasons)[:500])
    store.set_ticket_status(ticket_id, "escalated")
    return "escalated"
