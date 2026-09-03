"""The Devin API client (v3), behind a transport so the whole pipeline runs without a key.

Two transports:
- HttpTransport talks to https://api.devin.ai/v3/organizations/{org_id}. Bearer auth with an
  org-scoped service user key. 429, 5xx and transport errors back off with jitter. Polling
  only; there is no outbound webhook from Devin.
- FakeTransport replays fixtures from data/replay/sessions/*.json, or synthesises a plausible
  session when no fixture exists. It records every call, so tests can assert what would have
  been sent. It is the default unless mode is live AND a key is present.

Verified against the org on 2026-09-03: list endpoints paginate with `first` (default 100,
max 200) and `after` (cursor; an invalid value is a 400, an unknown parameter name is silently
ignored). `tags` and `session_ids` are array parameters. Responses carry `items`, `end_cursor`,
`has_next_page`, `total`.

Two distinct predicates on a session, because they answer different questions:
- `terminal`: the poller stops waiting. True for exit/error/suspended, and for any
  `status_detail` other than "working" (waiting_for_user and waiting_for_approval included).
- `alive`: the session still exists at Devin and can be resumed or adopted. True unless the
  status is exit/error/suspended. A session parked on a question is alive.
Success is exit + finished. A terminal session with no structured output is a failure.
"""

from __future__ import annotations

import json
import random
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx

from swe_loop.config import Settings

API_BASE = "https://api.devin.ai/v3"
TERMINAL_STATUSES = {"exit", "error", "suspended"}
ATTENTION_DETAILS = {"waiting_for_user", "waiting_for_approval"}
PAGE_SIZE = 100
MAX_PAGES = 50


class DevinError(RuntimeError):
    def __init__(self, status: int, detail: str):
        super().__init__(f"Devin API {status}: {detail}")
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class SessionSpec:
    prompt: str
    tags: tuple[str, ...]
    repos: tuple[str, ...]
    max_acu_limit: int
    structured_output_schema: dict[str, Any]
    playbook_id: str | None = None
    title: str | None = None
    structured_output_required: bool = True
    devin_mode: str = "normal"

    def to_payload(self) -> dict[str, Any]:
        p: dict[str, Any] = {
            "prompt": self.prompt,
            "tags": list(self.tags),
            "repos": list(self.repos),
            "max_acu_limit": self.max_acu_limit,
            "structured_output_schema": self.structured_output_schema,
            "structured_output_required": self.structured_output_required,
            "devin_mode": self.devin_mode,
        }
        if self.playbook_id:
            p["playbook_id"] = self.playbook_id
        if self.title:
            p["title"] = self.title
        return p


@dataclass(frozen=True)
class SessionState:
    session_id: str
    url: str
    status: str
    status_detail: str | None
    acus_consumed: float | None = None
    structured_output: dict[str, Any] | None = None
    pull_requests: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES or self.status_detail not in (None, "", "working")

    @property
    def alive(self) -> bool:
        return self.status not in TERMINAL_STATUSES

    @property
    def succeeded(self) -> bool:
        return self.status == "exit" and self.status_detail == "finished"

    @property
    def needs_attention(self) -> bool:
        return self.status_detail in ATTENTION_DETAILS

    @property
    def too_large(self) -> bool:
        return self.status_detail == "usage_limit_exceeded"

    @classmethod
    def from_raw(cls, d: dict[str, Any]) -> SessionState:
        prs = d.get("pull_requests") or []
        return cls(
            session_id=d.get("session_id") or d.get("id") or "",
            url=d.get("url") or d.get("session_url") or "",
            status=d.get("status") or "",
            status_detail=d.get("status_detail"),
            acus_consumed=d.get("acus_consumed"),
            structured_output=d.get("structured_output"),
            pull_requests=tuple(p.get("url") if isinstance(p, dict) else str(p) for p in prs),
            tags=tuple(d.get("tags") or ()),
            raw=d,
        )


class Transport(Protocol):
    def create_session(self, payload: dict[str, Any]) -> dict[str, Any]: ...
    def get_session(self, session_id: str) -> dict[str, Any]: ...
    def list_sessions(self, tags: list[str] | None = None) -> list[dict[str, Any]]: ...
    def send_message(self, session_id: str, text: str) -> dict[str, Any]: ...
    def terminate(self, session_id: str, archive: bool = True) -> dict[str, Any]: ...
    def list_insights(self, session_ids: list[str] | None = None) -> list[dict[str, Any]]: ...
    def list_automations(self) -> list[dict[str, Any]]: ...
    def list_playbooks(self) -> list[dict[str, Any]]: ...
    def list_knowledge_notes(self) -> list[dict[str, Any]]: ...
    def list_secrets(self) -> list[dict[str, Any]]: ...
    def create_pr_review(self, pr_url: str) -> dict[str, Any]: ...
    def create_playbook(self, payload: dict[str, Any]) -> dict[str, Any]: ...
    def create_knowledge_note(self, payload: dict[str, Any]) -> dict[str, Any]: ...


# ---------------------------------------------------------------------------- HTTP
class HttpTransport:
    def __init__(
        self,
        api_key: str,
        org_id: str,
        *,
        client: httpx.Client | None = None,
        max_retries: int = 5,
        sleep=time.sleep,
    ):
        if not api_key or not org_id:
            raise ValueError("HttpTransport needs an api key and an org id")
        self.base = f"{API_BASE}/organizations/{org_id}"
        self.client = client or httpx.Client(timeout=30)
        self.headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        self.max_retries = max_retries
        self.sleep = sleep

    def _req(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        url = f"{self.base}{path}"
        delay = 1.0
        last = "no attempt made"
        for attempt in range(self.max_retries + 1):
            try:
                r = self.client.request(method, url, headers=self.headers, **kw)
            except httpx.HTTPError as ex:  # timeouts and connection errors: retry, then DevinError
                last = f"{type(ex).__name__}: {ex}"
                if attempt == self.max_retries:
                    raise DevinError(0, last) from ex
                self.sleep(min(delay + random.uniform(0, delay), 60))
                delay = min(delay * 2, 30)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                last = f"{r.status_code}: {r.text[:200]}"
                if attempt == self.max_retries:
                    raise DevinError(r.status_code, r.text[:200])
                retry_after = r.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else delay + random.uniform(0, delay)
                self.sleep(min(wait, 60))
                delay = min(delay * 2, 30)
                continue
            if r.status_code >= 400:
                raise DevinError(r.status_code, r.text[:300])
            if not r.content:
                return {}
            return r.json()
        raise DevinError(0, last)

    def _paged(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Cursor pagination with `first`/`after`. A cursor that stops advancing ends the loop."""
        items: list[dict[str, Any]] = []
        after: str | None = None
        seen: set[str] = set()
        for _ in range(MAX_PAGES):
            q: dict[str, Any] = {"first": PAGE_SIZE, **params}
            if after:
                q["after"] = after
            page = self._req("GET", path, params=q)
            items += page.get("items", [])
            nxt = page.get("end_cursor")
            if not page.get("has_next_page") or not nxt or nxt in seen:
                break
            seen.add(nxt)
            after = nxt
        return items

    def create_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._req("POST", "/sessions", json=payload)

    def get_session(self, session_id: str) -> dict[str, Any]:
        return self._req("GET", f"/sessions/{session_id}")

    def list_sessions(self, tags: list[str] | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"tags": list(tags)} if tags else {}
        items = self._paged("/sessions", params)
        if tags:  # confirm client-side regardless of the server's any/all semantics
            want = set(tags)
            items = [i for i in items if want <= set(i.get("tags") or [])]
        return items

    def send_message(self, session_id: str, text: str) -> dict[str, Any]:
        return self._req("POST", f"/sessions/{session_id}/messages", json={"message": text})

    def terminate(self, session_id: str, archive: bool = True) -> dict[str, Any]:
        return self._req(
            "DELETE", f"/sessions/{session_id}", params={"archive": str(archive).lower()}
        )

    def list_insights(self, session_ids: list[str] | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"session_ids": list(session_ids)} if session_ids else {}
        return self._paged("/sessions/insights", params)

    def list_automations(self) -> list[dict[str, Any]]:
        return self._paged("/automations", {})

    def list_playbooks(self) -> list[dict[str, Any]]:
        return self._paged("/playbooks", {})

    def list_knowledge_notes(self) -> list[dict[str, Any]]:
        return self._paged("/knowledge/notes", {})

    def list_secrets(self) -> list[dict[str, Any]]:
        return self._paged("/secrets", {})

    def create_pr_review(self, pr_url: str) -> dict[str, Any]:
        return self._req("POST", "/pr-reviews", json={"pr_url": pr_url})

    def create_playbook(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._req("POST", "/playbooks", json=payload)

    def create_knowledge_note(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._req("POST", "/knowledge/notes", json=payload)


# ---------------------------------------------------------------------------- Fake
class FakeTransport:
    """Replays fixtures; synthesises when none exist. Records every call in `calls`."""

    def __init__(self, replay_dir: Path | str | None = None, *, synthesize: bool = True):
        self.replay_dir = Path(replay_dir) if replay_dir else None
        self.synthesize = synthesize
        self.calls: list[tuple[str, Any]] = []
        self._fixtures: list[dict[str, Any]] = []
        self._sessions: dict[str, dict[str, Any]] = {}
        self._counter = 0
        self._prefix = f"fake-{uuid.uuid4().hex[:4]}"  # distinct per instance: ids never collide across seeds and runs
        self.fail_terminate: set[str] = set()  # tests: session ids whose terminate raises
        if self.replay_dir and (self.replay_dir / "sessions").is_dir():
            for f in sorted((self.replay_dir / "sessions").glob("*.json")):
                self._fixtures.append(json.loads(f.read_text()))

    def _pick_fixture(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        tags = set(payload.get("tags", []))
        for fx in self._fixtures:
            if fx.get("used"):
                continue
            if not fx.get("match_tags") or set(fx["match_tags"]) <= tags:
                fx["used"] = True
                return fx
        return None

    def _synth(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._counter += 1
        sid = f"{self._prefix}-{self._counter:03d}"
        repo = (payload.get("repos") or ["owner/repo"])[0]
        tags = payload.get("tags", [])
        shard = next((t.split(":", 1)[1] for t in tags if t.startswith("shard:")), "X")
        if "triage" in tags:
            ticket_id = next((t for t in tags if t.startswith("tkt_")), "tkt_X")
            out: dict[str, Any] = {
                "ticket_id": ticket_id,
                "summary": "synthesised by FakeTransport; replace with a recorded fixture",
                "classes": ["synthesised"],
                "sites": [
                    {
                        "file": "superset/synthesised.py",
                        "line": 1,
                        "class": "synthesised",
                        "kind": "mechanical",
                        "tests": ["tests/unit_tests/synthesised_test.py"],
                    }
                ],
                "acceptance_cmd": {"p2": "true", "p3": "true"},
                "context_sufficient": True,
                "missing": [],
                "split": "one",
                "shards": [],
                "est_size": "S",
                "needs_human": [],
            }
            return self._timeline(sid, out, pr_url=None)
        out = {
            "shard": shard,
            "self_reported_done": True,
            "files_changed": [],
            "call_sites_fixed": [],
            "tests_run": 0,
            "tests_passed": 0,
            "pr_url": f"https://github.com/{repo}/pull/{900 + self._counter}",
            "needs_human": [],
            "notes": "synthesised by FakeTransport; replace with a recorded fixture",
        }
        return self._timeline(sid, out, pr_url=out["pr_url"])

    @staticmethod
    def _timeline(sid: str, out: dict[str, Any], *, pr_url: str | None) -> dict[str, Any]:
        last: dict[str, Any] = {
            "status": "exit",
            "status_detail": "finished",
            "acus_consumed": 2.1,
            "structured_output": out,
        }
        if pr_url:
            last["pull_requests"] = [{"url": pr_url}]
        return {
            "session_id": sid,
            "url": f"https://app.devin.ai/sessions/{sid}",
            "timeline": [
                {"status": "new", "status_detail": None, "acus_consumed": 0.0},
                {"status": "running", "status_detail": "working", "acus_consumed": 0.6},
                {"status": "running", "status_detail": "working", "acus_consumed": 1.4},
                last,
            ],
            "insights": {"session_size": "S", "num_user_messages": 1, "num_devin_messages": 6},
        }

    def _state(self, sid: str, advance: bool) -> dict[str, Any]:
        s = self._sessions[sid]
        tl = s["fixture"]["timeline"]
        idx = min(s["i"], len(tl) - 1) if advance else min(max(s["i"] - 1, 0), len(tl) - 1)
        if advance:
            s["i"] += 1
        state = dict(tl[idx])
        if s["terminated"]:
            state.update(status="exit", status_detail="terminated")
        state.update(
            session_id=sid, url=s["fixture"]["url"], tags=sorted(s["created"].get("tags", []))
        )
        return state

    # transport API
    def create_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("create_session", payload))
        fx = self._pick_fixture(payload)
        if fx is None:
            if not self.synthesize:
                raise DevinError(0, "no fixture for this session and synthesis is off")
            fx = self._synth(payload)
        sid = fx["session_id"]
        self._sessions[sid] = {
            "fixture": fx,
            "i": 1,
            "created": payload,
            "messages": [],
            "terminated": False,
        }
        first = fx["timeline"][0]
        return {
            "session_id": sid,
            "url": fx["url"],
            "tags": sorted(payload.get("tags", [])),
            **first,
        }

    def get_session(self, session_id: str) -> dict[str, Any]:
        self.calls.append(("get_session", session_id))
        return self._state(session_id, advance=True)

    def list_sessions(self, tags: list[str] | None = None) -> list[dict[str, Any]]:
        self.calls.append(("list_sessions", tags))
        want = set(tags or [])
        return [
            self._state(sid, advance=False)
            for sid, s in self._sessions.items()
            if want <= set(s["created"].get("tags", []))
        ]

    def send_message(self, session_id: str, text: str) -> dict[str, Any]:
        self.calls.append(("send_message", (session_id, text)))
        if session_id not in self._sessions:
            raise DevinError(404, f"session {session_id} not found")
        self._sessions[session_id]["messages"].append(text)
        return {"ok": True}

    def terminate(self, session_id: str, archive: bool = True) -> dict[str, Any]:
        self.calls.append(("terminate", (session_id, archive)))
        if session_id in self.fail_terminate:
            raise DevinError(404, "session not found")
        self._sessions[session_id]["terminated"] = True
        return {"ok": True, "archived": archive}

    def list_insights(self, session_ids: list[str] | None = None) -> list[dict[str, Any]]:
        self.calls.append(("list_insights", session_ids))
        out = []
        for sid, s in self._sessions.items():
            if session_ids and sid not in session_ids:
                continue
            last = s["fixture"]["timeline"][-1]
            out.append(
                {
                    "session_id": sid,
                    "acus_consumed": last.get("acus_consumed"),
                    **s["fixture"].get("insights", {}),
                }
            )
        return out

    def list_automations(self) -> list[dict[str, Any]]:
        self.calls.append(("list_automations", None))
        return []

    def list_playbooks(self) -> list[dict[str, Any]]:
        self.calls.append(("list_playbooks", None))
        return []

    def list_knowledge_notes(self) -> list[dict[str, Any]]:
        self.calls.append(("list_knowledge_notes", None))
        return []

    def list_secrets(self) -> list[dict[str, Any]]:
        self.calls.append(("list_secrets", None))
        return []

    def create_pr_review(self, pr_url: str) -> dict[str, Any]:
        self.calls.append(("create_pr_review", pr_url))
        return {"review_id": f"rev-{len(self.calls)}", "pr_url": pr_url, "status": "queued"}

    def create_playbook(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("create_playbook", payload))
        return {"playbook_id": f"pb-{len(self.calls)}", **payload}

    def create_knowledge_note(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("create_knowledge_note", payload))
        return {"note_id": f"kn-{len(self.calls)}", **payload}


# ---------------------------------------------------------------------------- Client
class DevinClient:
    def __init__(self, transport: Transport):
        self.t = transport

    @classmethod
    def from_settings(cls, settings: Settings) -> DevinClient:
        if settings.live and settings.devin_api_key and settings.devin_org_id:
            return cls(HttpTransport(settings.devin_api_key, settings.devin_org_id))
        return cls(FakeTransport(settings.replay_dir))

    @property
    def is_fake(self) -> bool:
        return isinstance(self.t, FakeTransport)

    def start(self, spec: SessionSpec) -> SessionState:
        return SessionState.from_raw(self.t.create_session(spec.to_payload()))

    def status(self, session_id: str) -> SessionState:
        return SessionState.from_raw(self.t.get_session(session_id))

    def find(
        self, tags: list[str], *, exclude: set[str] | None = None, alive_only: bool = True
    ) -> SessionState | None:
        """Sessions on the org carrying every tag, minus `exclude`. An alive one wins; with
        alive_only=False the last-listed terminal one is returned when no live one exists."""
        exclude = exclude or set()
        cands = [SessionState.from_raw(r) for r in self.t.list_sessions(tags)]
        cands = [c for c in cands if c.session_id and c.session_id not in exclude]
        for c in cands:
            if c.alive:
                return c
        if alive_only or not cands:
            return None
        return cands[-1]

    def find_live(self, tags: list[str], *, exclude: set[str] | None = None) -> SessionState | None:
        return self.find(tags, exclude=exclude, alive_only=True)

    def message(self, session_id: str, text: str) -> None:
        self.t.send_message(session_id, text)

    def terminate(self, session_id: str) -> None:
        self.t.terminate(session_id, archive=True)

    def insights(self, session_ids: list[str]) -> dict[str, dict[str, Any]]:
        return {i["session_id"]: i for i in self.t.list_insights(session_ids)}

    def review_pr(self, pr_url: str) -> dict[str, Any]:
        return self.t.create_pr_review(pr_url)
