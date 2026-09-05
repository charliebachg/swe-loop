"""Replay and record. A clean clone with no key must reproduce the dashboard.

`record(store, path)` dumps every table of a run to JSON. `restore(store, path)` loads it.
`seed(store, cfg, ...)` fills an empty store for replay: from a recorded run when one is
committed under data/replay/, otherwise by running the loop against the fake transport and
synthesising gate verdicts that are labelled as synthesised. The synthesised run always
includes one failure, because a dashboard that has never shown a failure has not been tested.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from swe_loop.config import TargetConfig
from swe_loop.devin import DevinClient, FakeTransport
from swe_loop.dispatch import dispatch
from swe_loop.poll import Poller
from swe_loop.reduce import detect_conflicts, record_merge
from swe_loop.router import route_all
from swe_loop.store import Store, load_tickets

TABLES = (
    "events",
    "tickets",
    "work_orders",
    "sessions",
    "triage_sessions",
    "scan_sessions",
    "evidence",
    "verdicts",
    "escalations",
    "human_actions",
    "budget",
    "timeline",
    "settings",
    "automations",
    "automation_runs",
    "insights",
)

# settings that belong to the machine or the person, never to a recording
PRIVATE_SETTINGS = ("person.name", "watch.started_at", "watch.last_tick", "watch.last_issue_look")


def redactions(extra: list[tuple[str, str]] | None = None) -> list[tuple[str, str]]:
    """Local absolute paths never leave the machine: the target clone, this repository, and the
    home directory are rewritten to portable forms, longest first."""
    import os

    root = Path(__file__).resolve().parents[1]
    pairs = [(str((root.parent / "superset-fork").resolve()), "../superset-fork"), (str(root), ".")]
    import tempfile

    for tmp in {tempfile.gettempdir(), os.path.realpath(tempfile.gettempdir())}:
        if tmp and tmp != "/":
            pairs.append((tmp, "<tmp>"))
    home = os.path.expanduser("~")
    if home and home != "/":
        pairs.append((home, "~"))
    pairs += extra or []
    return sorted(pairs, key=lambda kv: -len(kv[0]))


def record(
    store: Store,
    path: Path | str,
    redact: list[tuple[str, str]] | None = None,
    *,
    evidence_from: Path | str | None = None,
) -> dict[str, int]:
    """Write the store to one JSON file, paths made portable, personal settings left out. With
    evidence_from, the check logs are copied beside it under evidence/, scrubbed the same way, and
    every output_path in the recording points at the copy, so a fresh clone can open them."""
    dump = {t: store._all(f"SELECT * FROM {t}") for t in TABLES}
    dump["settings"] = [r for r in dump["settings"] if r.get("key") not in PRIVATE_SETTINGS]
    path = Path(path)
    pairs = redactions(redact)
    copied = 0
    if evidence_from:
        src_root = Path(evidence_from).resolve()
        dst_root = path.parent / "evidence"
        for r in dump["evidence"]:
            op = r.get("output_path") or ""
            if not op:
                continue
            src = Path(op)
            if not src.is_absolute():
                src = (Path(__file__).resolve().parents[1] / src).resolve()
            try:
                rel = src.resolve().relative_to(src_root)
            except ValueError:
                continue
            if not src.exists():
                continue
            dst = dst_root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            body = src.read_text(errors="replace")
            for a, b in pairs:
                body = body.replace(a, b)
            dst.write_text(body)
            r["output_path"] = str(Path("data") / "replay" / "evidence" / rel)
            copied += 1
    text = json.dumps(dump, indent=1, default=str)
    for a, b in pairs:
        text = text.replace(a, b)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    out = {t: len(rows) for t, rows in dump.items()}
    out["evidence_files"] = copied
    return out


def restore(store: Store, path: Path | str) -> dict[str, int]:
    dump = json.loads(Path(path).read_text())
    counts = {}
    for t in TABLES:
        rows = dump.get(t, [])
        for r in rows:
            cols = ", ".join(r.keys())
            qs = ", ".join("?" for _ in r)
            store.conn.execute(
                f"INSERT OR REPLACE INTO {t} ({cols}) VALUES ({qs})", tuple(r.values())
            )
        counts[t] = len(rows)
    # the recording carries the columns that existed when it was made, so anything added since
    # arrives empty. A ticket with no number shows as blanks on every badge in the app.
    store.number_unnumbered_tickets()
    return counts


def synthesise(
    store: Store, cfg: TargetConfig, tickets_json: Path | str, replay_dir: Path | str | None = None
) -> dict[str, Any]:
    """Run the loop with the fake transport and label the gate results as synthesised."""
    load_tickets(store, tickets_json)
    store.set_budget(acu_cap=300, per_session_cap=cfg.max_acu_limit)
    route_all(store, cfg)
    client = DevinClient(FakeTransport(replay_dir))
    poller = Poller(store, client, cfg, sleep=lambda s: None, clock=lambda: 0.0)
    sids: list[tuple[str, str, str]] = []
    for t in store.list_tickets("routed"):
        for wo in store.work_orders_for(t["id"]):
            if wo["status"] != "devin":
                continue
            sid = dispatch(store, client, wo, cfg)
            poller.wait(sid)
            sids.append((t["id"], wo["shard_id"], sid))
    # gate results, synthesised: every shard passes except the last, which fails T1 once and
    # then passes on retry, so the dashboard shows a retry and a self-report gap
    for i, (tid, shard, sid) in enumerate(sids):
        s = store.get_session(sid)
        tree = f"synthesised-{shard}"
        if i == len(sids) - 1 and len(sids) > 1:
            store.insert_evidence(
                session_id=sid,
                tier="T0",
                command="git diff --name-only base..HEAD",
                cwd="worktree",
                tree_hash=tree,
                exit_code=0,
                output="clean",
                passed=True,
            )
            store.insert_evidence(
                session_id=sid,
                tier="T1",
                command="pandas_3_0_5",
                cwd="worktree",
                tree_hash=tree,
                exit_code=1,
                output="FAILED 1 test (synthesised)",
                passed=False,
            )
            store.insert_verdict(
                session_id=sid,
                gate_result="fail",
                decision="retry",
                reason="synthesised: T1 failed on the first attempt",
                tree_hash=tree,
            )
            poller.retry_with_failure(sid, "FAILED tests (synthesised)")
            store.conn.execute(
                "UPDATE sessions SET terminal_at=?, status='exit', status_detail='finished' WHERE id=?",
                (s["created_at"], sid),
            )
            tree = f"synthesised-{shard}-2"
        store.insert_evidence(
            session_id=sid,
            tier="T0",
            command="git diff --name-only base..HEAD",
            cwd="worktree",
            tree_hash=tree,
            exit_code=0,
            output="clean",
            passed=True,
        )
        store.insert_evidence(
            session_id=sid,
            tier="T1",
            command="pandas_2_3_3_warnings_as_errors",
            cwd="worktree",
            tree_hash=tree,
            exit_code=0,
            output="passed (synthesised)",
            passed=True,
        )
        store.insert_evidence(
            session_id=sid,
            tier="T1",
            command="pandas_3_0_5",
            cwd="worktree",
            tree_hash=tree,
            exit_code=0,
            output="passed (synthesised)",
            passed=True,
        )
        store.insert_verdict(
            session_id=sid,
            gate_result="pass",
            decision="pass",
            reason="synthesised: T0 clean, acceptance exit 0",
            tree_hash=tree,
            review_severity="completed:no issues",
        )
        store.set_ticket_status(tid, "reviewed")
    detect_conflicts(store)
    # a person merged the first two; the rest await review
    for tid, _, _ in sids[:2]:
        record_merge(store, tid, actor="replay")
    return {"sessions": len(sids), "merged": min(2, len(sids)), "synthesised": True}


def seed(
    store: Store, cfg: TargetConfig, *, tickets_json: Path | str, replay_dir: Path | str
) -> dict[str, Any]:
    if store._one("SELECT COUNT(*) AS n FROM tickets")["n"]:
        return {"seeded": False, "reason": "store not empty"}
    run = Path(replay_dir) / "run.json"
    if run.exists():
        return {"seeded": True, "recorded": True, **restore(store, run)}
    return {"seeded": True, "recorded": False, **synthesise(store, cfg, tickets_json, replay_dir)}
