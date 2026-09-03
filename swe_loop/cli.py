"""Command line: serve the dashboard, run the loop, seed or record a replay.

    python -m swe_loop serve                 # dashboard + intake on :8000
    python -m swe_loop triage [--ticket ID]  # one triage session per new ticket; the verdict becomes work orders
    python -m swe_loop review-followup --ticket ID  # review remarks back to the session, re-gate
    python -m swe_loop run                   # one pass: route, dispatch, poll, gate, reduce
    python -m swe_loop seed                  # fill an empty store for replay
    python -m swe_loop record data/replay/run.json
    python -m swe_loop apply-config --dry-run  # what would be created on the org; creates nothing
    python -m swe_loop apply-config          # create playbooks and knowledge on the org (live only)

Mode comes from the environment: without DEVIN_API_KEY everything is replay, always.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from swe_loop import cost
from swe_loop.config import Settings, TargetConfig
from swe_loop.devin import DevinClient
from swe_loop.dispatch import dispatch
from swe_loop.gate import Gate, apply_result
from swe_loop.knowledge import load_notes, load_playbook
from swe_loop.poll import Poller
from swe_loop.reduce import detect_conflicts, refresh_reviews, summary
from swe_loop.replay import record, seed
from swe_loop.router import route_all
from swe_loop.store import Store, now

ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "data" / "inventory" / "2026-09-03"


def _ctx():
    settings = Settings.from_env()
    cfg = TargetConfig.load(settings.config_path)
    store = Store(settings.db_path)
    client = DevinClient.from_settings(settings)
    return settings, cfg, store, client


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run("swe_loop.app:app", host=args.host, port=args.port, reload=False)
    return 0


def cmd_seed(args: argparse.Namespace) -> int:
    settings, cfg, store, _ = _ctx()
    if settings.live or args.as_new:
        # live: the tickets enter as new and a triage session decides; nothing is synthesised
        if store._one("SELECT COUNT(*) AS n FROM tickets")["n"]:
            print(json.dumps({"seeded": False, "reason": "store not empty"}))
            return 0
        from swe_loop.store import load_tickets

        ids = load_tickets(store, INVENTORY / "tickets.json", triaged=False)
        store.log(
            "L0 intake",
            f"{len(ids)} ticket(s) loaded as new",
            detail=str(INVENTORY / "tickets.json"),
        )
        print(json.dumps({"seeded": True, "recorded": False, "as_new": True, "tickets": ids}))
        return 0
    out = seed(store, cfg, tickets_json=INVENTORY / "tickets.json", replay_dir=settings.replay_dir)
    print(json.dumps(out))
    return 0


def cmd_budget(args: argparse.Namespace) -> int:
    _, cfg, store, _ = _ctx()
    per = args.per_session if args.per_session is not None else cfg.max_acu_limit
    if args.cap <= 0 or per <= 0:
        print("refusing: caps must be positive", file=sys.stderr)
        return 2
    store.set_budget(acu_cap=args.cap, per_session_cap=per)
    store.log("budget", f"cap {args.cap:g} ACU, {per:g} per session")
    print(json.dumps(store.budget_state()))
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    _, _, store, _ = _ctx()
    print(json.dumps(record(store, args.path)))
    return 0


def run_once(
    settings: Settings,
    cfg: TargetConfig,
    store: Store,
    client: DevinClient,
    *,
    playbook_id: str | None = None,
    log=print,
) -> dict:
    """One pass: route → dispatch → poll → gate → reduce over the routed tickets. Shared by the
    CLI and the Run now button. In replay the poller does not sleep for real."""
    fast = client.is_fake
    poller = Poller(
        store, client, cfg, sleep=(lambda s: time.sleep(min(s, 0.05))) if fast else time.sleep
    )
    decisions = route_all(store, cfg)
    log(f"routed {len(decisions)} ticket(s)")
    gate = None
    repo_root = (ROOT / cfg.gate.get("repo_root", "../superset-fork")).resolve()
    if repo_root.exists() and not fast:
        gate = Gate(
            store,
            cfg,
            repo_root=repo_root,
            evidence_dir=ROOT / cfg.gate.get("evidence_dir", "data/live/evidence"),
            timeout=int(cfg.gate.get("timeout_s", 1800)),
        )
    out = {"dispatched": 0, "finished": 0, "gated": 0, "escalated": 0}
    for t in (
        store.list_tickets("routed")
        + store.list_tickets("dispatched")
        + store.list_tickets("running")
    ):
        for wo in store.work_orders_for(t["id"]):
            if wo["status"] not in ("devin", "dispatched"):
                continue
            review = "required" if "review required" in (t["router_reason"] or "") else "normal"
            sid = dispatch(store, client, wo, cfg, review=review, playbook_id=playbook_id)
            out["dispatched"] += 1
            res = poller.wait(sid)
            log(f"{t['id']} shard {wo['shard_id']}: {res.kind} {res.detail}")
            if res.kind == "finished":
                out["finished"] += 1
                if gate:
                    g = gate.run_gate(sid)
                    did = apply_result(g, store, client, poller)
                    out["gated"] += 1
                    log(f"  gate {g.gate_result}: {'; '.join(g.reasons)[:120]} -> {did}")
                else:
                    store.log(
                        "L5 gate",
                        "skipped",
                        ticket_id=t["id"],
                        session_id=sid,
                        detail="replay: a fake session has no real PR to check out",
                    )
            elif res.kind not in ("running",):
                out["escalated"] += 1
            poller.enforce_budget()
    detect_conflicts(store)
    if not fast:
        n_rev = refresh_reviews(store, client, settings.github_token)
        if n_rev:
            log(f"{n_rev} Devin Review result(s) read back")
    store.set_setting("automation.repair.last_run", now())
    store.set_setting("automation.repair.last_result", json.dumps(out))
    log(json.dumps(summary(store)))
    return out


def cmd_run(args: argparse.Namespace) -> int:
    settings, cfg, store, client = _ctx()
    if not settings.live:
        print(
            "mode=replay: sessions are faked; the gate is skipped",
            file=sys.stderr,
        )
    pid = args.playbook_id or store.get_setting("playbook_id.repair-pandas3")
    run_once(settings, cfg, store, client, playbook_id=pid)
    return 0


def plan_config(client: DevinClient | None) -> dict[str, Any]:
    """What apply-config would create on the org, and what is already there. Read-only:
    the only calls are GET listings, and only when a live client is given."""
    existing_pb: set[str] = set()
    existing_kn: set[str] = set()
    if client is not None and not client.is_fake:
        existing_pb = {p.get("title", "") for p in client.t.list_playbooks()}
        existing_kn = {n.get("name", "") for n in client.t.list_knowledge_notes()}
    playbooks = []
    for name, schema in (
        ("triage-pandas3", "triage_verdict.schema.json"),
        ("repair-pandas3", "repair_result.schema.json"),
    ):
        pb = load_playbook(ROOT / "playbooks" / f"{name}.md", ROOT / "schemas" / schema)
        payload = pb.to_payload()
        playbooks.append(
            {
                "file": name,
                "name": pb.name,
                "sections": [ln[3:].strip() for ln in pb.body.splitlines() if ln.startswith("## ")],
                "body_chars": len(pb.body),
                "schema_fields": sorted((pb.structured_output_schema or {}).get("properties", {})),
                "schema_bytes": len(json.dumps(pb.structured_output_schema or {})),
                "payload_keys": sorted(payload),
                "action": "exists on the org" if pb.name in existing_pb else "would create",
            }
        )
    notes = [
        {
            "name": n.name,
            "trigger_description": n.trigger_description,
            "body_chars": len(n.body),
            "action": "exists on the org" if n.name in existing_kn else "would create",
        }
        for n in load_notes()
    ]
    return {
        "playbooks": playbooks,
        "knowledge_notes": notes,
        "secrets": "none: the target's tests need no credentials; repository access is the Devin GitHub App",
        "automations": "none: v0 work enters through /intake/github or the run command; a native Automation is a v1 item",
        "creates": sum(1 for x in playbooks + notes if x["action"] == "would create"),
    }


def cmd_triage(args: argparse.Namespace) -> int:
    from swe_loop.triage import triage_all

    settings, cfg, store, client = _ctx()
    if not settings.live:
        print("mode=replay: the triage session is faked", file=sys.stderr)
    inv = cfg.triage.get("inventory_url") or None
    pid = args.playbook_id or store.get_setting("playbook_id.triage-pandas3")
    if args.answer and not args.ticket:
        print("refusing: --answer needs --ticket", file=sys.stderr)
        return 2
    results = triage_all(
        store,
        client,
        cfg,
        ticket_id=args.ticket,
        inventory_path=inv,
        playbook_id=pid,
        answer=args.answer,
    )
    if not results:
        print("no tickets with status new")
    for r in results:
        print(json.dumps(r))
    return 0


def cmd_review_followup(args: argparse.Namespace) -> int:
    from swe_loop.followup import review_followup

    settings, cfg, store, client = _ctx()
    if not settings.live:
        print("mode=replay: the session is faked; the gate is skipped", file=sys.stderr)
    out = review_followup(store, client, cfg, args.ticket, settings.github_token)
    print(json.dumps(out))
    return 0


def cmd_cost(args: argparse.Namespace) -> int:
    """A person copies the console's per-session dollars: --set <devin id or prefix>=<usd> ..."""
    _, _, store, _ = _ctx()
    out = {}
    for item in args.set:
        key, _, val = item.partition("=")
        try:
            usd = float(val)
        except ValueError:
            print(f"refusing: {item!r} is not id=usd", file=sys.stderr)
            return 2
        table = store.set_session_cost(key.strip(), usd)
        out[key.strip()] = table or "no unique session with that id"
    print(json.dumps({"entered": out, "spend": cost.spend(store)}, default=str))
    return 0


def cmd_apply_config(args: argparse.Namespace) -> int:
    settings, cfg, store, client = _ctx()
    if args.dry_run:
        plan = plan_config(client if settings.live else None)
        plan["mode"] = settings.mode
        plan["note"] = "dry run: nothing was created" + (
            "" if settings.live else "; replay mode, so the org was not read either"
        )
        print(json.dumps(plan, indent=1))
        return 0
    if not settings.live:
        print(
            "refusing: apply-config creates objects on the org and only runs in live mode",
            file=sys.stderr,
        )
        return 2
    existing_pb = {p.get("title", ""): p.get("playbook_id") for p in client.t.list_playbooks()}
    existing_kn = {n.get("name", ""): n.get("note_id") for n in client.t.list_knowledge_notes()}
    made: dict[str, Any] = {}
    skipped: dict[str, Any] = {}
    for name, schema in (
        ("triage-pandas3", "triage_verdict.schema.json"),
        ("repair-pandas3", "repair_result.schema.json"),
    ):
        pb = load_playbook(ROOT / "playbooks" / f"{name}.md", ROOT / "schemas" / schema)
        if pb.name in existing_pb:
            skipped[name] = existing_pb[pb.name]
            pid = existing_pb[pb.name]
        else:
            pid = client.t.create_playbook(pb.to_payload()).get("playbook_id")
            made[name] = pid
        if pid:
            store.set_setting(f"playbook_id.{name}", pid)
    for note in load_notes():
        if note.name in existing_kn:
            skipped[note.name] = existing_kn[note.name]
            continue
        made[note.name] = client.t.create_knowledge_note(note.to_payload(pinned_repo=cfg.repo)).get(
            "note_id"
        )
    print(json.dumps({"created": made, "already_on_the_org": skipped}, indent=1))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="swe_loop")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(fn=cmd_serve)
    sd = sub.add_parser("seed")
    sd.add_argument(
        "--as-new", action="store_true", help="tickets as new, no verdicts (always so in live mode)"
    )
    sd.set_defaults(fn=cmd_seed)
    bg = sub.add_parser("budget")
    bg.add_argument("--cap", type=float, required=True, help="ACU cap for the whole run")
    bg.add_argument(
        "--per-session", type=float, default=None, help="ACU cap per session; default from the seam"
    )
    bg.set_defaults(fn=cmd_budget)
    r = sub.add_parser("record")
    r.add_argument("path")
    r.set_defaults(fn=cmd_record)
    ru = sub.add_parser("run")
    ru.add_argument("--playbook-id", default=None)
    ru.set_defaults(fn=cmd_run)
    tr = sub.add_parser("triage")
    tr.add_argument(
        "--ticket", default=None, help="one ticket id; default: every ticket with status new"
    )
    tr.add_argument("--playbook-id", default=None)
    tr.add_argument(
        "--answer",
        default=None,
        help="a person's answer to a waiting triage session; polling resumes",
    )
    tr.set_defaults(fn=cmd_triage)
    rf = sub.add_parser("review-followup")
    rf.add_argument(
        "--ticket",
        required=True,
        help="send the review remarks on this ticket's PR back to its session, then re-gate",
    )
    rf.set_defaults(fn=cmd_review_followup)
    co = sub.add_parser("cost")
    co.add_argument(
        "--set",
        action="append",
        default=[],
        help="<devin session id or prefix>=<usd from the console>; repeatable",
    )
    co.set_defaults(fn=cmd_cost)
    ac = sub.add_parser("apply-config")
    ac.add_argument(
        "--dry-run", action="store_true", help="print what would be created; create nothing"
    )
    ac.set_defaults(fn=cmd_apply_config)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
