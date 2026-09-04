"""One automation run, start to finish. The Run button and the CLI both land here.

The repository's issues become tickets, a triage session scopes each new one, code routes them,
repair sessions run, the gate checks every PR from a clean checkout, Devin Review reads it.
Each step writes to the timeline as it happens, so the pages can follow along."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

from swe_loop.config import Settings, TargetConfig
from swe_loop.intake import ingest, normalize
from swe_loop.store import Store, now, plural

ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "data" / "inventory" / "2026-09-03"

Fetch = Callable[[str, dict[str, str]], Any]


def _http_get_json(url: str, headers: dict[str, str]) -> Any:
    import httpx

    r = httpx.get(url, headers=headers, timeout=20)
    r.raise_for_status()
    return r.json()


def fetch_issues(
    repo: str, label: str, token: str, fetch: Fetch | None = None
) -> list[dict[str, Any]]:
    """Open issues on the repository carrying the label. Pull requests are issues to GitHub; they
    are dropped here."""
    url = (
        f"https://api.github.com/repos/{repo}/issues?state=open&per_page=100&labels={quote(label)}"
    )
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "swe-loop"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    items = (fetch or _http_get_json)(url, headers)
    return [i for i in items if isinstance(i, dict) and "pull_request" not in i]


def drafted_issues(
    repo: str, label: str, tickets_json: Path = INVENTORY / "tickets.json"
) -> list[dict[str, Any]]:
    """The issues as drafted for the repository, for a run with no network: same numbers, same
    titles, same labels as the ones filed."""
    if not tickets_json.exists():
        return []
    d = json.loads(tickets_json.read_text())
    if d.get("repo") and d["repo"] != repo:
        return []
    numbers = d.get("numbers", {})
    out = []
    for sh in d.get("shards", []):
        n = numbers.get(sh["id"])
        if n is None:
            continue
        out.append(
            {
                "number": n,
                "title": sh["title"],
                "body": sh.get("why", ""),
                "labels": [{"name": label}],
                "user": {"login": "charliebachg"},
                "html_url": f"https://github.com/{repo}/issues/{n}",
            }
        )
    return out


def parent_number(repo: str, tickets_json: Path = INVENTORY / "tickets.json") -> int | None:
    """The tracking issue the shards hang under. It describes the work; it is not work, so no
    session is ever spent on it."""
    if not tickets_json.exists():
        return None
    d = json.loads(tickets_json.read_text())
    if d.get("repo") and d["repo"] != repo:
        return None
    n = (d.get("numbers") or {}).get("P")
    return int(n) if n is not None else None


def shard_letters(repo: str, tickets_json: Path = INVENTORY / "tickets.json") -> dict[int, str]:
    """Issue number to shard letter, when the repository is the one the inventory was drafted for.
    Keeps the ticket ids the rest of the app already knows (tkt_A, not tkt_is1)."""
    if not tickets_json.exists():
        return {}
    d = json.loads(tickets_json.read_text())
    if d.get("repo") and d["repo"] != repo:
        return {}
    return {int(n): sid for sid, n in d.get("numbers", {}).items() if sid != "P"}


def intake_issues(
    store: Store, cfg: TargetConfig, issues: list[dict[str, Any]], *, source_repo: str
) -> dict[str, Any]:
    """Every issue becomes a ticket with status new, once. A ticket the store already has is
    left exactly as it is."""
    letters = shard_letters(source_repo)
    parent = parent_number(source_repo)
    exclude = {lbl.lower() for lbl in (cfg.trigger.get("exclude_labels") or [])}
    new_ids: list[str] = []
    known = 0
    skipped = 0
    for issue in issues:
        number = issue.get("number")
        labels = {(lbl.get("name") or "").lower() for lbl in (issue.get("labels") or [])}
        if (parent is not None and number == parent) or (labels & exclude):
            skipped += 1
            store.log(
                "intake",
                f"issue #{number} skipped",
                detail="the tracking issue for the others; it describes work, it is not work"
                if number == parent
                else f"carries {', '.join(sorted(labels & exclude))}",
            )
            continue
        payload = {"action": "labeled", "issue": issue, "repository": {"full_name": source_repo}}
        ev = normalize("github", payload, cfg)
        if ev is None:
            continue
        letter = letters.get(int(issue.get("number") or -1))
        tid = f"tkt_{letter}" if letter else None
        before = store.get_ticket(tid) if tid else None
        if before is None and tid is None:
            from swe_loop.intake import ticket_id_for

            before = store.get_ticket(ticket_id_for(ev))
        if before is not None:
            known += 1
            continue
        tid = ingest(store, ev, ticket_id=tid, as_new=True)
        store.log(
            "intake",
            f"issue #{issue.get('number')} became a ticket",
            ticket_id=tid,
            detail=issue.get("html_url"),
        )
        new_ids.append(tid)
    return {"issues": len(issues), "new": new_ids, "known": known, "skipped": skipped}


def run_automation(
    settings: Settings,
    cfg: TargetConfig,
    store: Store,
    client: Any,
    aid: str,
    *,
    log: Callable[[str], None] = print,
    fetch: Fetch | None = None,
    stop_after: str | None = None,
) -> dict[str, Any]:
    """The whole loop for one automation: intake, triage, route, repair, gate, review.

    stop_after="intake" files the tickets and stops there, which is how you find out what a
    repository holds without paying to fix it."""
    from swe_loop.cli import run_once
    from swe_loop.triage import triage_all

    a = store.get_automation(aid)
    if a is None:
        raise ValueError(f"no automation {aid}")
    rid = store.start_automation_run(aid)
    repo = a.get("target") or cfg.repo
    label = (a.get("trigger") or {}).get("issue_label") or cfg.trigger.get(
        "issue_label", "swe-loop"
    )
    result: dict[str, Any] = {"issues": 0, "new_tickets": [], "known": 0, "triaged": 0}
    try:
        live = bool(settings.live and not getattr(client, "is_fake", False))
        if a["kind"] == "scan":
            from swe_loop import scan as scan_mod

            found = scan_mod.run_scan(
                settings,
                cfg,
                store,
                client,
                limit=a.get("max_findings"),
                playbook_id=store.get_setting("playbook_id.scan-pandas3"),
                log=log,
            )
            result["scan"] = found.get("kind")
            result["session"] = found.get("session", "")
            result["taken"] = len(found.get("taken", []))
            result["dropped"] = found.get("dropped", 0)
            got = {
                "issues": len(found.get("new", [])) + len(found.get("known", [])),
                "new": found.get("new", []),
                "known": len(found.get("known", [])),
                "skipped": len(found.get("refused", [])) + len(found.get("taken", [])),
            }
        else:
            issues = (
                fetch_issues(repo, label, settings.github_token, fetch)
                if live or fetch
                else drafted_issues(repo, label)
            )
            got = intake_issues(store, cfg, issues, source_repo=repo)
        result.update(
            {
                "issues": got["issues"],
                "new_tickets": got["new"],
                "known": got["known"],
                "skipped": got["skipped"],
            }
        )
        store.log(
            "intake",
            f"{plural(got['issues'], 'open issue')} with label {label}; "
            f"{plural(len(got['new']), 'new ticket')}",
            detail=repo,
        )
        log(f"intake: {plural(got['issues'], 'issue')}, {len(got['new'])} new")
        if live and not store.get_setting("playbook_id.repair-pandas3"):
            from swe_loop.cli import apply_config

            made = apply_config(settings, cfg, store, client)
            n = len(made["created"])
            store.log(
                "triage",
                f"the organisation was configured: {plural(n, 'object')} created",
                detail="playbooks and Knowledge notes; anything already there was adopted",
            )
            log(f"apply-config: {n} created, {len(made['already_on_the_org'])} already there")
        # A ticket in a file another change owns is refused before anything is spent on it.
        from swe_loop import scan as scan_mod

        held = scan_mod.refuse_reserved(store, cfg)
        if held:
            result["refused_reserved"] = len(held)
            log(f"refused {plural(len(held), 'ticket')} in files another change already owns")
        if stop_after == "intake":
            result["stopped_after"] = "intake"
            log("stopping after intake, as asked: nothing is scoped or repaired")
            store.finish_automation_run(rid, result)
            return result
        inv = cfg.triage.get("inventory_url") or None
        pid_tri = store.get_setting("playbook_id.triage-pandas3")
        verdicts = triage_all(store, client, cfg, inventory_path=inv, playbook_id=pid_tri)
        result["triaged"] = len(verdicts)
        log(f"scoping: {plural(len(verdicts), 'session')}")
        pid_rep = store.get_setting("playbook_id.repair-pandas3")
        result.update(run_once(settings, cfg, store, client, playbook_id=pid_rep, log=log))
        store.finish_automation_run(rid, result)
    except Exception as ex:
        result["error"] = f"{type(ex).__name__}: {ex}"[:300]
        store.finish_automation_run(rid, result, status="failed")
        raise
    finally:
        store.set_automation(aid, last_run=now(), last_result=result)
    return result
