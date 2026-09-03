"""The poller and the manage verbs. Devin has no outbound webhook, so we poll.

Backoff 5 s to 30 s, a wall-clock timeout, and the terminal rule from `SessionState`. On a
terminal session the poller records what the session claimed, then hands the row to the gate.
A row already marked terminal is never processed twice. The verbs the brief calls "manage":

- waiting_for_user: the poller cannot read the question, so it answers once with a restatement
  of the work order (scope, acceptance commands, the seam's forbidden paths, how to report a
  blocker). A second wait goes to a person.
- waiting_for_approval: a person, always. We never assume we can clear it.
- usage_limit_exceeded: "too large". Escalated, never retried blind.
- terminal with no structured output: a failure, never a pass.
- wall clock exceeded: terminate with archive=true, escalate. Terminate is best-effort; the
  local row is marked terminal whether or not the API call succeeds.
- budget cap reached: terminate every live session with archive=true, escalate.
- gate fail: the exact failure text into the same session, at most MAX_RETRIES times. The
  rejected claim's digest is remembered so the same claim is not accepted again.
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
from swe_loop.dispatch import load_result_schema, reconcile
from swe_loop.store import Store, digest, now

MAX_RETRIES = 2


@dataclass(frozen=True)
class Outcome:
    session_id: str
    kind: str  # running | finished | failed_no_output | too_large | needs_human | timeout | error
    detail: str = ""


def work_order_answer(wo: dict[str, Any], cfg: TargetConfig) -> str:
    acc = "\n".join(f"- {k}: `{v}`" for k, v in wo["acceptance"].items())
    forbidden = ", ".join(cfg.forbidden_paths) or "tests and CI configuration"
    return (
        "Proceed with the ticket as specified. The files in scope are: "
        + ", ".join(wo["files"])
        + ". The acceptance commands are:\n"
        + acc
        + f"\nDo not modify anything under {forbidden}, and do not change dependency pins. "
        "If you are blocked on something the ticket does not answer, provide structured output "
        "with is_final=true, self_reported_done=false, and the blocker in needs_human."
    )


def output_digest(out: dict[str, Any] | None) -> str | None:
    return digest(json.dumps(out, sort_keys=True)) if out else None


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

    # ------------------------------------------------------------------ helpers
    def _escalate(self, ticket_id: str, sid: str | None, kind: str, reason: str) -> None:
        self.store.insert_escalation(ticket_id, sid, kind, reason)
        self.store.set_ticket_status(ticket_id, "escalated")

    def _terminate(self, sid: str, kind: str, reason: str) -> None:
        """Best-effort terminate on the org; the local row is marked terminal regardless."""
        row = self.store.get_session(sid)
        note = ""
        if row["devin_session_id"]:
            try:
                self.client.terminate(row["devin_session_id"])
            except DevinError as ex:
                note = f" (terminate call failed: {ex.status} {ex.detail[:80]})"
        self.store.mark_terminal(sid, status="exit", status_detail="terminated")
        self.store.log(
            "L4 manage", "terminated (archive=true)", session_id=sid, detail=reason + note
        )
        wo = self.store.get_work_order(row["work_order_id"])
        self._escalate(wo["ticket_id"], sid, kind, reason + note)

    def _refresh_insights(self, sid: str, devin_id: str) -> None:
        try:
            ins = self.client.insights([devin_id]).get(devin_id)
        except DevinError:  # telemetry is best-effort; the claim is already recorded
            return
        if not ins:
            return
        fields: dict[str, Any] = {}
        if ins.get("session_size"):
            fields["session_size"] = ins["session_size"]
        if ins.get("acus_consumed") is not None:
            fields["acus_consumed"] = ins["acus_consumed"]
        if fields:
            self.store.update_session(sid, **fields)

    # ------------------------------------------------------------------ one observation
    def poll_once(self, sid: str) -> Outcome:
        row = self.store.get_session(sid)
        if not row:
            return Outcome(sid, "error", "unknown session")
        if row["terminal_at"]:
            return Outcome(sid, "already_terminal", f"{row['status']}/{row['status_detail']}")
        if not row["devin_session_id"]:
            status = reconcile(self.store, self.client, sid, self.cfg)
            if status != "bound":
                wo = self.store.get_work_order(row["work_order_id"])
                self._escalate(
                    wo["ticket_id"],
                    sid,
                    "review_blocked",
                    "reserved row could not be reconciled to a session on the org",
                )
                return Outcome(sid, "error", "orphaned")
            row = self.store.get_session(sid)

        state = self.client.status(row["devin_session_id"])
        wo = self.store.get_work_order(row["work_order_id"])
        ticket = self.store.get_ticket(wo["ticket_id"])
        self.store.log(
            "L4 poll",
            f"{state.status}/{state.status_detail or '-'}",
            ticket_id=ticket["id"],
            session_id=sid,
            detail=f"acus={state.acus_consumed}",
        )

        if not state.terminal:
            self.store.update_session(
                sid,
                status=state.status,
                status_detail=state.status_detail,
                acus_consumed=state.acus_consumed,
            )
            if ticket["status"] == "dispatched":
                self.store.set_ticket_status(ticket["id"], "running")
            return Outcome(sid, "running", state.status_detail or state.status)

        if state.needs_attention and not state.delivered:
            self.store.update_session(sid, status=state.status, status_detail=state.status_detail)
            return self._attention(sid, row, state, wo, ticket)

        out = state.structured_output
        if (
            state.succeeded
            and out
            and row["rejected_output_digest"]
            and output_digest(out) == row["rejected_output_digest"]
        ):
            # the session has not produced a new claim since the gate rejected this one
            self.store.update_session(sid, status="running", status_detail="working")
            return Outcome(sid, "running", "terminal state still carries the rejected claim")

        self.store.mark_terminal(
            sid,
            status=state.status,
            status_detail=state.status_detail,
            acus_consumed=state.acus_consumed,
        )
        self._refresh_insights(sid, row["devin_session_id"])

        if state.too_large:
            self._escalate(
                ticket["id"],
                sid,
                "usage_limit",
                f"usage_limit_exceeded at {state.acus_consumed} ACU: the shard is too large; re-shard, do not retry",
            )
            return Outcome(sid, "too_large", f"{state.acus_consumed} ACU")

        if not state.succeeded and not state.delivered:
            self._escalate(
                ticket["id"],
                sid,
                "review_blocked",
                f"session ended {state.status}/{state.status_detail} without finishing",
            )
            return Outcome(sid, "error", f"{state.status}/{state.status_detail}")

        if not out:
            self.store.update_session(sid, self_reported_done=0)
            self.store.insert_verdict(
                session_id=sid,
                gate_result="missing_evidence",
                decision="escalate",
                reason="terminal session provided no structured output; a claim that was never made cannot pass",
            )
            self._escalate(
                ticket["id"], sid, "review_blocked", "finished with no structured output"
            )
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
            self._escalate(
                ticket["id"],
                sid,
                "waiting_for_user",
                "session is waiting for approval; a person must act",
            )
            return Outcome(sid, "needs_human", "waiting_for_approval")
        prior = self.store.conn.execute(
            "SELECT COUNT(*) FROM escalations WHERE session_id=? AND kind='waiting_for_user'",
            (sid,),
        ).fetchone()[0]
        if prior == 0:
            self.client.message(row["devin_session_id"], work_order_answer(wo, self.cfg))
            self.store.log(
                "L4 manage",
                "answered waiting_for_user from the work order",
                ticket_id=ticket["id"],
                session_id=sid,
            )
            eid = self.store.insert_escalation(
                ticket["id"],
                sid,
                "waiting_for_user",
                "answered once with the work-order restatement",
            )
            self.store.conn.execute("UPDATE escalations SET resolved_at=? WHERE id=?", (now(), eid))
            return Outcome(sid, "running", "answered from the work order")
        self._escalate(
            ticket["id"],
            sid,
            "waiting_for_user",
            "asked again after the work-order answer; a person must act",
        )
        return Outcome(sid, "needs_human", "waiting_for_user twice")

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
        self._terminate(
            sid,
            "review_blocked",
            f"wall clock of {self.wall_clock:.0f}s exceeded; terminated and archived",
        )
        return Outcome(sid, "timeout")

    # ------------------------------------------------------------------ the other verbs
    def retry_with_failure(self, sid: str, failure_text: str) -> bool:
        """Gate fail: the exact failure text into the same session. At most MAX_RETRIES times."""
        row = self.store.get_session(sid)
        if row["retries"] >= MAX_RETRIES:
            return False
        self.client.message(
            row["devin_session_id"],
            "The verification gate ran the acceptance commands on a clean checkout of your branch and they "
            "did not pass. Exact output follows. Fix it on the same branch, push, and provide structured "
            "output again with is_final=true.\n\n" + failure_text[:6000],
        )
        rejected = (
            output_digest(json.loads(row["structured_output_json"]))
            if row["structured_output_json"]
            else None
        )
        self.store.conn.execute(
            "UPDATE sessions SET retries=retries+1, terminal_at=NULL, status='running', "
            "status_detail='working', rejected_output_digest=? WHERE id=?",
            (rejected, sid),
        )
        self.store.log(
            "L4 manage",
            "retry with the exact failure text",
            session_id=sid,
            detail=failure_text[:200],
        )
        wo = self.store.get_work_order(row["work_order_id"])
        self.store.set_ticket_status(wo["ticket_id"], "running")
        return True

    def enforce_budget(self) -> list[str]:
        """Terminate every live session when spend reaches the cap. Returns the ids terminated."""
        b = self.store.budget_state()
        if b.get("cap") is None or b["spent"] < b["cap"]:
            return []
        stopped = []
        for s in self.store.live_sessions():
            self._terminate(
                s["id"],
                "budget",
                f"ACU cap {b['cap']} reached at {b['spent']}; terminated and archived",
            )
            stopped.append(s["id"])
        return stopped

    def reconcile_reserved(self) -> dict[str, str]:
        """Rows reserved before a crash and never bound: adopt by work-order tag, or orphan."""
        out = {}
        for s in self.store._all(
            "SELECT id FROM sessions WHERE devin_session_id IS NULL AND status='reserved'"
        ):
            out[s["id"]] = reconcile(self.store, self.client, s["id"], self.cfg)
        return out
