"""The part of the app that is looking when Devin's schedule fires.

Devin holds the recurrence: a schedule registered against the code scan, backed by an Automation
on the organisation, firing on Devin's side. There is no outbound webhook, so nothing about a run
reaches this store unless something here is looking. Until this module existed, the looking was
a command somebody had to remember to start in a terminal, and when they did not, Devin ran every
hour and the board said nothing had happened.

So the app looks by itself. One daemon thread, one tick every couple of minutes. A tick asks Devin
whether the schedule is switched on; if it is, it runs the automation in the mode where the
schedule is the only trigger, which records any run Devin made, files anything new it found, and
never starts a scan of its own. If the schedule is off, the tick costs one read and nothing else.

The same thread looks at GitHub for the issue-driven automations: one list call per enabled
automation, and a ticket filed for any new issue carrying the label. That is all it does with an
issue; scoping and repair start when a person clicks Run, the same as an event that arrives by
webhook. Looking leaves nothing on the timeline; finding something does.

The watcher never runs at the same time as a run somebody clicked: it takes the same lock and
skips the tick if the lock is held. Stopping it loses nothing, because adopt reads state rather
than events; the next tick picks up whatever happened in between.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from swe_loop.config import Settings, TargetConfig
from swe_loop.store import Store, now

DEFAULT_EVERY = 120.0


class Watcher:
    def __init__(
        self,
        settings: Settings,
        cfg: TargetConfig,
        store: Store,
        client: Any,
        lock: threading.Lock,
        *,
        aid: str = "auto_codescan",
        every: float = DEFAULT_EVERY,
        log: Callable[[str], None] = lambda _m: None,
        sleep: Callable[[float], None] = time.sleep,
        fetch: Callable[[str], Any] | None = None,
    ):
        self.settings, self.cfg, self.store, self.client = settings, cfg, store, client
        self.lock, self.aid, self.every, self.log, self.sleep = lock, aid, every, log, sleep
        self.fetch = fetch
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> bool:
        """Start looking. Returns False when there is nothing to look at: replay mode has no
        organisation; without a schedule on Devin and without a GitHub token there is nothing
        that could change on its own."""
        if not self.settings.live or getattr(self.client, "is_fake", False):
            return False
        row = self.store.get_automation(self.aid) or {}
        if not row.get("devin_automation_id") and not self._issue_automations():
            return False
        if self._thread and self._thread.is_alive():
            return True
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="swe-loop-watch", daemon=True)
        self._thread.start()
        self.store.set_setting("watch.started_at", now())
        return True

    def stop(self) -> None:
        self._stop.set()
        self.store.set_setting("watch.started_at", "")

    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stop.is_set())

    # ------------------------------------------------------------------ the loop
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as ex:  # noqa: BLE001 - a bad tick is logged, never fatal
                self.store.log(
                    "scan",
                    "a look at Devin's schedule failed",
                    detail=f"{type(ex).__name__}: {ex}"[:200],
                )
            try:
                self.look_for_issues()
            except Exception as ex:  # noqa: BLE001 - same rule
                self.store.log(
                    "intake",
                    "a look at GitHub failed",
                    detail=f"{type(ex).__name__}: {ex}"[:200],
                )
            # sleep in small steps so stop() is honoured promptly
            waited = 0.0
            while waited < self.every and not self._stop.is_set():
                self.sleep(min(2.0, self.every - waited))
                waited += 2.0

    def tick(self) -> dict[str, Any]:
        """One look. Cheap when the schedule is off; the full adopt path when it is on."""
        from swe_loop import runner

        row = self.store.get_automation(self.aid) or {}
        devin_id = row.get("devin_automation_id")
        if not devin_id:
            return {"looked": False, "why": "no schedule on Devin"}
        theirs = self.client.automation(devin_id) or {}
        self.store.set_setting("watch.last_tick", now())
        if not theirs.get("enabled"):
            self.store.set_setting("watch.schedule_on", "0")
            return {"looked": True, "schedule": "off"}
        self.store.set_setting("watch.schedule_on", "1")
        if not self.lock.acquire(blocking=False):
            return {"looked": True, "schedule": "on", "skipped": "a run is going"}
        try:
            out = runner.run_automation(
                self.settings,
                self.cfg,
                self.store,
                self.client,
                self.aid,
                log=self.log,
                only_if_scheduled=True,
            )
        finally:
            self.lock.release()
        return {
            "looked": True,
            "schedule": "on",
            **{k: out.get(k) for k in ("scan", "scheduled_runs", "new_tickets")},
        }

    # ------------------------------------------------------------------ issues on GitHub
    def _issue_automations(self) -> list[dict[str, Any]]:
        """The enabled automations that start from issues on a repository. A public repository is
        read without a token: GitHub allows sixty anonymous calls an hour and a look every two
        minutes uses thirty. A token only raises that ceiling."""
        return [
            a
            for a in self.store.list_automations()
            if a.get("kind") == "repair"
            and a.get("enabled")
            and (a.get("trigger") or {}).get("source", "github") == "github"
        ]

    def look_for_issues(self) -> dict[str, Any]:
        """One list call per issue-driven automation. A new issue with the label becomes a ticket
        and a run row that says so; an unchanged repository leaves nothing behind."""
        from swe_loop import runner

        autos = self._issue_automations()
        if not autos:
            return {"looked": False, "why": "no issue automation, or no GitHub token"}
        out: dict[str, Any] = {"looked": True, "new_tickets": []}
        for a in autos:
            repo = a.get("target") or self.cfg.repo
            label = (a.get("trigger") or {}).get("issue_label") or self.cfg.trigger.get(
                "issue_label", "swe-loop"
            )
            if not self.lock.acquire(blocking=False):
                return {**out, "skipped": "a run is going"}
            try:
                issues = runner.fetch_issues(repo, label, self.settings.github_token, self.fetch)
                got = runner.intake_issues(
                    self.store, self.cfg, issues, source_repo=repo, quiet=True
                )
            finally:
                self.lock.release()
            self.store.set_setting("watch.last_issue_look", now())
            if not got["new"]:
                continue
            result = {
                "issues": got["issues"],
                "new_tickets": got["new"],
                "known": got["known"],
                "skipped": got["skipped"],
                "started_by": "a new issue on GitHub",
                "stopped_after": "intake",
            }
            rid = self.store.start_automation_run(a["id"])
            self.store.finish_automation_run(rid, result)
            self.store.set_automation(a["id"], last_run=now(), last_result=result)
            out["new_tickets"] += got["new"]
        return out


def status(store: Store) -> dict[str, Any]:
    """What the page can say about the watching: whether it is on, when it last looked, and
    whether Devin's schedule was on when it did."""
    started = store.get_setting("watch.started_at") or ""
    last = store.get_setting("watch.last_tick") or ""
    return {
        "watching": bool(started),
        "since": started[:16].replace("T", " "),
        "last_look": last[:16].replace("T", " "),
        "schedule_on": store.get_setting("watch.schedule_on") == "1",
    }
