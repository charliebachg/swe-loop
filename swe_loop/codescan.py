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
import re
from pathlib import Path
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


_CLASS_WORDS = {
    "idor": "insecure object reference",
    "sql": "SQL",
    "xss": "cross-site scripting",
    "csrf": "cross-site request forgery",
    "ssrf": "server-side request forgery",
    "info": "information",
    "authz": "authorisation",
    "authn": "authentication",
}


def class_words(cls: str) -> str:
    """Devin names a finding's class in its own shorthand, and prefixes anything that does not
    fit a category with "other". Neither reads as English on a board someone is scanning."""
    cls = re.sub(r"^other[-_ ]+", "", (cls or "").strip())
    parts = [p for p in re.split(r"[-_\s]+", cls) if p]
    return " ".join(_CLASS_WORDS.get(p.lower(), p) for p in parts)


def safe_title(t: dict[str, Any], hide: bool) -> str:
    """What a row may say about a finding before a person has confirmed it."""
    if not (hide and is_unverified_security(t)):
        return t.get("title") or ""
    kind = class_words(t.get("class") or "") or "a security finding"
    return f"{kind}, detail withheld until someone confirms it"


# ------------------------------------------------------------- Devin holds the recurrence
def hand_schedule_to_devin(
    store: Store, client: Any, scan_id: str, rrule: str, *, enabled: bool = False, aid: str
) -> dict[str, Any]:
    """Ask Devin to keep scanning on a recurrence of its own, rather than us running a timer.

    Devin backs the schedule with an Automation on the organisation, so what comes back is the
    id of a real thing that a person can see and switch on. It is created switched off: a scan
    that starts by itself on the morning of a walk-through is not a surprise anyone wants.
    """
    made = client.auto_scan(scan_id, rrule, enabled=enabled)
    store.set_automation(aid, devin_automation_id=made.get("automation_id"))
    store.log(
        "scan",
        "Devin holds the schedule now",
        detail=f"Automation {made.get('automation_id')} · {made.get('rrule')} · "
        + ("switched on" if made.get("enabled") else "switched off"),
    )
    return made


def take_schedule_back(store: Store, client: Any, scan_id: str, *, aid: str) -> None:
    """Remove the recurrence from Devin and forget its id."""
    client.stop_auto_scan(scan_id)
    store.set_automation(aid, devin_automation_id=None)
    store.log("scan", "the schedule was removed from Devin", detail=scan_id)


# ---------------------------------------------------------------- Devin fixes its own finding
def acceptance_for(files: list[str], root: Path) -> dict[str, str]:
    """What to re-run against a fix nobody wrote a ticket for.

    A remediation arrives with no work order, so there are no acceptance commands to inherit.
    What we can always run is the repository's own linter on what changed, and the unit tests
    that sit under the same path. Where no test covers the change the gate records that instead
    of a pass, which is the honest outcome: a fix nothing exercises has not been verified.
    """
    out: dict[str, str] = {}
    if files:
        out["lint"] = ".venv-p2/bin/ruff check " + " ".join(files)
    for f in files:
        parts = Path(f).with_suffix("").parts
        if len(parts) < 2 or parts[0] != "superset":
            continue
        guess = Path("tests/unit_tests", *parts[1:-1], f"{parts[-1]}_test.py")
        alt = Path("tests/unit_tests", *parts[1:-1], f"test_{parts[-1]}.py")
        for cand in (guess, alt, Path("tests/unit_tests", *parts[1:-1])):
            if (root / cand).exists():
                out[f"tests {cand.name}"] = (
                    f".venv-p3/bin/python -m pytest -c pytest.ini -p no:cacheprovider "
                    f"-o addopts= {cand}"
                )
                break
    return out


def remediate(
    settings: Settings,
    cfg: TargetConfig,
    store: Store,
    client: Any,
    ticket_id: str,
    *,
    sleep: Any = None,
    wall_clock: float = 3600.0,
    log: Any = print,
) -> dict[str, Any]:
    """Ask Devin to fix a finding its own scanner reported, then check the result like any other.

    Devin's scanner can open the pull request itself. That is the fastest path from a finding to
    a fix and it is Devin's own feature, so the loop uses it rather than describing one. What the
    loop adds is the part Devin cannot do for itself: the change is re-checked here, on a clean
    copy the session could not write to, before anyone is asked to merge it.
    """
    import time as _time

    ev = store._one("SELECT payload_json FROM events WHERE ticket_id=?", ticket_id)
    if not ev:
        return {"kind": "no_finding", "ticket": ticket_id}
    f = json.loads(ev["payload_json"])
    scan_id, fid = f.get("scan_id"), f.get("finding_id")
    if not (scan_id and fid):
        return {"kind": "no_finding", "ticket": ticket_id}
    sleep = sleep or (
        (lambda s_: _time.sleep(min(s_, 0.05)))
        if getattr(client, "is_fake", False)
        else _time.sleep
    )
    try:
        started = client.remediate(scan_id, fid)
    except Exception as ex:  # noqa: BLE001 - a 409 means it is already being fixed
        store.log(
            "dispatch", "Devin would not start a fix", ticket_id=ticket_id, detail=str(ex)[:200]
        )
        return {"kind": "refused", "ticket": ticket_id, "why": str(ex)[:200]}
    sess = started.get("session_id", "")
    store.set_ticket_status(ticket_id, "dispatched")
    store.log(
        "dispatch",
        "Devin is fixing its own finding",
        ticket_id=ticket_id,
        detail=f"session {sess} on finding {fid}",
    )
    log(f"remediation started for {ticket_id}: session {sess}")

    t0, wait, pr = _time.monotonic(), 10.0, None
    while not pr:
        if _time.monotonic() - t0 > wall_clock:
            store.log("poll", "the fix did not arrive in time", ticket_id=ticket_id)
            return {"kind": "timeout", "ticket": ticket_id, "session": sess}
        sleep(wait)
        wait = min(wait * 1.5, 30.0)
        now_f = next(
            (x for x in client.code_scan_findings(scan_id) if x.get("finding_id") == fid), {}
        )
        pr = now_f.get("pr_url")
        store.log("poll", "waiting on the fix", ticket_id=ticket_id, session_id=None)
    store.log("dispatch", "a pull request was opened", ticket_id=ticket_id, detail=pr)
    log(f"remediation pull request: {pr}")
    return {"kind": "opened", "ticket": ticket_id, "session": sess, "pr": pr, "finding": fid}


def gate_remediation(
    settings: Settings,
    cfg: TargetConfig,
    store: Store,
    ticket_id: str,
    pr_url: str,
    devin_session_id: str,
    *,
    log: Any = print,
) -> str:
    """Put Devin's own fix through the same checks as anything else, and return the session id.

    The work order is built from the file the *finding* named, not from the file the pull request
    happens to touch. Building it from the pull request would make the scope check pass by
    construction, which is not a check. Built from the finding, a fix that wandered into other
    files is caught, exactly as it would be for work the loop dispatched itself.
    """
    ev = store._one("SELECT payload_json FROM events WHERE ticket_id=?", ticket_id)
    f = json.loads(ev["payload_json"]) if ev else {}
    where = where_of(f)
    files = [where] if where else []
    root = Path(__file__).resolve().parents[1] / cfg.gate.get("repo_root", "../superset-fork")
    acceptance = acceptance_for(files, root)
    # Checking the same fix twice must not leave two of everything behind: the row for this
    # Devin session is the one that gets re-checked.
    existing = store.session_by_devin_id(devin_session_id)
    if existing:
        sid = existing["id"]
        store.conn.execute(
            "UPDATE work_orders SET files_json=?, acceptance_json=? WHERE id=?",
            (json.dumps(files), json.dumps(acceptance), existing["work_order_id"]),
        )
        store.conn.commit()
    else:
        wo = store.insert_work_order(
            ticket_id=ticket_id,
            shard_id="remediation",
            files=files,
            tests=[],
            acceptance=acceptance,
            est_size="XS",
        )
        sid = store.reserve_session(
            work_order_id=wo, playbook_id=None, tags=[cfg.session.get("tags_prefix", "swe-loop")]
        )
        store.bind_devin_session(
            sid,
            devin_session_id=devin_session_id,
            url=f"https://app.devin.ai/sessions/{devin_session_id}",
            status="exit",
        )
    store.update_session(
        sid,
        status="exit",
        status_detail="finished",
        # without this the row never leaves the list of what is working right now
        terminal_at=now(),
        pull_request_url=pr_url,
        self_reported_done=1,
        structured_output={"pr_url": pr_url, "files_changed": files, "self_reported_done": True},
    )
    store.log(
        "gate",
        "checking a fix Devin wrote for its own finding",
        ticket_id=ticket_id,
        session_id=sid,
        detail=f"{plural(len(acceptance), 'command')} on {where or 'no file named'}",
    )
    log(f"gating {pr_url} against {where or 'no file named by the finding'}")
    return sid
