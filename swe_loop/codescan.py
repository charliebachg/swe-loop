"""Devin's own code scans as a source of work.

Devin ships a scanner. It is not a session we prompt: it is started against a repository with an
*area* to look in, it runs its own orchestrator session, and its findings are read back from the
organisation. So this module starts one, waits, and turns each finding into a ticket the rest of
the loop already knows how to handle. Nothing here re-implements finding; the point is to consume
what Devin found.

Two rules the findings meet before they reach the board.

A file another change already owns is refused, for the same reason a prompt-driven scan's finding
is: two open changes to one file collide at the merge.

A security finding is not filed as a defect. Superset's own `AGENTS.md` requires that an
automated security finding name the capability row in `SECURITY.md` it believes is violated and
the principal the attacker is assumed to hold, and says that a finding which cannot identify both
"should be filed as questions, not vulnerabilities". Devin's scanner does not return either
field, so these go to a person to answer rather than to a session to fix.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from swe_loop.config import Settings, TargetConfig
from swe_loop.store import Store, clip, now, plural

# Devin stamps one of these on a scan. Only security runs without a profile; the rest are
# rejected without one, and this organisation has none.
AREAS = (
    "security",
    "performance",
    "db-queries",
    "test-coverage",
    "dead-code",
    "code-quality",
    "telemetry",
    "accessibility",
    "general",
    "migration-docs",
)
TERMINAL = ("completed", "failed", "cancelled")


def finding_id(f: dict[str, Any]) -> str:
    """Stable across scans, so the same finding twice is one ticket, not two."""
    seed = f.get("finding_id") or f"{where_of(f)}:{f.get('title', '')}"
    return "tkt_cs" + hashlib.sha256(str(seed).encode()).hexdigest()[:8]


def where_of(f: dict[str, Any]) -> str:
    """The file a finding points at, from the first snippet it offered as evidence."""
    for s in f.get("reference_snippets") or []:
        if s.get("file_path"):
            return str(s["file_path"])
    return ""


def line_of(f: dict[str, Any]) -> Any:
    for s in f.get("reference_snippets") or []:
        if s.get("start_line") is not None:
            return s["start_line"]
    return None


def file_findings(
    store: Store, cfg: TargetConfig, findings: list[dict[str, Any]], limit: int | None = None
) -> dict[str, Any]:
    """Each finding becomes a ticket, in the state a new ticket starts in."""
    forbidden = tuple(cfg.forbidden_paths)
    reserved = tuple(cfg.scan.get("reserved_paths") or ())
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    findings = sorted(findings, key=lambda f: order.get(str(f.get("severity")), 9))
    dropped = 0
    if limit is not None and len(findings) > limit:
        dropped = len(findings) - limit
        findings = findings[:limit]
    new, known, refused, taken, questions = [], [], [], [], []
    for f in findings:
        where = where_of(f)
        if where.startswith(forbidden):
            refused.append(where)
            continue
        if where in reserved:
            taken.append(where)
            continue
        tid = finding_id(f)
        if store.get_ticket(tid):
            known.append(tid)
            continue
        title = clip(str(f.get("title") or where or "a finding with no title"), 190)
        store.insert_event("code_scan", f, ticket_id=tid)
        store.upsert_ticket(
            id=tid,
            source="code_scan",
            title=title,
            status="new",
            cls=str(f.get("category") or "") or None,
        )
        store.log(
            "intake",
            "Devin's scanner found something and filed it",
            ticket_id=tid,
            detail=f"{where}:{line_of(f)} · {f.get('severity', 'unrated')} · "
            + clip(str(f.get("description") or ""), 120),
        )
        # Superset's own rule, from its AGENTS.md: an automated security finding must name the
        # capability row in SECURITY.md it violates and the principal the attacker holds, and one
        # that cannot name both "should be filed as questions, not vulnerabilities". Devin's
        # scanner returns neither field, so every one of these goes to a person. Routing them to
        # a session would be filing vulnerability reports the repository has said it does not
        # want, against a project nobody here maintains.
        store.set_router_decision(
            tid,
            "human_only",
            "Devin's scanner reported this. This repository requires an automated security "
            "finding to name the capability row in SECURITY.md it violates and the principal "
            "the attacker holds, and to be filed as a question when it cannot name both. The "
            "scanner returns neither, so a person answers it before any session touches it.",
        )
        store.set_ticket_status(tid, "escalated")
        store.insert_escalation(
            tid, None, "human_only", "a security finding is a question until a person answers it"
        )
        questions.append(tid)
        new.append(tid)
    return {
        "new": new,
        "known": known,
        "refused": refused,
        "taken": taken,
        "questions": questions,
        "dropped": dropped,
    }


def run(
    settings: Settings,
    cfg: TargetConfig,
    store: Store,
    client: Any,
    *,
    area: str | None = None,
    profile_id: str | None = None,
    limit: int | None = None,
    sleep: Any = None,
    wall_clock: float = 3600.0,
    log: Any = print,
) -> dict[str, Any]:
    """Start one of Devin's scans, wait for it, and file what it found."""
    import time as _time

    area = area or cfg.scan.get("devin_area") or "security"
    profile_id = profile_id or cfg.scan.get("devin_profile_id") or None
    if area not in AREAS:
        raise ValueError(f"{area} is not one of Devin's scan types: {', '.join(AREAS)}")
    if area != "security" and not profile_id:
        # Devin rejects this itself; saying so here costs nothing and reads better than a 400
        raise ValueError(
            f"a {area} scan needs a scan profile. Only security runs without one, and a profile "
            "can only be made in the Devin console: every write on the profile endpoints is 405 "
            "and the API offers no way to create one. Make it there, then put its id in the "
            "seam as scan.devin_profile_id."
        )
    limit = limit or int(cfg.scan.get("max_findings", 5))
    sleep = sleep or (
        (lambda s_: _time.sleep(min(s_, 0.05)))
        if getattr(client, "is_fake", False)
        else _time.sleep
    )
    started = client.start_code_scan(repo_name=cfg.repo, scan_type=area, profile_id=profile_id)
    scan_id = started.get("scan_id")
    sid = store.insert_scan_session(
        devin_session_id=scan_id,
        url=started.get("orchestrator_session_id") or "",
        status=started.get("status") or "running",
        status_detail=area,
        playbook_id=None,
        tags=[cfg.session.get("tags_prefix", "swe-loop"), "code_scan", area],
    )
    store.log(
        "scan",
        "Devin's scanner started",
        session_id=sid,
        detail=f"{area} on {cfg.repo}" + (f" · profile {profile_id}" if profile_id else ""),
    )
    log(f"code scan {scan_id} started: {area} on {cfg.repo}")

    t0, wait, state = _time.monotonic(), 10.0, started
    while str(state.get("status")) not in TERMINAL:
        if _time.monotonic() - t0 > wall_clock:
            store.update_scan_session(sid, terminal_at=now(), outcome="timeout", status="error")
            return {"kind": "timeout", "scan": scan_id}
        sleep(wait)
        wait = min(wait * 1.5, 30.0)
        state = client.code_scan(scan_id) or state
        store.update_scan_session(sid, status=str(state.get("status")))
        store.log("scan", f"{state.get('status')}", session_id=sid)

    if str(state.get("status")) != "completed":
        store.update_scan_session(
            sid, terminal_at=now(), outcome=str(state.get("status")), status="error"
        )
        log(f"code scan {state.get('status')}")
        return {"kind": str(state.get("status")), "scan": scan_id}

    found = client.code_scan_findings(scan_id)
    filed = file_findings(store, cfg, found, limit)
    store.update_scan_session(
        sid,
        terminal_at=now(),
        outcome="filed",
        status="exit",
        status_detail="finished",
        findings={"findings": found},
    )
    store.log(
        "scan",
        f"filed {plural(len(filed['new']), 'new ticket')}",
        session_id=sid,
        detail=f"{plural(len(found), 'finding')} from Devin's {area} scan"
        + (f", {filed['dropped']} past the cap" if filed["dropped"] else ""),
    )
    log(f"code scan: {len(filed['new'])} new, {len(filed['known'])} already known")
    return {"kind": "filed", "scan": scan_id, "area": area, "findings": len(found), **filed}


def summarise(store: Store) -> dict[str, Any]:
    """What Devin's scanner has reported here, for the pages."""
    rows = [
        json.loads(e["payload_json"])
        for e in store._all("SELECT payload_json FROM events WHERE source='code_scan'")
    ]
    sev: dict[str, int] = {}
    for f in rows:
        k = str(f.get("severity") or "unrated")
        sev[k] = sev.get(k, 0) + 1
    return {"findings": len(rows), "by_severity": sev}


# ---------------------------------------------------------------- showing them safely
MASK_SETTING = "show_security_detail"


def masked(store: Store) -> bool:
    """Whether an unverified security finding hides its detail on screen.

    On by default. These are claims about somebody else's production software that nobody has
    confirmed, and this dashboard is meant to be put on a shared screen. Reading a file and line
    out of it publishes an unreviewed vulnerability report with no disclosure process behind it.
    A person who needs the detail opens the ticket; the setting turns it off for a private look.
    """
    return (store.get_setting(MASK_SETTING) or "") != "1"


def is_unverified_security(t: dict[str, Any]) -> bool:
    """A finding Devin's scanner reported and nobody has stood behind yet."""
    return t.get("source") == "code_scan" and "SECURITY.md" in (t.get("router_reason") or "")


def safe_title(t: dict[str, Any], hide: bool) -> str:
    """What a row may say about a finding before a person has confirmed it."""
    if not (hide and is_unverified_security(t)):
        return t.get("title") or ""
    kind = (t.get("class") or "").replace("-", " ").strip() or "a security finding"
    return f"{kind}, detail withheld until someone confirms it"
