"""Command line: serve the dashboard, run the loop, seed or record a replay.

    python -m swe_loop serve                 # dashboard + intake on :8000
    python -m swe_loop run                   # one pass: route, dispatch, poll, gate, reduce
    python -m swe_loop seed                  # fill an empty store for replay
    python -m swe_loop record data/replay/run.json
    python -m swe_loop apply-config          # create playbooks and knowledge on the org (live only)

Mode comes from the environment: without DEVIN_API_KEY everything is replay, always.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from swe_loop.config import Settings, TargetConfig
from swe_loop.devin import DevinClient
from swe_loop.dispatch import dispatch
from swe_loop.gate import Gate, apply_result
from swe_loop.knowledge import load_notes, load_playbook
from swe_loop.poll import Poller
from swe_loop.reduce import detect_conflicts, summary
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
    out = seed(store, cfg, tickets_json=INVENTORY / "tickets.json", replay_dir=settings.replay_dir)
    print(json.dumps(out))
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
    run_once(settings, cfg, store, client, playbook_id=args.playbook_id)
    return 0


def cmd_apply_config(args: argparse.Namespace) -> int:
    settings, _cfg, _, client = _ctx()
    if not settings.live:
        print(
            "refusing: apply-config creates objects on the org and only runs in live mode",
            file=sys.stderr,
        )
        return 2
    made = {}
    for name, schema in (
        ("triage-pandas3", "triage_verdict.schema.json"),
        ("repair-pandas3", "repair_result.schema.json"),
    ):
        pb = load_playbook(ROOT / "playbooks" / f"{name}.md", ROOT / "schemas" / schema)
        made[name] = client.t.create_playbook(pb.to_payload()).get("playbook_id")
    for note in load_notes():
        made[note.name] = client.t.create_knowledge_note(note.to_payload()).get("note_id")
    print(json.dumps(made, indent=1))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="swe_loop")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(fn=cmd_serve)
    sub.add_parser("seed").set_defaults(fn=cmd_seed)
    r = sub.add_parser("record")
    r.add_argument("path")
    r.set_defaults(fn=cmd_record)
    ru = sub.add_parser("run")
    ru.add_argument("--playbook-id", default=None)
    ru.set_defaults(fn=cmd_run)
    sub.add_parser("apply-config").set_defaults(fn=cmd_apply_config)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
