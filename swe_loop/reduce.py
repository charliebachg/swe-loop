"""Merge. Writes stay single-threaded and a person merges.

Sessions read and reason in parallel; nothing here merges anything. Reduce answers two
questions per ticket: are all of its shards verified and reviewed, and do any two shards touch
the same file (a cross-shard conflict, its own escalation class). Then it records the human
merge when a person clicks the button, with the actor hashed for audit and never rendered.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from swe_loop.store import Store


@dataclass
class TicketReadiness:
    ticket_id: str
    status: str
    shards: int = 0
    verified: int = 0
    reviewed: bool = False
    conflicts: list[tuple[str, str, str]] = field(default_factory=list)  # (file, shard, shard)
    pr_urls: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return (
            self.shards > 0
            and self.verified == self.shards
            and self.reviewed
            and not self.conflicts
        )


def _latest_pass(store: Store, work_order_id: str) -> dict[str, Any] | None:
    for s in reversed(store.sessions_for(work_order_id)):
        v = store.latest_verdict(s["id"])
        if v and v["gate_result"] == "pass":
            return {**s, "verdict": v}
    return None


def merge_notes(store: Store, ticket_id: str) -> dict[str, Any]:
    """What the merger should read before clicking: the review outcome per PR and the notes
    the session itself left under needs_human. Both come from the latest passing session."""
    reviews: list[str] = []
    notes: list[dict[str, Any]] = []
    for wo in store.work_orders_for(ticket_id):
        p = _latest_pass(store, wo["id"])
        if not p:
            continue
        sev = (p["verdict"].get("review_severity") or "").split(":", 1)
        state = sev[1] if len(sev) == 2 else sev[0]
        pr = (p.get("pull_request_url") or "").rsplit("/", 1)[-1]
        reviews.append(f"PR #{pr}: {state}" if pr else state)
        out = json.loads(p["structured_output_json"]) if p.get("structured_output_json") else {}
        for h in out.get("needs_human") or []:
            if isinstance(h, dict):
                notes.append(
                    {
                        "shard": wo["shard_id"],
                        "site": h.get("site", ""),
                        "reason": h.get("reason", ""),
                    }
                )
    return {"reviews": reviews, "notes": notes}


def readiness(store: Store, ticket_id: str) -> TicketReadiness:
    t = store.get_ticket(ticket_id)
    r = TicketReadiness(ticket_id, t["status"])
    files_by_shard: dict[str, set[str]] = {}
    for wo in store.work_orders_for(ticket_id):
        if wo["status"] in ("split", "refuse", "human_only"):
            continue
        r.shards += 1
        files_by_shard[wo["shard_id"]] = set(wo["files"])
        p = _latest_pass(store, wo["id"])
        if p:
            r.verified += 1
            if p["pull_request_url"]:
                r.pr_urls.append(p["pull_request_url"])
    r.reviewed = t["status"] == "merged" or (
        t["status"] == "reviewed"
        and r.verified > 0
        and all(
            (
                ((_latest_pass(store, wo["id"]) or {}).get("verdict") or {}).get("review_severity")
                or ""
            ).startswith("completed:")
            for wo in store.work_orders_for(ticket_id)
            if wo["status"] not in ("split", "refuse", "human_only")
        )
    )
    shards = sorted(files_by_shard)
    for i, a in enumerate(shards):
        for b in shards[i + 1 :]:
            for f in sorted(files_by_shard[a] & files_by_shard[b]):
                r.conflicts.append((f, a, b))
    return r


def detect_conflicts(store: Store) -> list[dict[str, Any]]:
    """Across every open ticket: two live shards claiming the same file. Recorded once."""
    owners: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for t in store.list_tickets():
        if t["status"] in ("merged", "refused"):
            continue
        for wo in store.work_orders_for(t["id"]):
            if wo["status"] in ("split", "refuse", "human_only"):
                continue
            for f in wo["files"]:
                owners[f].append((t["id"], wo["shard_id"]))
    found = []
    for f, claims in owners.items():
        if len(claims) < 2:
            continue
        tickets = sorted({c[0] for c in claims})
        reason = f"{f} is claimed by shards {sorted(c[1] for c in claims)} across tickets {tickets}"
        for tid in tickets:
            dup = store.conn.execute(
                "SELECT 1 FROM escalations WHERE ticket_id=? AND kind='conflict' AND reason=?",
                (tid, reason),
            ).fetchone()
            if not dup:
                store.insert_escalation(tid, None, "conflict", reason)
        found.append({"file": f, "claims": claims})
    return found


def record_merge(
    store: Store, ticket_id: str, actor: str, pr_url: str | None = None
) -> dict[str, Any]:
    """A person merged on GitHub and says so. The actor is hashed; never rendered."""
    r = readiness(store, ticket_id)
    if not r.ready:
        raise ValueError(
            f"{ticket_id} is not ready to merge: verified {r.verified}/{r.shards}, "
            f"reviewed={r.reviewed}, conflicts={len(r.conflicts)}"
        )
    hid = store.record_human_action(ticket_id, "merge", actor)
    store.set_ticket_status(ticket_id, "merged")
    store.conn.execute(
        "UPDATE work_orders SET status='merged' WHERE ticket_id=? AND status NOT IN ('split','refuse','human_only')",
        (ticket_id,),
    )
    return {
        "ticket_id": ticket_id,
        "human_action_id": hid,
        "pr_urls": r.pr_urls,
        "noted_pr": pr_url,
    }


def summary(store: Store) -> dict[str, Any]:
    tickets = [readiness(store, t["id"]) for t in store.list_tickets()]
    return {
        "ready": [t.ticket_id for t in tickets if t.ready and t.status != "merged"],
        "merged": [t.ticket_id for t in tickets if t.status == "merged"],
        "waiting": [
            {
                "ticket_id": t.ticket_id,
                "verified": t.verified,
                "shards": t.shards,
                "reviewed": t.reviewed,
            }
            for t in tickets
            if not t.ready and t.status not in ("merged", "refused", "escalated") and t.shards
        ],
        "conflicts": [c for t in tickets for c in t.conflicts],
    }


# ---------------------------------------------------------------------------- Devin Review readback
_REVIEW_BOT = "devin-ai-integration[bot]"


def _github_review_outcome(
    pr_url: str, token: str, fetch: Any = None, since: str | None = None
) -> str | None:
    """What Devin Review posted on the pull request: 'no issues' or 'N comment(s)'. None when
    it cannot be read (no token, network)."""
    import httpx

    if not token or "/pull/" not in pr_url:
        return None
    owner_repo, _, num = pr_url.split("github.com/", 1)[-1].partition("/pull/")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    get = fetch or (lambda url: httpx.get(url, headers=headers, timeout=15).json())
    try:
        reviews = get(f"https://api.github.com/repos/{owner_repo}/pulls/{num}/reviews")
        comments = get(f"https://api.github.com/repos/{owner_repo}/pulls/{num}/comments")
    except Exception:  # noqa: BLE001 - the review stays 'completed' without detail
        return None
    mine = [r for r in reviews if (r.get("user") or {}).get("login") == _REVIEW_BOT]
    if not mine:
        return None
    body = mine[-1].get("body") or ""
    inline = [c for c in comments if (c.get("user") or {}).get("login") == _REVIEW_BOT]
    if since:
        # only what this review round added: the request time is the verdict's; GitHub timestamps are UTC
        inline = [
            c for c in inline if str(c.get("created_at") or "") >= since.replace("+00:00", "Z")
        ]
        mine = [
            r for r in mine if str(r.get("submitted_at") or "") >= since.replace("+00:00", "Z")
        ] or mine
    if "No Issues Found" in body and not inline:
        return "no issues"
    return f"{len(inline)} comment(s)"


def refresh_reviews(store: Store, client: Any, github_token: str = "", fetch: Any = None) -> int:
    """Read back every requested Devin Review. The request happens at the gate; the result
    is read here so the Tracker and the Review page show it. Returns how many were updated."""
    rows = store._all(
        "SELECT v.id, v.session_id, v.created_at, s.pull_request_url AS pr_url, s.work_order_id FROM verdicts v "
        "JOIN sessions s ON s.id = v.session_id WHERE v.review_severity LIKE 'requested%' "
        "AND s.pull_request_url IS NOT NULL"
    )
    n = 0
    for r in rows:
        try:
            state = client.t.get_pr_review(r["pr_url"])
        except Exception as ex:  # noqa: BLE001 - leave it requested; try again next pass
            store.log(
                "review", "status read failed", session_id=r["session_id"], detail=str(ex)[:120]
            )
            continue
        if state.get("status") != "completed":
            continue
        outcome = _github_review_outcome(
            r["pr_url"], github_token, fetch, since=r.get("created_at")
        )
        label = "completed:" + (outcome or "see the pull request")
        store.conn.execute("UPDATE verdicts SET review_severity=? WHERE id=?", (label, r["id"]))
        store.conn.commit()
        wo = store.get_work_order(r["work_order_id"])
        store.log(
            "review",
            f"Devin Review completed: {outcome or 'see the pull request'}",
            ticket_id=wo["ticket_id"] if wo else None,
            session_id=r["session_id"],
            detail=f"{r['pr_url']} commit {str(state.get('commit_sha', ''))[:10]}",
        )
        n += 1
    return n
