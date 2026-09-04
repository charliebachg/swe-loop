"""Route. Policy lives here and in the seam, never in a prompt, so no session can set its
own autonomy. Every decision carries its reason and lands in the store as an escalation when
it is not `devin`."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from swe_loop.config import TargetConfig
from swe_loop.shard import split_work_order
from swe_loop.store import Store


@dataclass(frozen=True)
class Decision:
    route: str  # devin | human_only | refuse
    reason: str
    review: str = "normal"  # normal | required
    shard: dict[str, Any] = field(default_factory=dict)


def _under(path: str, prefixes: list[str]) -> str | None:
    for p in prefixes:
        if path.startswith(p):
            return p
    return None


def _covered(file: str, tests: list[str]) -> bool:
    """A file under a 100%-coverage gate needs a test that plausibly covers it."""
    stem = file.rsplit("/", 1)[-1].removesuffix(".py")
    parent = file.rsplit("/", 2)[-2] if "/" in file else ""
    return any(stem in t or (parent and f"/{parent}/" in t) for t in tests)


def decide(shard: dict[str, Any], cfg: TargetConfig, verdict: dict[str, Any] | None) -> Decision:
    r = cfg.router
    forbidden = list(r.get("forbidden_paths", []))
    cov = list(r.get("coverage_100_paths", []))
    human_classes = set(r.get("human_only_classes", []))
    files = shard["files"]
    tests = shard.get("tests", [])

    for f in files:
        hit = _under(f, forbidden)
        if hit:
            return Decision(
                "refuse", f"{f} is under forbidden path {hit}; sessions never edit it", shard=shard
            )
    for f in files:
        hit = _under(f, cov)
        if hit and not _covered(f, tests):
            return Decision(
                "refuse",
                f"{f} is under {hit}, which runs --cov-fail-under=100, and no listed test covers it",
                shard=shard,
            )
    if shard.get("oversize"):
        return Decision(
            "human_only",
            f"{shard['site_count']} call sites in one file exceeds the per-shard cap; a person scopes it",
            shard=shard,
        )

    sites = [s for s in (verdict or {}).get("sites", []) if s.get("file") in set(files)]
    classes = set()
    for s in sites:
        classes.update(s.get("classes") or ([s["class"]] if s.get("class") else []))
    if not sites and verdict and verdict.get("classes"):
        classes.update(verdict["classes"])
    bad = classes & human_classes
    if bad:
        return Decision(
            "human_only",
            f"class {sorted(bad)} is context-dependent per the upstream release notes; needs manual review",
            shard=shard,
        )
    # only notes about this piece of work matter here; the others belong to another shard
    mine = notes_for(shard, (verdict or {}).get("needs_human"))
    blocking = blocking_notes(cfg, mine)
    if blocking:
        return Decision("human_only", _needs_human_reason(blocking), shard=shard)

    review = "normal"
    silent = [s for s in sites if s.get("warned") and not s.get("broke")]
    advisory = [h for h in (mine or []) if isinstance(h, dict)] if not blocking else []
    if silent or advisory or (verdict or {}).get("review") == "required":
        review = "required"
    reason = f"{len(files)} file(s), {shard.get('site_count', len(files))} site(s), acceptance command present"
    if silent:
        reason += f"; {len(silent)} site(s) warned but did not break: silent behaviour change, review required"
    if advisory:
        reason += (
            f"; {len(advisory)} note(s) for whoever reviews it, so the change is written but a "
            "person signs it off"
        )
    if review == "required" and "review" not in reason:
        reason += "; review required per the triage verdict"
    return Decision("devin", reason, review=review, shard=shard)


def notes_for(shard: dict[str, Any], needs_human: Any) -> Any:
    """The session's notes that are about the files this piece of work covers.

    A note about a test file is not a reason to refuse a change to product code somewhere else."""
    if needs_human is True or not isinstance(needs_human, list):
        return needs_human
    files = set(shard.get("files") or [])
    if not files:
        return needs_human
    out = []
    for h in needs_human:
        if not isinstance(h, dict):
            out.append(h)
            continue
        where = str(h.get("site") or "").rsplit(":", 1)[0] if h.get("site") else ""
        if not where or where in files:
            out.append(h)
    return out


def blocking_notes(cfg: TargetConfig, verdict: Any) -> Any:
    """Which of the session's notes actually stop it from doing the work.

    A note is blocking when the session says so, when the place it names is somewhere a session
    may never edit, or when the kind of change is one the seam reserves for a person. Anything
    else is a note for whoever reviews the change, and the work goes ahead with review forced.
    A session asking for a second opinion is not the same as a session being forbidden."""
    if verdict is True:
        return True  # the whole ticket, in the verdict's own shorthand
    if not isinstance(verdict, list):
        return []
    forbidden = tuple(cfg.forbidden_paths)
    human_classes = {c.lower() for c in cfg.router.get("human_only_classes", [])}
    out = []
    for h in verdict:
        if not isinstance(h, dict):
            out.append({"site": "", "reason": str(h)[:200]})
            continue
        if h.get("blocking") is False:
            continue
        where = str(h.get("site") or "")
        cls = str(h.get("class") or "").lower()
        if h.get("blocking") is True or where.startswith(forbidden) or cls in human_classes:
            out.append(h)
    return out


def _needs_human_reason(needs_human: Any) -> str:
    """The verdict's own words when it gave them; the standing rule otherwise."""
    if isinstance(needs_human, list) and needs_human and isinstance(needs_human[0], dict):
        first = needs_human[0]
        where = str(first.get("site") or "")[:60]
        why = str(first.get("reason") or "")[:140]
        more = f" (+{len(needs_human) - 1} more)" if len(needs_human) > 1 else ""
        return f"triage: {len(needs_human)} site(s) need a person{more}: {where}: {why}"
    return "the AI never edits tests or the build, so this one is your team's"


def route_ticket(store: Store, ticket_id: str, cfg: TargetConfig) -> list[Decision]:
    """Split each work order, decide per shard, write the decisions back. Ticket-level
    decision is `devin` if any shard is; otherwise the strictest of the rest."""
    t = store.get_ticket(ticket_id)
    if not t:
        raise KeyError(ticket_id)
    verdict = json.loads(t["triage_verdict_json"]) if t.get("triage_verdict_json") else None
    wos = store.work_orders_for(ticket_id)
    decisions: list[Decision] = []

    if not wos:
        notes = (verdict or {}).get("needs_human")
        if notes:
            # nothing was scoped for a session, so it goes to a person whether the notes
            # forbid the work or only ask about it
            d = Decision(
                "human_only",
                _needs_human_reason(blocking_notes(cfg, notes) or notes),
                shard={},
            )
        else:
            d = Decision(
                "refuse", "no work order and no triage verdict; nothing to route", shard={}
            )
        store.set_router_decision(ticket_id, d.route, d.reason)
        return [d]

    for wo in wos:
        sites = (verdict or {}).get("sites", [])
        shards = split_work_order(wo, cfg, sites)
        if len(shards) > 1:
            # replace the oversize work order with its shards
            store.conn.execute("UPDATE work_orders SET status=? WHERE id=?", ("split", wo["id"]))
            for sh in shards:
                wid = store.insert_work_order(
                    ticket_id=ticket_id,
                    shard_id=sh["shard_id"],
                    files=sh["files"],
                    tests=sh["tests"],
                    acceptance=sh["acceptance"],
                    est_size=sh["est_size"],
                )
                d = decide(sh, cfg, verdict)
                store.conn.execute("UPDATE work_orders SET status=? WHERE id=?", (d.route, wid))
                decisions.append(
                    Decision(d.route, d.reason, d.review, {**sh, "work_order_id": wid})
                )
        else:
            sh = shards[0]
            d = decide(sh, cfg, verdict)
            store.conn.execute("UPDATE work_orders SET status=? WHERE id=?", (d.route, wo["id"]))
            decisions.append(
                Decision(d.route, d.reason, d.review, {**sh, "work_order_id": wo["id"]})
            )

    routes = [d.route for d in decisions]
    if "devin" in routes:
        top = "devin"
    elif "human_only" in routes:
        top = "human_only"
    else:
        top = "refuse"
    reason = "; ".join(f"{d.shard.get('shard_id')}: {d.route} ({d.reason})" for d in decisions)
    store.set_router_decision(ticket_id, top, reason[:1000])
    for d in decisions:
        if top == "devin" and d.route != "devin":
            store.insert_escalation(
                ticket_id, None, "router_refused" if d.route == "refuse" else "human_only", d.reason
            )
    return decisions


def route_all(store: Store, cfg: TargetConfig) -> dict[str, list[Decision]]:
    return {t["id"]: route_ticket(store, t["id"], cfg) for t in store.list_tickets("triaged")}
