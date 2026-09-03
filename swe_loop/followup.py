"""Review to repair, closed by code: the remarks Devin Review left on a pull request go back into
the session that wrote it, the session revises on the same branch, the gate re-runs on the new
head, and a fresh review is requested only if the gate passes again. A person reads the result;
nobody retypes a reviewer's comment into a chat box."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import httpx

from swe_loop import reduce as reduce_mod
from swe_loop.config import TargetConfig
from swe_loop.devin import DevinClient
from swe_loop.gate import Gate, apply_result
from swe_loop.poll import Poller, output_digest
from swe_loop.store import Store

REVIEW_BOT = "devin-ai-integration[bot]"
ROOT = Path(__file__).resolve().parents[1]


def _plain(html: str) -> str:
    text = re.sub(r"<[^>]+>", "", html or "")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.replace("*Was this helpful? React with 👍 or 👎 to provide feedback.*", "").strip()


def fetch_review_remarks(pr_url: str, token: str, fetch: Any = None) -> list[dict[str, Any]]:
    """The reviewer's inline comments on the PR: path, line, and the remark in plain text."""
    if "/pull/" not in pr_url or (not token and fetch is None):
        return []
    owner_repo, _, num = pr_url.split("github.com/", 1)[-1].partition("/pull/")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    get = fetch or (lambda url: httpx.get(url, headers=headers, timeout=20).json())
    try:
        comments = get(f"https://api.github.com/repos/{owner_repo}/pulls/{num}/comments")
    except Exception:  # noqa: BLE001 - no remarks is a valid answer
        return []
    out = []
    for c in comments or []:
        if (c.get("user") or {}).get("login") != REVIEW_BOT:
            continue
        out.append(
            {
                "path": c.get("path", ""),
                "line": c.get("line") or c.get("original_line") or "",
                "body": _plain(c.get("body", ""))[:1500],
            }
        )
    return out


def compose_message(pr_url: str, branch: str | None, remarks: list[dict[str, Any]]) -> str:
    lines = [
        f"Devin Review left {len(remarks)} remark(s) on your pull request {pr_url}. Address each one on the same branch"
        + (f" ({branch})" if branch else "")
        + ", push, and run the acceptance commands again. Do not open a new pull request. "
        "If a remark is wrong or out of scope, say so in the PR description under 'not done, and why' instead of changing code. "
        "When done, provide structured output again with is_final=true: the same fields, with call_sites_fixed extended by what you changed now.",
        "",
    ]
    for i, r in enumerate(remarks, 1):
        loc = f"{r['path']}:{r['line']}" if r.get("path") else "general"
        lines.append(f"{i}. {loc}\n{r['body']}\n")
    return "\n".join(lines)


def review_followup(
    store: Store,
    client: DevinClient,
    cfg: TargetConfig,
    ticket_id: str,
    github_token: str = "",
    *,
    fetch: Any = None,
    sleep: Any = None,
    wall_clock: float = 3600.0,
    log: Any = print,
) -> dict[str, Any]:
    """Send the review's remarks back to the session that opened the PR, wait, re-gate."""
    target = None
    for wo in store.work_orders_for(ticket_id):
        p = reduce_mod._latest_pass(store, wo["id"])
        if p and p.get("pull_request_url"):
            target = (wo, p)
    if not target:
        return {
            "ticket_id": ticket_id,
            "kind": "no_pr",
            "detail": "no passing session with a pull request",
        }
    wo, sess = target
    remarks = fetch_review_remarks(sess["pull_request_url"], github_token, fetch)
    if not remarks:
        return {
            "ticket_id": ticket_id,
            "kind": "nothing_to_address",
            "pr": sess["pull_request_url"],
        }
    out = sess.get("structured_output_json")
    branch = None
    if out:
        import json

        try:
            branch = json.loads(out).get("branch")
        except ValueError:
            branch = None
    text = compose_message(sess["pull_request_url"], branch, remarks)
    client.message(sess["devin_session_id"], text)
    store.log(
        "L6 review",
        f"{len(remarks)} review remark(s) sent back to the session",
        ticket_id=ticket_id,
        session_id=sess["id"],
        detail=sess["pull_request_url"],
    )
    prior_status = (store.get_ticket(ticket_id) or {}).get("status")
    # the session is live again; the poller must wait for a claim that differs from the last one
    digest = output_digest(json.loads(out)) if out else None
    store.update_session(
        sess["id"],
        terminal_at=None,
        status="running",
        status_detail="working",
        rejected_output_digest=digest,
    )
    store.set_ticket_status(ticket_id, "running")
    fast = client.is_fake
    import time as _time

    poller = Poller(
        store,
        client,
        cfg,
        sleep=sleep or ((lambda s_: _time.sleep(min(s_, 0.05))) if fast else _time.sleep),
    )
    poller.wall_clock = wall_clock
    res = poller.wait(sess["id"])
    log(f"{ticket_id}: session {res.kind} {res.detail}")
    if res.kind != "finished":
        return {
            "ticket_id": ticket_id,
            "kind": res.kind,
            "detail": res.detail,
            "remarks": len(remarks),
        }
    repo_root = (ROOT / cfg.gate.get("repo_root", "../superset-fork")).resolve()
    if fast or not repo_root.exists():
        store.log(
            "L5 gate",
            "skipped",
            ticket_id=ticket_id,
            session_id=sess["id"],
            detail="replay: a fake session has no real PR to check out",
        )
        if prior_status:
            store.set_ticket_status(ticket_id, prior_status)
        return {"ticket_id": ticket_id, "kind": "revised_unverified", "remarks": len(remarks)}
    gate = Gate(
        store,
        cfg,
        repo_root=repo_root,
        evidence_dir=ROOT / cfg.gate.get("evidence_dir", "data/live/evidence"),
        timeout=int(cfg.gate.get("timeout_s", 1800)),
    )
    g = gate.run_gate(sess["id"])
    did = apply_result(g, store, client, poller)
    log(f"{ticket_id}: gate {g.gate_result}: {'; '.join(g.reasons)[:120]} -> {did}")
    return {
        "ticket_id": ticket_id,
        "kind": "revised",
        "gate": g.gate_result,
        "decision": did,
        "remarks": len(remarks),
    }
