"""The ticket store. SQLite with WAL. Every number on the dashboard is a query over these tables.

Rules the schema enforces:
- A session row exists before the Devin API is called. `sessions.id` is ours; `devin_session_id`
  is filled in after the call returns. A crash between the two loses nothing.
- Evidence is bound to the tree it was produced on. A receipt from another tree is stale.
- Human actors are stored as a hash for audit and never rendered.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

TICKET_STATUSES = (
    "new",  # no verdict yet: the triage session scopes it; the router never sees it
    "triaged",
    "routed",
    "dispatched",
    "running",
    "gated",
    "reviewed",
    "merged",
    "escalated",
    "refused",
)
ROUTES = ("devin", "human_only", "refuse")
ESCALATION_KINDS = (
    "router_refused",
    "human_only",
    "usage_limit",
    "waiting_for_user",
    "oracle_touched",
    "detector_still_fires",
    "conflict",
    "budget",
    "review_blocked",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY, source TEXT NOT NULL, received_at TEXT NOT NULL,
  payload_json TEXT NOT NULL, ticket_id TEXT
);
CREATE TABLE IF NOT EXISTS tickets (
  id TEXT PRIMARY KEY, source TEXT NOT NULL, external_ref TEXT, parent_ref TEXT,
  class TEXT, title TEXT NOT NULL, status TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  triage_verdict_json TEXT, router_decision TEXT, router_reason TEXT
);
CREATE TABLE IF NOT EXISTS work_orders (
  id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL REFERENCES tickets(id),
  shard_id TEXT NOT NULL, files_json TEXT NOT NULL, tests_json TEXT NOT NULL,
  acceptance_json TEXT NOT NULL, est_size TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY, work_order_id TEXT NOT NULL REFERENCES work_orders(id),
  devin_session_id TEXT UNIQUE, url TEXT, playbook_id TEXT, tags_json TEXT,
  created_at TEXT NOT NULL, terminal_at TEXT, status TEXT, status_detail TEXT,
  acus_consumed REAL, session_size TEXT, structured_output_json TEXT,
  self_reported_done INTEGER, pull_request_url TEXT, parent_session_id TEXT, attempt INTEGER NOT NULL DEFAULT 1,
  retries INTEGER NOT NULL DEFAULT 0, rejected_output_digest TEXT
);
CREATE TABLE IF NOT EXISTS evidence (
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
  tier TEXT NOT NULL, command TEXT NOT NULL, cwd TEXT NOT NULL, tree_hash TEXT NOT NULL,
  exit_code INTEGER NOT NULL, output_digest TEXT NOT NULL, output_path TEXT,
  passed INTEGER NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS verdicts (
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
  gate_result TEXT NOT NULL, review_severity TEXT, decision TEXT NOT NULL, reason TEXT,
  tree_hash TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS escalations (
  id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL REFERENCES tickets(id), session_id TEXT,
  kind TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL, resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS human_actions (
  id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL REFERENCES tickets(id),
  kind TEXT NOT NULL, at TEXT NOT NULL, actor_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS budget (
  id INTEGER PRIMARY KEY CHECK (id = 1), acu_cap REAL NOT NULL, per_session_cap REAL NOT NULL,
  window_start TEXT NOT NULL, window_end TEXT
);
CREATE TABLE IF NOT EXISTS timeline (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, ticket_id TEXT, session_id TEXT,
  layer TEXT NOT NULL, event TEXT NOT NULL, detail TEXT
);
CREATE INDEX IF NOT EXISTS ix_timeline_session ON timeline(session_id);
CREATE INDEX IF NOT EXISTS ix_tickets_status ON tickets(status);
CREATE INDEX IF NOT EXISTS ix_sessions_wo ON sessions(work_order_id);
CREATE INDEX IF NOT EXISTS ix_evidence_session ON evidence(session_id);
"""


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def digest(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()


class Store:
    def __init__(self, path: Path | str = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)

    # ------------------------------------------------------------------ helpers
    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN")
        try:
            yield self.conn
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def _one(self, sql: str, *args: Any) -> dict[str, Any] | None:
        row = self.conn.execute(sql, args).fetchone()
        return dict(row) if row else None

    def _all(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    # ------------------------------------------------------------------ timeline
    def log(
        self,
        layer: str,
        event: str,
        *,
        ticket_id: str | None = None,
        session_id: str | None = None,
        detail: str | None = None,
    ) -> None:
        """One line per thing that happened. The operational view reads this table."""
        self.conn.execute(
            "INSERT INTO timeline (at, ticket_id, session_id, layer, event, detail) "
            "VALUES (?,?,?,?,?,?)",
            (now(), ticket_id, session_id, layer, event, (detail or "")[:400]),
        )

    def timeline(
        self,
        *,
        session_id: str | None = None,
        ticket_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        q, args = "SELECT * FROM timeline", []
        if session_id:
            q += " WHERE session_id=?"
            args.append(session_id)
        elif ticket_id:
            q += " WHERE ticket_id=?"
            args.append(ticket_id)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        return list(reversed(self._all(q, *args)))

    # ------------------------------------------------------------------ events
    def insert_event(
        self, source: str, payload: dict[str, Any], ticket_id: str | None = None
    ) -> str:
        eid = new_id("evt")
        self.conn.execute(
            "INSERT INTO events VALUES (?,?,?,?,?)",
            (eid, source, now(), json.dumps(payload, sort_keys=True), ticket_id),
        )
        return eid

    # ------------------------------------------------------------------ tickets
    def upsert_ticket(
        self,
        *,
        id: str,
        source: str,
        title: str,
        status: str = "triaged",
        external_ref: str | None = None,
        parent_ref: str | None = None,
        cls: str | None = None,
        triage_verdict: dict[str, Any] | None = None,
        router_decision: str | None = None,
        router_reason: str | None = None,
    ) -> str:
        assert status in TICKET_STATUSES, status
        if router_decision is not None:
            assert router_decision in ROUTES, router_decision
        ts = now()
        self.conn.execute(
            """INSERT INTO tickets (id, source, external_ref, parent_ref, class, title, status,
                 created_at, updated_at, triage_verdict_json, router_decision, router_reason)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET title=excluded.title, status=excluded.status,
                 external_ref=COALESCE(excluded.external_ref, tickets.external_ref),
                 class=COALESCE(excluded.class, tickets.class), updated_at=excluded.updated_at,
                 triage_verdict_json=COALESCE(excluded.triage_verdict_json, tickets.triage_verdict_json),
                 router_decision=COALESCE(excluded.router_decision, tickets.router_decision),
                 router_reason=COALESCE(excluded.router_reason, tickets.router_reason)""",
            (
                id,
                source,
                external_ref,
                parent_ref,
                cls,
                title,
                status,
                ts,
                ts,
                json.dumps(triage_verdict) if triage_verdict else None,
                router_decision,
                router_reason,
            ),
        )
        return id

    def set_ticket_status(self, ticket_id: str, status: str) -> None:
        assert status in TICKET_STATUSES, status
        self.conn.execute(
            "UPDATE tickets SET status=?, updated_at=? WHERE id=?", (status, now(), ticket_id)
        )
        self.log("ticket", status, ticket_id=ticket_id)

    def set_router_decision(self, ticket_id: str, decision: str, reason: str) -> None:
        assert decision in ROUTES, decision
        status = {"devin": "routed", "human_only": "escalated", "refuse": "refused"}[decision]
        self.conn.execute(
            "UPDATE tickets SET router_decision=?, router_reason=?, status=?, updated_at=? WHERE id=?",
            (decision, reason, status, now(), ticket_id),
        )
        self.log("L2 route", decision, ticket_id=ticket_id, detail=reason)
        if decision != "devin":
            self.insert_escalation(
                ticket_id,
                None,
                "human_only" if decision == "human_only" else "router_refused",
                reason,
            )

    def get_ticket(self, ticket_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM tickets WHERE id=?", ticket_id)

    def list_tickets(self, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            return self._all("SELECT * FROM tickets WHERE status=? ORDER BY created_at", status)
        return self._all("SELECT * FROM tickets ORDER BY created_at")

    # ------------------------------------------------------------------ work orders
    def insert_work_order(
        self,
        *,
        ticket_id: str,
        shard_id: str,
        files: list[str],
        tests: list[str],
        acceptance: dict[str, str],
        est_size: str | None = None,
    ) -> str:
        wid = new_id("wo")
        self.conn.execute(
            "INSERT INTO work_orders VALUES (?,?,?,?,?,?,?,?,?)",
            (
                wid,
                ticket_id,
                shard_id,
                json.dumps(files),
                json.dumps(tests),
                json.dumps(acceptance),
                est_size,
                "pending",
                now(),
            ),
        )
        return wid

    def get_work_order(self, wid: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM work_orders WHERE id=?", wid)
        if row:
            row["files"] = json.loads(row.pop("files_json"))
            row["tests"] = json.loads(row.pop("tests_json"))
            row["acceptance"] = json.loads(row.pop("acceptance_json"))
        return row

    def work_orders_for(self, ticket_id: str) -> list[dict[str, Any]]:
        return [
            self.get_work_order(r["id"])
            for r in self._all(
                "SELECT id FROM work_orders WHERE ticket_id=? ORDER BY created_at", ticket_id
            )
        ]

    # ------------------------------------------------------------------ sessions
    def reserve_session(
        self,
        *,
        work_order_id: str,
        playbook_id: str | None,
        tags: list[str],
        attempt: int = 1,
        parent_session_id: str | None = None,
    ) -> str:
        """Write the durable row BEFORE the API call. Returns our id; devin_session_id is null."""
        sid = new_id("ses")
        self.conn.execute(
            """INSERT INTO sessions (id, work_order_id, playbook_id, tags_json, created_at, status, attempt, parent_session_id)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                sid,
                work_order_id,
                playbook_id,
                json.dumps(tags),
                now(),
                "reserved",
                attempt,
                parent_session_id,
            ),
        )
        return sid

    def bind_devin_session(
        self, sid: str, *, devin_session_id: str, url: str, status: str = "new"
    ) -> None:
        self.conn.execute(
            "UPDATE sessions SET devin_session_id=?, url=?, status=? WHERE id=?",
            (devin_session_id, url, status, sid),
        )
        row = self.get_session(sid)
        wo = self.get_work_order(row["work_order_id"]) if row else None
        self.log(
            "L4 dispatch",
            "session bound",
            ticket_id=wo["ticket_id"] if wo else None,
            session_id=sid,
            detail=f"{devin_session_id} {url}",
        )

    def update_session(self, sid: str, **fields: Any) -> None:
        allowed = {
            "status",
            "status_detail",
            "terminal_at",
            "acus_consumed",
            "session_size",
            "structured_output_json",
            "self_reported_done",
            "pull_request_url",
            "rejected_output_digest",
        }
        bad = set(fields) - allowed
        assert not bad, f"unknown session fields: {bad}"
        if "structured_output" in fields:
            fields["structured_output_json"] = json.dumps(fields.pop("structured_output"))
        sets = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE sessions SET {sets} WHERE id=?", (*fields.values(), sid))

    def get_session(self, sid: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM sessions WHERE id=?", sid)

    def session_by_devin_id(self, devin_session_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM sessions WHERE devin_session_id=?", devin_session_id)

    def mark_terminal(
        self,
        sid: str,
        *,
        status: str,
        status_detail: str | None,
        acus_consumed: float | None = None,
    ) -> None:
        """Status and terminal_at in one write, so a crash cannot leave a terminal status
        without its timestamp (or the reverse)."""
        self.conn.execute(
            "UPDATE sessions SET status=?, status_detail=?, terminal_at=?, "
            "acus_consumed=COALESCE(?, acus_consumed) WHERE id=?",
            (status, status_detail, now(), acus_consumed, sid),
        )

    def bound_devin_ids(self) -> set[str]:
        return {
            r["devin_session_id"]
            for r in self._all(
                "SELECT devin_session_id FROM sessions WHERE devin_session_id IS NOT NULL"
            )
        }

    def live_sessions(self) -> list[dict[str, Any]]:
        return self._all(
            "SELECT * FROM sessions WHERE terminal_at IS NULL AND devin_session_id IS NOT NULL"
        )

    def budget_state(self) -> dict[str, Any]:
        """The one definition of spend and cap, used by the dashboard and by enforcement."""
        spent = self._one("SELECT COALESCE(SUM(acus_consumed), 0) AS n FROM sessions")["n"]
        b = self._one("SELECT * FROM budget WHERE id = 1") or {}
        return {
            "spent": spent,
            "cap": b.get("acu_cap"),
            "per_session_cap": b.get("per_session_cap"),
        }

    def sessions_for(self, work_order_id: str) -> list[dict[str, Any]]:
        return self._all(
            "SELECT * FROM sessions WHERE work_order_id=? ORDER BY attempt", work_order_id
        )

    # ------------------------------------------------------------------ evidence
    def insert_evidence(
        self,
        *,
        session_id: str,
        tier: str,
        command: str,
        cwd: str,
        tree_hash: str,
        exit_code: int,
        output: bytes | str,
        output_path: str | None = None,
        passed: bool | None = None,
    ) -> str:
        eid = new_id("evd")
        if passed is None:
            passed = exit_code == 0
        self.conn.execute(
            "INSERT INTO evidence VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                eid,
                session_id,
                tier,
                command,
                cwd,
                tree_hash,
                exit_code,
                digest(output),
                output_path,
                int(passed),
                now(),
            ),
        )
        self.log(
            "L5 gate",
            f"{tier} {'ok' if passed else 'FAIL'} exit {exit_code}",
            session_id=session_id,
            detail=command,
        )
        return eid

    def evidence_for(self, session_id: str, tree_hash: str | None = None) -> list[dict[str, Any]]:
        """Only evidence produced on `tree_hash` counts. Anything else is stale by definition."""
        if tree_hash is None:
            return self._all(
                "SELECT * FROM evidence WHERE session_id=? ORDER BY created_at, rowid", session_id
            )
        return self._all(
            "SELECT * FROM evidence WHERE session_id=? AND tree_hash=? ORDER BY created_at, rowid",
            session_id,
            tree_hash,
        )

    # ------------------------------------------------------------------ verdicts
    def insert_verdict(
        self,
        *,
        session_id: str,
        gate_result: str,
        decision: str,
        reason: str,
        tree_hash: str | None = None,
        review_severity: str | None = None,
    ) -> str:
        assert gate_result in ("pass", "fail", "missing_evidence"), gate_result
        assert decision in ("pass", "retry", "escalate"), decision
        vid = new_id("vrd")
        self.conn.execute(
            "INSERT INTO verdicts VALUES (?,?,?,?,?,?,?,?)",
            (vid, session_id, gate_result, review_severity, decision, reason, tree_hash, now()),
        )
        self.log("L5 gate", f"{gate_result} -> {decision}", session_id=session_id, detail=reason)
        return vid

    def latest_verdict(self, session_id: str) -> dict[str, Any] | None:
        return self._one(
            "SELECT * FROM verdicts WHERE session_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            session_id,
        )

    # ------------------------------------------------------------------ escalations / humans / budget
    def insert_escalation(
        self, ticket_id: str, session_id: str | None, kind: str, reason: str
    ) -> str:
        assert kind in ESCALATION_KINDS, kind
        eid = new_id("esc")
        self.conn.execute(
            "INSERT INTO escalations VALUES (?,?,?,?,?,?,?)",
            (eid, ticket_id, session_id, kind, reason, now(), None),
        )
        self.log("escalate", kind, ticket_id=ticket_id, session_id=session_id, detail=reason)
        return eid

    def list_escalations(self, unresolved_only: bool = True) -> list[dict[str, Any]]:
        q = (
            "SELECT * FROM escalations"
            + (" WHERE resolved_at IS NULL" if unresolved_only else "")
            + " ORDER BY created_at"
        )
        return self._all(q)

    def record_human_action(self, ticket_id: str, kind: str, actor: str) -> str:
        assert kind in ("merge", "review_comment", "approve", "reject", "resolve"), kind
        hid = new_id("hum")
        self.conn.execute(
            "INSERT INTO human_actions VALUES (?,?,?,?,?)",
            (hid, ticket_id, kind, now(), digest(actor)[:16]),
        )
        self.log("L7 reduce", f"human {kind}", ticket_id=ticket_id)
        return hid

    def set_budget(self, acu_cap: float, per_session_cap: float) -> None:
        self.conn.execute(
            "INSERT INTO budget (id, acu_cap, per_session_cap, window_start) VALUES (1,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET acu_cap=excluded.acu_cap, per_session_cap=excluded.per_session_cap",
            (acu_cap, per_session_cap, now()),
        )

    # ------------------------------------------------------------------ the headline queries
    def metrics(self) -> dict[str, Any]:
        """The four tiles at the top of the dashboard, as queries. Nothing is narrated."""
        tickets = self._one("SELECT COUNT(*) AS n FROM tickets WHERE router_decision IS NOT NULL")[
            "n"
        ]  # decided tickets only: a ticket still in triage could not have been verified yet
        verified = self._one(
            """SELECT COUNT(DISTINCT t.id) AS n FROM tickets t
               JOIN work_orders w ON w.ticket_id = t.id
               JOIN sessions s ON s.work_order_id = w.id
               JOIN verdicts v ON v.session_id = s.id AND v.gate_result = 'pass'
               JOIN human_actions h ON h.ticket_id = t.id AND h.kind = 'merge'"""
        )["n"]
        acus = [
            r["acus_consumed"]
            for r in self._all(
                """SELECT s.acus_consumed FROM sessions s
               JOIN verdicts v ON v.session_id = s.id AND v.gate_result = 'pass'
               WHERE s.acus_consumed IS NOT NULL ORDER BY s.acus_consumed"""
            )
        ]
        said = self._one("SELECT COUNT(*) AS n FROM sessions WHERE self_reported_done = 1")["n"]
        passed = self._one(
            "SELECT COUNT(DISTINCT session_id) AS n FROM verdicts WHERE gate_result = 'pass'"
        )["n"]
        budget = self.budget_state()

        def pct(xs: list[float], p: float) -> float | None:
            if not xs:
                return None
            k = max(0, min(len(xs) - 1, round(p * (len(xs) - 1))))
            return xs[k]

        return {
            "verified_changes": {"n": verified, "of": tickets},
            "acu_per_verified": {"median": pct(acus, 0.5), "p95": pct(acus, 0.95), "n": len(acus)},
            "self_reported_vs_verified": {
                "said_done": said,
                "passed_gate": passed,
                "gap": said - passed,
            },
            "budget": budget,
        }

    def funnel(self) -> dict[str, int]:
        c = lambda sql: self._one(sql)["n"]
        return {
            "tickets": c("SELECT COUNT(*) AS n FROM tickets"),
            "routed_to_devin": c("SELECT COUNT(*) AS n FROM tickets WHERE router_decision='devin'"),
            "refused_or_human": c(
                "SELECT COUNT(*) AS n FROM tickets WHERE router_decision IN ('refuse','human_only')"
            ),
            "sessions_created": c(
                "SELECT COUNT(*) AS n FROM sessions WHERE devin_session_id IS NOT NULL"
            ),
            "sessions_terminal": c(
                "SELECT COUNT(*) AS n FROM sessions WHERE terminal_at IS NOT NULL"
            ),
            "gate_passed": c(
                "SELECT COUNT(DISTINCT session_id) AS n FROM verdicts WHERE gate_result='pass'"
            ),
            "gate_failed": c(
                "SELECT COUNT(DISTINCT session_id) AS n FROM verdicts WHERE gate_result IN ('fail','missing_evidence')"
            ),
            "human_merged": c(
                "SELECT COUNT(DISTINCT ticket_id) AS n FROM human_actions WHERE kind='merge'"
            ),
        }


# ---------------------------------------------------------------------- seeding from the inventory
def load_tickets(store: Store, tickets_json: Path | str, source: str = "inventory") -> list[str]:
    """Seed tickets and work orders from data/inventory/<date>/tickets.json (the B5 drafts + numbers)."""
    d = json.loads(Path(tickets_json).read_text())
    repo = d.get("repo", "")
    numbers = d.get("numbers", {})
    parent_ref = f"{repo}#{numbers['P']}" if "P" in numbers else None
    ids: list[str] = []
    for sh in d["shards"]:
        tid = f"tkt_{sh['id']}"
        classes = sorted({c for s in sh["sites"] for c in s["classes"]})
        ext = f"{repo}#{numbers[sh['id']]}" if sh["id"] in numbers else None
        verdict = {
            "acceptance_cmd": sh["acceptance"],
            "context_sufficient": True,
            "split": "one",
            "est_size": "XS" if len(sh["sites"]) == 1 else "S",
            "needs_human": sh["route"] != "devin",
            "review": sh.get("review"),
            "sites": sh["sites"],
        }
        store.upsert_ticket(
            id=tid,
            source=source,
            title=sh["title"],
            status="triaged",
            external_ref=ext,
            parent_ref=parent_ref,
            cls=",".join(classes),
            triage_verdict=verdict,
        )
        if sh["route"] == "devin":
            store.insert_work_order(
                ticket_id=tid,
                shard_id=sh["id"],
                files=sh["files"],
                tests=sh["tests"],
                acceptance=sh["acceptance"],
                est_size=verdict["est_size"],
            )
        ids.append(tid)
    return ids
