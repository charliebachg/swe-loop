"""The scan step: a session reads the repository and files what it finds as tickets.

Every other way work enters this loop needs someone to have written a ticket first. This one does
not: it points a session at the repository, and what it finds becomes tickets that go through the
same triage, routing, checks and review as anything else. The session reads and reports; it
changes nothing, and nothing it reports is trusted beyond being written down as a ticket for the
loop to scope."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from swe_loop.config import Settings, TargetConfig
from swe_loop.devin import SessionSpec
from swe_loop.store import Store, now

ROOT = Path(__file__).resolve().parents[1]
SCAN_ACU_CAP = 4


def load_schema() -> dict[str, Any]:
    return json.loads((ROOT / "schemas" / "scan_findings.schema.json").read_text())


def validate(out: Any) -> list[str]:
    """What the loop insists on before a finding is written down as a ticket."""
    if not isinstance(out, dict):
        return ["output is not an object"]
    problems = []
    if not isinstance(out.get("searched"), str) or not out["searched"].strip():
        problems.append("searched is missing")
    findings = out.get("findings")
    if not isinstance(findings, list):
        return [*problems, "findings is not a list"]
    for i, f in enumerate(findings):
        if not isinstance(f, dict):
            problems.append(f"finding {i} is not an object")
            continue
        for key in ("title", "file", "class", "why"):
            if not str(f.get(key) or "").strip():
                problems.append(f"finding {i} has no {key}")
        if not isinstance(f.get("line"), int):
            problems.append(f"finding {i} has no line number")
    return problems


def finding_id(f: dict[str, Any]) -> str:
    """A ticket id that is the same for the same place, so scanning twice does not file twice."""
    key = f"{f.get('file', '')}:{f.get('line', '')}".encode()
    return "tkt_sc" + hashlib.sha256(key).hexdigest()[:8]


def build_prompt(cfg: TargetConfig, limit: int) -> str:
    look = cfg.scan.get("look_for") or f"behaviour that changes in the {cfg.name} upgrade"
    versions = cfg.session.get("version_range", "")
    return (
        "## What\n"
        f"Read `{cfg.repo}` on branch `{cfg.base_branch}` and find places where {look}. "
        "Report what you find. Do not change anything.\n\n"
        "## How\n"
        "Do:\n"
        f"- Work from the library's own upgrade notes for {versions or 'the two versions named'}.\n"
        "- Prefer evidence a command produced over a pattern you recognise. The project's own "
        "tests with warnings promoted to errors are the best evidence available.\n"
        "- Read the surrounding function before deciding a site is affected.\n"
        f"- Report at most {limit} findings, strongest evidence first, and fewer if that is the "
        "honest answer.\n"
        "Don't:\n"
        f"- Touch any file, or anything under {', '.join(cfg.forbidden_paths)}.\n"
        "- Open a pull request, push a branch, or comment anywhere.\n"
        "- Report a site you have not read, or pad the list to reach the maximum.\n\n"
        "## Result\n"
        "Provide structured output matching the findings schema and call "
        "provide_structured_output with is_final=true: searched, and findings with title, file, "
        "line, class, why, tests and confidence."
    )


def build_spec(cfg: TargetConfig, limit: int, playbook_id: str | None) -> SessionSpec:
    return SessionSpec(
        prompt=build_prompt(cfg, limit),
        tags=(cfg.session.get("tags_prefix", "swe-loop"), "scan"),
        repos=(cfg.repo,),
        max_acu_limit=min(SCAN_ACU_CAP, cfg.max_acu_limit),
        structured_output_schema=load_schema(),
        playbook_id=playbook_id,
        title=f"scan {cfg.repo}",
    )


def file_findings(store: Store, cfg: TargetConfig, out: dict[str, Any]) -> dict[str, Any]:
    """Each finding becomes a ticket the loop has never seen, in the state a new ticket starts in.

    A finding already filed is left alone: scanning the same repository twice must not fill the
    board with duplicates."""
    forbidden = tuple(cfg.forbidden_paths)
    new, known, refused = [], [], []
    for f in out.get("findings") or []:
        where = str(f.get("file") or "")
        if where.startswith(forbidden):
            refused.append(where)  # the scan was told to stay out; the loop does not relent
            continue
        tid = finding_id(f)
        if store.get_ticket(tid):
            known.append(tid)
            continue
        store.insert_event("scan", f, ticket_id=tid)
        store.upsert_ticket(
            id=tid,
            source="scan",
            title=str(f.get("title") or f"{where}:{f.get('line')}")[:200],
            status="new",
            cls=str(f.get("class") or "") or None,
        )
        store.log(
            "intake",
            "a scan found something and filed it",
            ticket_id=tid,
            detail=f"{where}:{f.get('line')} · {f.get('confidence', 'unrated')} · {str(f.get('why') or '')[:120]}",
        )
        new.append(tid)
    return {"new": new, "known": known, "refused": refused}


def run_scan(
    settings: Settings,
    cfg: TargetConfig,
    store: Store,
    client: Any,
    *,
    limit: int | None = None,
    playbook_id: str | None = None,
    sleep: Any = None,
    wall_clock: float = 3600.0,
    log: Any = print,
) -> dict[str, Any]:
    """One scan session, start to findings. A session that ends without output files nothing."""
    import time as _time

    limit = limit or int(cfg.scan.get("max_findings", 3))
    sleep = sleep or (
        (lambda s_: _time.sleep(min(s_, 0.05)))
        if getattr(client, "is_fake", False)
        else _time.sleep
    )
    spec = build_spec(cfg, limit, playbook_id)
    state = client.start(spec)
    store.log(
        "intake", "scan session started", detail=f"{state.session_id} · at most {limit} findings"
    )
    started = _time.monotonic()
    wait = 5.0
    while not (state.delivered or state.terminal):
        if _time.monotonic() - started > wall_clock:
            store.log("intake", "scan session ran out of time", detail=state.session_id)
            return {"kind": "timeout", "session": state.session_id}
        sleep(wait)
        wait = min(wait * 1.5, 30.0)
        state = client.status(state.session_id)
    out = state.structured_output
    if not out:
        store.log("intake", "scan session ended without findings", detail=state.session_id)
        return {"kind": "no_output", "session": state.session_id}
    problems = validate(out)
    if problems:
        store.log("intake", "scan output rejected", detail="; ".join(problems)[:200])
        return {"kind": "invalid", "session": state.session_id, "problems": problems}
    filed = file_findings(store, cfg, out)
    store.log(
        "intake",
        f"scan filed {len(filed['new'])} new ticket(s)",
        detail=f"{out.get('searched', '')[:160]}",
    )
    log(f"scan: {len(filed['new'])} new, {len(filed['known'])} already known")
    return {"kind": "filed", "session": state.session_id, "at": now(), **filed}
