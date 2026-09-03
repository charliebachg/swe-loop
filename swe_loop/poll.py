"""The poller and the manage verbs. Devin has no outbound webhook, so we poll.

Backoff 5 s to 30 s, a wall-clock timeout, and the terminal rule from `SessionState`. On a
terminal session the poller records what the session claimed, then hands the row to the gate.
The verbs the brief calls "manage":

- waiting_for_user: answered once from the work order if it is the kind of question the work
  order answers; a second time it goes to a person.
- waiting_for_approval: a person, always. We never assume we can clear it.
- usage_limit_exceeded: "too large". Escalated, never retried blind.
- terminal with no structured output: a failure, never a pass.
- wall clock exceeded: terminate with archive=true, escalate.
- budget cap reached: terminate every live session with archive=true, escalate.
- gate fail: the exact failure text into the same session, at most twice (called by the gate).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft7Validator

from swe_loop.config import TargetConfig
from swe_loop.devin import DevinClient, DevinError, SessionState
from swe_loop.dispatch import load_result_schema
from swe_loop.store import Store, now

MAX_RETRIES = 2


@dataclass(frozen=True)
class Outcome:
    session_id: str
    kind: str  # running | finished | failed_no_output | too_large | needs_human | timeout | error
    detail: str = ""


def _work_order_answer(wo: dict[str, Any], ticket: dict[str, Any]) -> str:
    acc = "\n".join(f"- {k}: `{v}`" for k, v in wo["acceptance"].items())
    return (
        "Proceed with the ticket as specified. The files in scope are: "
        + ", ".join(wo["files"])
        + ". The acceptance commands are:\n"
        + acc
        + "\nDo not modify tests, CI configuration, migrations, or dependency files. "
        "If you are blocked on something the ticket does not answer, provide structured output "
        "with is_final=true, self_reported_done=false, and the blocker in needs_human."
    )


class Poller:
    def __init__(
        self,
        store: Store,
        client: DevinClient,
        cfg: TargetConfig,
        *,
        min_wait: float = 5.0,
        max_wait: float = 30.0,
        wall_clock: float = 3600.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.store, self.client, self.cfg = store, client, cfg
        self.min_wait, self.max_wait, self.wall_clock = min_wait, max_wait, wall_clock
        self.sleep, self.clock = sleep, clock
        self._validator = Draft7Validator(load_result_schema())

    # ------------------------------------------------------------------ one observation
    def poll_once(self, sid: str) -> Outcome:
        row = self.store.get_session(sid)
        if not row or not row["devin_session_id"]:
            return Outcome(sid, "error", "no devin session bound")
        state = self.client.status(row["devin_session_id"])
        self.store.update_session(
            sid,
            status=state.status,
            status_detail=state.status_detail,
            acus_consumed=state.acus_consumed,
        )
        wo = self.store.get_work_order(row["work_order_id"])
        ticket = self.store.get_ticket(wo["ticket_id"])

        if not state.terminal:
            if ticket["status"] == "dispatched":
                self.store.set_ticket_status(ticket["id"], "running")
            return Outcome(sid, "running", state.status_detail or state.status)

        if state.needs_attention:
            return self._attention(sid, row, state, wo, ticket)

        self.store.update_session(sid, terminal_at=now())
        self._refresh_insights(sid, row["devin_session_id"])

        if state.too_large:
            self.store.insert_escalation(
                ticket["id"],
                sid,
                "usage_limit",
                f"usage_limit_exceeded at {state.acus_consumed} ACU: the shard is too large; re-shard, do not retry",
            )
            self.store.set_ticket_status(ticket["id"], "escalated")
            return Outcome(sid, "too_large", f"{state.acus_consumed} ACU")

        if not state.succeeded:
            self.store.insert_escalation(
                ticket["id"],
                sid,
                "review_blocked",
                f"session ended {state.status}/{state.status_detail} without finishing",
            )
            self.store.set_ticket_status(ticket["id"], "escalated")
            return Outcome(sid, "error", f"{state.status}/{state.status_detail}")

        out = state.structured_output
        if not out:
            self.store.update_session(sid, self_reported_done=0)
            self.store.insert_verdict(
                session_id=sid,
                gate_result="missing_evidence",
                decision="escalate",
                reason="terminal session provided no structured output; a claim that was never made cannot pass",
            )
            self.store.insert_escalation(
                ticket["id"], sid, "review_blocked", "finished with no structured output"
            )
            self.store.set_ticket_status(ticket["id"], "escalated")
            return Outcome(sid, "failed_no_output")

        problems = [e.message for e in self._validator.iter_errors(out)]
        pr = out.get("pr_url") or (state.pull_requests[0] if state.pull_requests else None)
        self.store.update_session(
            sid,
            structured_output_json=json.dumps(out),
            self_reported_done=1 if out.get("self_reported_done") else 0,
            pull_request_url=pr,
        )
        self.store.set_ticket_status(ticket["id"], "gated")
        detail = "schema ok" if not problems else "schema problems: " + "; ".join(problems)[:300]
        return Outcome(sid, "finished", detail)

    def _attention(
        self,
        sid: str,
        row: dict[str, Any],
        state: SessionState,
        wo: dict[str, Any],
        ticket: dict[str, Any],
    ) -> Outcome:
        if state.status_detail == "waiting_for_approval":
            self.store.insert_escalation(
                ticket["id"],
                sid,
                "waiting_for_user",
                "session is waiting for approval; a person must act",
            )
            self.store.set_ticket_status(ticket["id"], "escalated")
            return Outcome(sid, "needs_human", "waiting_for_approval")
        prior = self.store.conn.execute(
            "SELECT COUNT(*) FROM escalations WHERE session_id=? AND kind='waiting_for_user'",
            (sid,),
        ).fetchone()[0]
        if prior == 0:
            self.client.message(row["devin_session_id"], _work_order_answer(wo, ticket))
            eid = self.store.insert_escalation(
                ticket["id"], sid, "waiting_for_user", "auto-answered from the work order"
            )
            self.store.conn.execute("UPDATE escalations SET resolved_at=? WHERE id=?", (now(), eid))
            return Outcome(sid, "running", "answered from the work order")
        self.store.insert_escalation(
            ticket["id"],
            sid,
            "waiting_for_user",
            "asked again after the work-order answer; a person must act",
        )
        self.store.set_ticket_status(ticket["id"], "escalated")
        return Outcome(sid, "needs_human", "waiting_for_user twice")

    def _refresh_insights(self, sid: str, devin_id: str) -> None:
        try:
            ins = self.client.insights([devin_id]).get(devin_id)
        except (DevinError, OSError):  # insights are best-effort telemetry
            ins = None
        if ins:
            fields: dict[str, Any] = {}
            if ins.get("session_size"):
                fields["session_size"] = ins["session_size"]
            if ins.get("acus_consumed") is not None:
                fields["acus_consumed"] = ins["acus_consumed"]
            if fields:
                self.store.update_session(sid, **fields)

    # ------------------------------------------------------------------ waiting
    def wait(self, sid: str) -> Outcome:
        start = self.clock()
        delay = self.min_wait
        while True:
            out = self.poll_once(sid)
            if out.kind != "running":
                return out
            if self.clock() - start > self.wall_clock:
                return self.timeout(sid)
            self.sleep(delay)
            delay = min(delay * 2, self.max_wait)

    def timeout(self, sid: str) -> Outcome:
        row = self.store.get_session(sid)
        self.client.terminate(row["devin_session_id"])
        wo = self.store.get_work_order(row["work_order_id"])
        self.store.update_session(sid, status="exit", status_detail="terminated", terminal_at=now())
        self.store.insert_escalation(
            wo["ticket_id"],
            sid,
            "review_blocked",
            f"wall clock of {self.wall_clock:.0f}s exceeded; terminated and archived",
        )
        self.store.set_ticket_status(wo["ticket_id"], "escalated")
        return Outcome(sid, "timeout")

    # ------------------------------------------------------------------ the other verbs
    def retry_with_failure(self, sid: str, failure_text: str) -> bool:
        """Gate fail: the exact failure text into the same session. At most MAX_RETRIES times."""
        row = self.store.get_session(sid)
        if row["attempt"] > MAX_RETRIES:
            return False
        self.client.message(
            row["devin_session_id"],
            "The verification gate ran the acceptance commands on a clean checkout of your branch and they "
            "did not pass. Exact output follows. Fix it on the same branch, push, and provide structured "
            "output again with is_final=true.\n\n" + failure_text[:6000],
        )
        self.store.conn.execute(
            "UPDATE sessions SET attempt=attempt+1, terminal_at=NULL, status='running', status_detail='working' WHERE id=?",
            (sid,),
        )
        wo = self.store.get_work_order(row["work_order_id"])
        self.store.set_ticket_status(wo["ticket_id"], "running")
        return True

    def enforce_budget(self) -> list[str]:
        """Terminate every live session when spend reaches the cap. Returns the ids terminated."""
        b = self.store.conn.execute("SELECT acu_cap FROM budget WHERE id=1").fetchone()
        if not b:
            return []
        spent = self.store.conn.execute(
            "SELECT COALESCE(SUM(acus_consumed),0) FROM sessions"
        ).fetchone()[0]
        if spent < b[0]:
            return []
        stopped = []
        for s in self.store._all(
            "SELECT * FROM sessions WHERE terminal_at IS NULL AND devin_session_id IS NOT NULL"
        ):
            self.client.terminate(s["devin_session_id"])
            self.store.update_session(
                s["id"], status="exit", status_detail="terminated", terminal_at=now()
            )
            wo = self.store.get_work_order(s["work_order_id"])
            self.store.insert_escalation(
                wo["ticket_id"],
                s["id"],
                "budget",
                f"ACU cap {b[0]} reached at {spent}; terminated and archived",
            )
            self.store.set_ticket_status(wo["ticket_id"], "escalated")
            stopped.append(s["id"])
        return stopped

    def reconcile_reserved(self) -> list[str]:
        """Rows reserved before a crash and never bound: adopt the org's live session by tags, or orphan."""
        fixed = []
        for s in self.store._all(
            "SELECT * FROM sessions WHERE devin_session_id IS NULL AND status='reserved'"
        ):
            tags = json.loads(s["tags_json"] or "[]")
            live = (
                self.client.find_live([t for t in tags if t.startswith("shard:") or t == tags[0]])
                if tags
                else None
            )
            if live:
                self.store.bind_devin_session(
                    s["id"], devin_session_id=live.session_id, url=live.url, status=live.status
                )
            else:
                self.store.update_session(s["id"], status="orphaned", terminal_at=now())
            fixed.append(s["id"])
        return fixed
