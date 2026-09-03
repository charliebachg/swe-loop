"""L1: the triage session. Builds the session spec from a ticket and validates the verdict.

The session reads and reasons; it does not patch. Its output is a verdict that satisfies
schemas/triage_verdict.schema.json, which encodes the three questions Devin's own guidance says
to answer before assigning a task: can success be described, is there enough context, would
breaking it down help.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft7Validator

from swe_loop.config import TargetConfig
from swe_loop.devin import SessionSpec
from swe_loop.store import Store

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "schemas" / "triage_verdict.schema.json"
PLAYBOOK_PATH = ROOT / "playbooks" / "triage-pandas3.md"
TRIAGE_ACU_CAP = 3


def load_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text())


def validate_verdict(verdict: Any) -> list[str]:
    """Return a list of problems. Empty means the verdict is acceptable."""
    v = Draft7Validator(load_schema())
    errors = sorted(v.iter_errors(verdict), key=lambda e: list(e.path))
    out = [f"{'/'.join(str(p) for p in e.path) or '$'}: {e.message}" for e in errors]
    if (
        isinstance(verdict, dict)
        and verdict.get("split") == "parallel"
        and not verdict.get("shards")
    ):
        out.append("shards: split is parallel but no shards were given")
    return out


def build_prompt(ticket: dict[str, Any], cfg: TargetConfig, inventory_path: str | None) -> str:
    """What / How / Result, in the shape Devin's prompting guide uses."""
    ext = ticket.get("external_ref") or ticket["id"]
    what = (
        f"In `{cfg.repo}` on branch `{cfg.base_branch}`, read ticket {ext}: {ticket['title']}.\n"
        f"Decide how the {cfg.name} migration work in this ticket should be carried out. "
        f"Do not change any code."
    )
    inv = (
        f"The inventory is at `{inventory_path}`."
        if inventory_path
        else "There is no inventory file; derive sites from the ticket."
    )
    how = "\n".join(
        [
            "Do:",
            f"- {inv}",
            "- Group call sites by module and by class of change. One file belongs to one shard.",
            "- For each group decide whether one session can finish it in under three hours; otherwise split.",
            (
                "- Name an acceptance command per group: pytest over the impacted tests with warnings as errors, "
                "and the same tests on the new library version."
            ),
            (
                "- Flag as needs_human: semantics the upstream notes say need manual review, anything under "
                f"{', '.join(cfg.forbidden_paths)}, and anything under {', '.join(cfg.router.get('coverage_100_paths', []))} without a covering test."
            ),
            "Don't:",
            "- Modify any file. Open or comment on any PR or issue. Install anything.",
            "- Run the full suite; run only the tests named for the sites you are scoping.",
        ]
    )
    result = (
        "Provide structured output matching the verdict schema and call provide_structured_output with "
        "is_final=true. Fields: ticket_id, summary, sites (file, line, class, kind, prescribed_fix, tests), "
        "acceptance_cmd (named commands), context_sufficient and missing, split (one|parallel) with shards, "
        "est_size, needs_human (site, reason). The session is done when that call has been made."
    )
    return f"## What\n{what}\n\n## How\n{how}\n\n## Result\n{result}\n"


def build_triage_spec(
    ticket: dict[str, Any],
    cfg: TargetConfig,
    *,
    inventory_path: str | None,
    playbook_id: str | None,
) -> SessionSpec:
    return SessionSpec(
        prompt=build_prompt(ticket, cfg, inventory_path),
        tags=(cfg.session.get("tags_prefix", "swe-loop"), "triage", ticket["id"]),
        repos=(cfg.repo,),
        max_acu_limit=min(TRIAGE_ACU_CAP, cfg.max_acu_limit),
        structured_output_schema=load_schema(),
        playbook_id=playbook_id,
        title=f"triage {ticket.get('external_ref') or ticket['id']}",
    )


def apply_verdict(store: Store, ticket_id: str, verdict: dict[str, Any]) -> list[str]:
    """Record the verdict and create work orders. Returns the work order ids.

    Routing is the router's job (L2); this only turns shards into rows. If the verdict says the
    whole ticket needs a person, no work order is created and the router will see needs_human.
    """
    problems = validate_verdict(verdict)
    if problems:
        raise ValueError("verdict rejected: " + "; ".join(problems))
    t = store.get_ticket(ticket_id)
    if not t:
        raise KeyError(ticket_id)
    store.upsert_ticket(
        id=ticket_id,
        source=t["source"],
        title=t["title"],
        status="triaged",
        cls=",".join(verdict.get("classes", [])) or t.get("class"),
        triage_verdict=verdict,
    )
    ids: list[str] = []
    if verdict["split"] == "parallel":
        for sh in verdict["shards"]:
            ids.append(
                store.insert_work_order(
                    ticket_id=ticket_id,
                    shard_id=sh["id"],
                    files=sh["files"],
                    tests=sh["tests"],
                    acceptance=sh.get("acceptance_cmd") or verdict["acceptance_cmd"],
                    est_size=sh["est_size"],
                )
            )
    else:
        files = sorted({s["file"] for s in verdict["sites"]})
        tests = sorted({t_ for s in verdict["sites"] for t_ in s.get("tests", [])})
        human_only = {h["site"] for h in verdict["needs_human"]}
        if files and not all(f"{s['file']}:{s['line']}" in human_only for s in verdict["sites"]):
            ids.append(
                store.insert_work_order(
                    ticket_id=ticket_id,
                    shard_id=ticket_id.removeprefix("tkt_"),
                    files=files,
                    tests=tests,
                    acceptance=verdict["acceptance_cmd"],
                    est_size=verdict["est_size"],
                )
            )
    return ids
