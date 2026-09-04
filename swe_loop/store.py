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
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

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
  id TEXT PRIMARY KEY, number INTEGER, source TEXT NOT NULL, external_ref TEXT, parent_ref TEXT,
  class TEXT, title TEXT NOT NULL, status TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  triage_verdict_json TEXT, router_decision TEXT, router_reason TEXT
);
CREATE TABLE IF NOT EXISTS automation_runs (
  id TEXT PRIMARY KEY, automation_id TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
  status TEXT NOT NULL, result_json TEXT
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
  retries INTEGER NOT NULL DEFAULT 0, rejected_output_digest TEXT, cost_usd REAL
);
CREATE TABLE IF NOT EXISTS triage_sessions (
  id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL REFERENCES tickets(id),
  devin_session_id TEXT UNIQUE, url TEXT, playbook_id TEXT, tags_json TEXT,
  created_at TEXT NOT NULL, terminal_at TEXT, status TEXT, status_detail TEXT,
  acus_consumed REAL, verdict_json TEXT, outcome TEXT, cost_usd REAL
);
CREATE TABLE IF NOT EXISTS scan_sessions (
  id TEXT PRIMARY KEY, devin_session_id TEXT UNIQUE, url TEXT, playbook_id TEXT, tags_json TEXT,
  created_at TEXT NOT NULL, terminal_at TEXT, status TEXT, status_detail TEXT,
  acus_consumed REAL, findings_json TEXT, outcome TEXT, cost_usd REAL
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
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS automations (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
  availability TEXT NOT NULL DEFAULT 'live', trigger_json TEXT NOT NULL, target TEXT NOT NULL,
  playbook TEXT, max_acu REAL, max_findings INTEGER, concurrency INTEGER NOT NULL DEFAULT 4,
  schedule TEXT, notes TEXT,
  created_at TEXT NOT NULL, last_run TEXT, last_result TEXT
);
CREATE TABLE IF NOT EXISTS playbooks (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, agent TEXT NOT NULL, body TEXT NOT NULL,
  schema_json TEXT, max_acu REAL, source TEXT NOT NULL, availability TEXT NOT NULL DEFAULT 'live',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_timeline_session ON timeline(session_id);
CREATE INDEX IF NOT EXISTS ix_tickets_status ON tickets(status);
CREATE INDEX IF NOT EXISTS ix_sessions_wo ON sessions(work_order_id);
CREATE INDEX IF NOT EXISTS ix_evidence_session ON evidence(session_id);
CREATE TABLE IF NOT EXISTS insights (
  devin_session_id TEXT PRIMARY KEY, fetched_at TEXT NOT NULL, payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_timeline_ticket ON timeline(ticket_id, at);
CREATE INDEX IF NOT EXISTS ix_timeline_at ON timeline(at);
"""


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def clip(text: str, n: int) -> str:
    """Shorten to n characters on a word boundary, and say that it was shortened. Cutting mid
    word reads as a rendering fault, which is worse than the missing words."""
    t = (text or "").strip()
    if len(t) <= n:
        return t
    cut = t[:n].rsplit(" ", 1)[0].rstrip(" ,.;:-")
    return (cut or t[:n].rstrip()) + "\u2026"


def _must(ok: bool, why: str) -> None:
    """A check that survives `python -O`. Every one of these guards a column name on its way
    into a SQL statement, so an assert, which -O deletes, is the wrong tool."""
    if not ok:
        raise ValueError(why)


def plural(n: int, one: str, many: str = "") -> str:
    """A count and its noun, written the way a person writes it. Nothing on a page a person
    reads should say "3 ticket(s)"."""
    return f"{n} {one if n == 1 else (many or one + 's')}"


def digest(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()


# Step names written by earlier versions of this code, and what they are called now.
STEP_RENAMES = {
    "L0 intake": "intake",
    "L1 triage": "triage",
    "L2 route": "route",
    "L3 shard": "shard",
    "L4 dispatch": "dispatch",
    "L4 poll": "poll",
    "L4 manage": "steer",
    "L5 gate": "gate",
    "L6 review": "review",
    "L7 reduce": "merge",
}


class Store:
    """One SQLite file. Each thread gets its own connection to it: the web server answers
    requests while a run thread writes, and SQLite's WAL mode lets both proceed. A single
    connection shared across threads would interleave cursors and fail in the middle of a run."""

    def __init__(self, path: Path | str = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._memory: sqlite3.Connection | None = None
        self.conn.executescript(SCHEMA)
        for table in ("sessions", "triage_sessions", "scan_sessions"):
            cols = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if "cost_usd" not in cols:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN cost_usd REAL")
        for table, col, decl in (
            ("tickets", "number", "INTEGER"),
            ("sessions", "pr_state", "TEXT"),
            ("automations", "max_findings", "INTEGER"),
            # when Devin runs the recurrence itself, the id of the Automation it made for us
            ("automations", "devin_automation_id", "TEXT"),
        ):
            cols = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if col not in cols:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        # a store written before ticket numbers existed: number them in the order they arrived
        for i, r in enumerate(
            self.conn.execute(
                "SELECT id FROM tickets WHERE number IS NULL ORDER BY created_at, rowid"
            ).fetchall(),
            start=(self.conn.execute("SELECT COALESCE(MAX(number), 0) FROM tickets").fetchone()[0])
            + 1,
        ):
            self.conn.execute("UPDATE tickets SET number=? WHERE id=?", (i, r[0]))
        for old, new in STEP_RENAMES.items():
            self.conn.execute("UPDATE timeline SET layer=? WHERE layer=?", (new, old))
        self.conn.commit()

    def _connect(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False, timeout=30.0)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA busy_timeout=30000")
        return c

    @property
    def conn(self) -> sqlite3.Connection:
        """This thread's connection, opened on first use."""
        if self.path == ":memory:":
            if self._memory is None:
                self._memory = self._connect()
            return self._memory
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._connect()
            self._local.conn = c
        return c

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
        _must(status in TICKET_STATUSES, f"unknown ticket status: {status}")
        if router_decision is not None:
            _must(router_decision in ROUTES, f"unknown route: {router_decision}")
        ts = now()
        number = (self.get_ticket(id) or {}).get("number") or (
            self.conn.execute("SELECT COALESCE(MAX(number), 0) + 1 FROM tickets").fetchone()[0]
        )
        self.conn.execute(
            """INSERT INTO tickets (id, number, source, external_ref, parent_ref, class, title, status,
                 created_at, updated_at, triage_verdict_json, router_decision, router_reason)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET title=excluded.title, status=excluded.status,
                 external_ref=COALESCE(excluded.external_ref, tickets.external_ref),
                 class=COALESCE(excluded.class, tickets.class), updated_at=excluded.updated_at,
                 triage_verdict_json=COALESCE(excluded.triage_verdict_json, tickets.triage_verdict_json),
                 router_decision=COALESCE(excluded.router_decision, tickets.router_decision),
                 router_reason=COALESCE(excluded.router_reason, tickets.router_reason)""",
            (
                id,
                number,
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
        _must(status in TICKET_STATUSES, f"unknown ticket status: {status}")
        self.conn.execute(
            "UPDATE tickets SET status=?, updated_at=? WHERE id=?", (status, now(), ticket_id)
        )
        self.log("ticket", status, ticket_id=ticket_id)

    def clear_router_decision(self, ticket_id: str) -> None:
        """Forget a routing decision and put the ticket back where a new one starts.

        Used when the reason for setting work aside has gone: the ticket is ordinary work again
        and goes through scoping and routing like anything else."""
        self.conn.execute(
            "UPDATE tickets SET router_decision=NULL, router_reason=NULL, status='new', "
            "updated_at=? WHERE id=?",
            (now(), ticket_id),
        )
        self.conn.execute(
            "UPDATE escalations SET resolved_at=? WHERE ticket_id=? AND resolved_at IS NULL",
            (now(), ticket_id),
        )
        self.conn.commit()

    def set_router_decision(self, ticket_id: str, decision: str, reason: str) -> None:
        _must(decision in ROUTES, f"unknown route: {decision}")
        status = {"devin": "routed", "human_only": "escalated", "refuse": "refused"}[decision]
        self.conn.execute(
            "UPDATE tickets SET router_decision=?, router_reason=?, status=?, updated_at=? WHERE id=?",
            (decision, reason, status, now(), ticket_id),
        )
        self.log(
            "route",
            {
                "devin": "given to the AI",
                "human_only": "handed to your team",
                "refuse": "set aside for now",
            }[decision],
            ticket_id=ticket_id,
            detail=reason,
        )
        # Only work that is actually waiting on somebody raises an escalation. Setting a ticket
        # aside because a file is busy asks nothing of anyone, so it does not belong in the queue
        # of things a person has to deal with.
        if decision == "human_only":
            self.insert_escalation(ticket_id, None, "human_only", reason)

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
            "dispatch",
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
        # this convenience has to come first: checked before converting, the caller-facing name
        # is never in the allow-list and the conversion below could never run
        if "structured_output" in fields:
            fields["structured_output_json"] = json.dumps(fields.pop("structured_output"))
        bad = set(fields) - allowed
        _must(not bad, f"unknown session fields: {bad}")
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
        spent += self._one("SELECT COALESCE(SUM(acus_consumed), 0) AS n FROM triage_sessions")["n"]
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

    # ------------------------------------------------------------------ triage sessions
    def insert_triage_session(
        self,
        *,
        ticket_id: str,
        devin_session_id: str,
        url: str,
        status: str,
        status_detail: str | None,
        playbook_id: str | None,
        tags: list[str],
    ) -> str:
        tid = f"tri_{uuid.uuid4().hex[:8]}"
        self.conn.execute(
            "INSERT INTO triage_sessions (id, ticket_id, devin_session_id, url, playbook_id, tags_json, "
            "created_at, status, status_detail) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                tid,
                ticket_id,
                devin_session_id,
                url,
                playbook_id,
                json.dumps(tags),
                now(),
                status,
                status_detail,
            ),
        )
        self.conn.commit()
        return tid

    TRIAGE_FIELDS: ClassVar[set[str]] = {
        "devin_session_id",
        "url",
        "playbook_id",
        "tags_json",
        "terminal_at",
        "status",
        "status_detail",
        "acus_consumed",
        "verdict_json",
        "outcome",
        "cost_usd",
    }

    def update_triage_session(self, tid: str, **fields: Any) -> None:
        if "verdict" in fields:
            fields["verdict_json"] = json.dumps(fields.pop("verdict"))
        _must(
            not (set(fields) - self.TRIAGE_FIELDS),
            f"unknown fields: {set(fields) - self.TRIAGE_FIELDS}",
        )
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE triage_sessions SET {cols} WHERE id=?", (*fields.values(), tid))
        self.conn.commit()

    def insert_scan_session(
        self,
        *,
        devin_session_id: str,
        url: str,
        status: str,
        status_detail: str | None,
        playbook_id: str | None,
        tags: list[str],
    ) -> str:
        """A scan session belongs to no ticket: it is what produces them."""
        sid = f"scn_{uuid.uuid4().hex[:8]}"
        self.conn.execute(
            "INSERT INTO scan_sessions (id, devin_session_id, url, playbook_id, tags_json, "
            "created_at, status, status_detail) VALUES (?,?,?,?,?,?,?,?)",
            (
                sid,
                devin_session_id,
                url,
                playbook_id,
                json.dumps(tags),
                now(),
                status,
                status_detail,
            ),
        )
        self.conn.commit()
        return sid

    SCAN_FIELDS: ClassVar[set[str]] = {
        "devin_session_id",
        "url",
        "playbook_id",
        "tags_json",
        "terminal_at",
        "status",
        "status_detail",
        "acus_consumed",
        "findings_json",
        "outcome",
        "cost_usd",
    }

    # ------------------------------------------------------------------ session insights
    # The whole payload is kept, not a chosen few columns: Devin adds fields to this endpoint
    # and a page that reads the payload picks them up without a migration.
    def put_insight(self, devin_session_id: str, payload: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO insights (devin_session_id, fetched_at, payload_json) VALUES (?,?,?) "
            "ON CONFLICT(devin_session_id) DO UPDATE SET fetched_at=excluded.fetched_at, "
            "payload_json=excluded.payload_json",
            (devin_session_id, now(), json.dumps(payload)),
        )
        self.conn.commit()

    def insights(self) -> list[dict[str, Any]]:
        """Every stored insight, newest session first."""
        out = []
        for r in self._all("SELECT * FROM insights"):
            d = json.loads(r["payload_json"])
            d["_fetched_at"] = r["fetched_at"]
            out.append(d)
        return sorted(out, key=lambda d: d.get("created_at") or 0, reverse=True)

    def update_scan_session(self, sid: str, **fields: Any) -> None:
        if "findings" in fields:
            fields["findings_json"] = json.dumps(fields.pop("findings"))
        _must(
            not (set(fields) - self.SCAN_FIELDS),
            f"unknown fields: {set(fields) - self.SCAN_FIELDS}",
        )
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE scan_sessions SET {cols} WHERE id=?", (*fields.values(), sid))
        self.conn.commit()

    def list_scan_sessions(self) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM scan_sessions ORDER BY created_at DESC, rowid DESC")

    def set_session_cost(self, devin_session_id: str, usd: float) -> str | None:
        """The console's dollar figure for one session, entered by a person. Matches any session
        this store started, by its Devin id or an unambiguous prefix. Returns the table updated.

        A scan is a session and is billed like one, so it is searched here too; leaving it out
        meant the console's figure for a scan was quietly discarded."""
        for table in ("sessions", "triage_sessions", "scan_sessions"):
            rows = self._all(
                f"SELECT id, devin_session_id FROM {table} WHERE devin_session_id LIKE ?",
                devin_session_id + "%",
            )
            if len(rows) == 1:
                self.conn.execute(f"UPDATE {table} SET cost_usd=? WHERE id=?", (usd, rows[0]["id"]))
                # A console figure is true at the moment it is read. A session that goes on
                # working costs more afterwards, so the page says when these were last taken.
                self.set_setting("cost.console_read_at", now())
                self.conn.commit()
                self.log(
                    "budget",
                    f"console cost entered: ${usd:.2f}",
                    session_id=rows[0]["id"] if table == "sessions" else None,
                    detail=rows[0]["devin_session_id"],
                )
                return table
        return None

    def triage_session_by_devin_id(self, devin_session_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM triage_sessions WHERE devin_session_id=?", devin_session_id)

    def get_triage_session(self, tid: str) -> dict[str, Any] | None:
        r = self._one("SELECT * FROM triage_sessions WHERE id=?", tid)
        if r and r.get("verdict_json"):
            r["verdict"] = json.loads(r["verdict_json"])
        return r

    def list_triage_sessions(self, ticket_id: str | None = None) -> list[dict[str, Any]]:
        if ticket_id:
            return self._all(
                "SELECT * FROM triage_sessions WHERE ticket_id=? ORDER BY created_at DESC, rowid DESC",
                ticket_id,
            )
        return self._all("SELECT * FROM triage_sessions ORDER BY created_at DESC, rowid DESC")

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
            "gate",
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
        _must(
            gate_result in ("pass", "fail", "missing_evidence"),
            f"unknown check result: {gate_result}",
        )
        _must(decision in ("pass", "retry", "escalate"), f"unknown decision: {decision}")
        vid = new_id("vrd")
        self.conn.execute(
            "INSERT INTO verdicts VALUES (?,?,?,?,?,?,?,?)",
            (vid, session_id, gate_result, review_severity, decision, reason, tree_hash, now()),
        )
        self.log("gate", f"{gate_result} -> {decision}", session_id=session_id, detail=reason)
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
        _must(kind in ESCALATION_KINDS, f"unknown escalation: {kind}")
        # One thing waiting on a person is one row. Two parts of the loop noticing the same
        # thing is not two things to deal with, and a queue that counts it twice is a queue
        # nobody trusts.
        same = self._one(
            "SELECT id FROM escalations WHERE ticket_id=? AND kind=? AND resolved_at IS NULL",
            ticket_id,
            kind,
        )
        if same:
            return str(same["id"])
        eid = new_id("esc")
        self.conn.execute(
            "INSERT INTO escalations VALUES (?,?,?,?,?,?,?)",
            (eid, ticket_id, session_id, kind, reason, now(), None),
        )
        # the log is read by people, so it says what happened rather than the internal name
        said = {
            "human_only": "handed to your team",
            "router_refused": "put behind another change",
            "oracle_touched": "a test changed, so someone has to look",
            "review_blocked": "the review did not finish",
            "waiting_for_user": "the AI asked a question",
            "usage_limit": "too big for one run",
            "detector_still_fires": "the problem is still there",
        }.get(kind, kind.replace("_", " "))
        self.log("escalate", said, ticket_id=ticket_id, session_id=session_id, detail=reason)
        return eid

    def close_escalations(self, ticket_id: str, kinds: tuple[str, ...], note: str) -> int:
        """Close what a ticket was waiting on, because it is no longer waiting on it.

        An escalation describes a moment. When the thing it describes has passed, a change that
        failed and then passed, work that was merged, the row has to go with it, or the board
        keeps asking for something nobody needs to do."""
        rows = self._all(
            "SELECT id FROM escalations WHERE ticket_id=? AND resolved_at IS NULL", ticket_id
        )
        n = 0
        for r in rows:
            e = self._one("SELECT kind FROM escalations WHERE id=?", r["id"])
            if kinds and e["kind"] not in kinds:
                continue
            self.resolve_escalation(r["id"], note, by_hand=False)
            n += 1
        return n

    def resolve_escalation(
        self, eid: str, note: str | None = None, *, by_hand: bool = True
    ) -> dict[str, Any] | None:
        """Close one thing a person was waiting on. by_hand=False when the loop closed it
        itself, because the log should not credit a person with something nobody did."""
        e = self._one("SELECT * FROM escalations WHERE id=?", eid)
        if not e:
            return None
        self.conn.execute("UPDATE escalations SET resolved_at=? WHERE id=?", (now(), eid))
        self.conn.commit()
        self.log(
            "merge",
            "dismissed by a person" if by_hand else "no longer waiting on anyone",
            ticket_id=e["ticket_id"],
            session_id=e["session_id"],
            detail=(note or "")[:200],
        )
        return e

    def list_escalations(self, unresolved_only: bool = True) -> list[dict[str, Any]]:
        q = (
            "SELECT * FROM escalations"
            + (" WHERE resolved_at IS NULL" if unresolved_only else "")
            + " ORDER BY created_at"
        )
        return self._all(q)

    def record_human_action(self, ticket_id: str, kind: str, actor: str) -> str:
        _must(
            kind in ("merge", "review_comment", "approve", "reject", "resolve"),
            f"unknown human action: {kind}",
        )
        hid = new_id("hum")
        self.conn.execute(
            "INSERT INTO human_actions VALUES (?,?,?,?,?)",
            (hid, ticket_id, kind, now(), digest(actor)[:16]),
        )
        self.log("merge", f"human {kind}", ticket_id=ticket_id)
        return hid

    def ticket_dump(self, ticket_id: str) -> dict[str, list[dict[str, Any]]]:
        """Every row about one ticket, for a snapshot before the rows are forgotten."""
        wos = self.work_orders_for(ticket_id)
        sess = [s for w in wos for s in self.sessions_for(w["id"])]
        sids = [s["id"] for s in sess]
        tri = self.list_triage_sessions(ticket_id)
        tsids = [t["id"] for t in tri]
        q = lambda sql, ids: (
            self._all(sql.replace("?", ",".join("?" for _ in ids)), *ids) if ids else []
        )
        return {
            "tickets": [t for t in [self.get_ticket(ticket_id)] if t],
            "work_orders": wos,
            "sessions": sess,
            "triage_sessions": tri,
            "evidence": q("SELECT * FROM evidence WHERE session_id IN (?)", sids),
            "verdicts": q("SELECT * FROM verdicts WHERE session_id IN (?)", sids),
            "escalations": self._all("SELECT * FROM escalations WHERE ticket_id=?", ticket_id),
            "human_actions": self._all("SELECT * FROM human_actions WHERE ticket_id=?", ticket_id),
            "events": self._all("SELECT * FROM events WHERE ticket_id=?", ticket_id),
            "timeline": self._all("SELECT * FROM timeline WHERE ticket_id=?", ticket_id)
            + q(
                "SELECT * FROM timeline WHERE ticket_id IS NULL AND session_id IN (?)", sids + tsids
            ),
        }

    def forget_ticket(self, ticket_id: str) -> int:
        """Delete every row about one ticket. Returns the number of rows removed. Used by a
        shard reset; the caller snapshots first."""
        d = self.ticket_dump(ticket_id)
        n = sum(len(v) for v in d.values())
        sids = [s["id"] for s in d["sessions"]]
        tsids = [t["id"] for t in d["triage_sessions"]]
        with self.tx() as c:

            def rm(sql: str, ids: list[str]) -> None:
                if ids:
                    c.execute(sql.replace("?", ",".join("?" for _ in ids)), ids)

            rm("DELETE FROM timeline WHERE session_id IN (?)", sids + tsids)
            c.execute("DELETE FROM timeline WHERE ticket_id=?", (ticket_id,))
            rm("DELETE FROM evidence WHERE session_id IN (?)", sids)
            rm("DELETE FROM verdicts WHERE session_id IN (?)", sids)
            rm("DELETE FROM sessions WHERE id IN (?)", sids)
            c.execute("DELETE FROM triage_sessions WHERE ticket_id=?", (ticket_id,))
            c.execute("DELETE FROM work_orders WHERE ticket_id=?", (ticket_id,))
            c.execute("DELETE FROM escalations WHERE ticket_id=?", (ticket_id,))
            c.execute("DELETE FROM human_actions WHERE ticket_id=?", (ticket_id,))
            c.execute("DELETE FROM events WHERE ticket_id=?", (ticket_id,))
            c.execute("DELETE FROM tickets WHERE id=?", (ticket_id,))
        return n

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self._one("SELECT value FROM settings WHERE key=?", key)
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, now()),
        )

    # ------------------------------------------------------------------ automations (configs)
    def upsert_automation(self, **a: Any) -> str:
        aid = a.get("id") or new_id("auto")
        row = {
            "id": aid,
            "name": a["name"],
            "kind": a.get("kind", "custom"),
            "enabled": 1 if a.get("enabled", True) else 0,
            "availability": a.get("availability", "live"),
            "trigger_json": json.dumps(a.get("trigger") or {}, sort_keys=True),
            "target": a.get("target", ""),
            "playbook": a.get("playbook"),
            "max_acu": a.get("max_acu"),
            "concurrency": int(a.get("concurrency") or 4),
            "max_findings": a.get("max_findings"),
            "devin_automation_id": a.get("devin_automation_id"),
            "schedule": a.get("schedule"),
            "notes": a.get("notes"),
            "created_at": now(),
            "last_run": a.get("last_run"),
            "last_result": a.get("last_result"),
        }
        cols = ", ".join(row)
        qs = ", ".join("?" for _ in row)
        updates = ", ".join(f"{k}=excluded.{k}" for k in row if k not in ("id", "created_at"))
        self.conn.execute(
            f"INSERT INTO automations ({cols}) VALUES ({qs}) ON CONFLICT(id) DO UPDATE SET {updates}",
            tuple(row.values()),
        )
        return aid

    def list_automations(self) -> list[dict[str, Any]]:
        out = []
        for r in self._all("SELECT * FROM automations ORDER BY created_at, rowid"):
            r["trigger"] = json.loads(r.pop("trigger_json") or "{}")
            r["last_result"] = json.loads(r["last_result"]) if r.get("last_result") else None
            out.append(r)
        return out

    def get_automation(self, aid: str) -> dict[str, Any] | None:
        rows = [a for a in self.list_automations() if a["id"] == aid]
        return rows[0] if rows else None

    def set_automation(self, aid: str, **fields: Any) -> None:
        allowed = {
            "enabled",
            "last_run",
            "last_result",
            "notes",
            "max_acu",
            "max_findings",
            "concurrency",
            "schedule",
            "playbook",
            "devin_automation_id",
        }
        bad = set(fields) - allowed
        _must(not bad, f"unknown automation fields: {bad}")
        if "last_result" in fields and not isinstance(fields["last_result"], str | type(None)):
            fields["last_result"] = json.dumps(fields["last_result"])
        sets = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE automations SET {sets} WHERE id=?", (*fields.values(), aid))

    def start_automation_run(self, aid: str) -> str:
        rid = new_id("run")
        self.conn.execute(
            "INSERT INTO automation_runs (id, automation_id, started_at, status) VALUES (?,?,?,?)",
            (rid, aid, now(), "running"),
        )
        return rid

    def finish_automation_run(self, rid: str, result: dict[str, Any], status: str = "done") -> None:
        self.conn.execute(
            "UPDATE automation_runs SET finished_at=?, status=?, result_json=? WHERE id=?",
            (now(), status, json.dumps(result), rid),
        )

    def list_automation_runs(self, aid: str, limit: int = 10) -> list[dict[str, Any]]:
        out = []
        for r in self._all(
            "SELECT * FROM automation_runs WHERE automation_id=? ORDER BY started_at DESC LIMIT ?",
            aid,
            limit,
        ):
            r["result"] = json.loads(r.pop("result_json") or "{}")
            out.append(r)
        return out

    def delete_automation(self, aid: str) -> None:
        self.conn.execute("DELETE FROM automations WHERE id=? AND kind='custom'", (aid,))

    # ------------------------------------------------------------------ playbooks
    def upsert_playbook(self, **p: Any) -> str:
        pid = p.get("id") or new_id("pb")
        ts = now()
        self.conn.execute(
            """INSERT INTO playbooks (id, name, agent, body, schema_json, max_acu, source, availability, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET name=excluded.name, agent=excluded.agent, body=excluded.body,
                 schema_json=excluded.schema_json, max_acu=excluded.max_acu, availability=excluded.availability,
                 updated_at=excluded.updated_at""",
            (
                pid,
                p["name"],
                p.get("agent", "custom"),
                p["body"],
                json.dumps(p["schema"]) if p.get("schema") else None,
                p.get("max_acu"),
                p.get("source", "user"),
                p.get("availability", "live"),
                ts,
                ts,
            ),
        )
        return pid

    def list_playbooks(self) -> list[dict[str, Any]]:
        out = []
        for r in self._all("SELECT * FROM playbooks ORDER BY created_at, rowid"):
            r["schema"] = json.loads(r.pop("schema_json")) if r.get("schema_json") else None
            out.append(r)
        return out

    def get_playbook(self, pid: str) -> dict[str, Any] | None:
        rows = [p for p in self.list_playbooks() if p["id"] == pid]
        return rows[0] if rows else None

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
def load_tickets(
    store: Store, tickets_json: Path | str, source: str = "inventory", *, triaged: bool = True
) -> list[str]:
    """Seed tickets from data/inventory/<date>/tickets.json (the drafts + issue numbers).

    triaged=True writes the inventory's verdict and work orders (replay). triaged=False writes the
    tickets as `new` with no verdict and no work order: the triage session decides (live)."""
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
        if not triaged:
            store.upsert_ticket(
                id=tid,
                source=source,
                title=sh["title"],
                status="new",
                external_ref=ext,
                parent_ref=parent_ref,
                cls=",".join(classes),
            )
            ids.append(tid)
            continue
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
