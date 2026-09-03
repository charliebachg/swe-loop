"""L7: reduce. Writes stay single-threaded and a person merges.

Sessions read and reason in parallel; nothing here merges anything. Reduce answers two
questions per ticket: are all of its shards verified and reviewed, and do any two shards touch
the same file (a cross-shard conflict, its own escalation class). Then it records the human
merge when a person clicks the button, with the actor hashed for audit and never rendered.
"""

from __future__ import annotations

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
    r.reviewed = t["status"] in ("reviewed", "merged")
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
