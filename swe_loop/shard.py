"""Shard. Deterministic, no model. Splits a work order into units small enough for one
session, keeping every file in exactly one shard. Files with no signal never reach a session,
because they never reach a work order in the first place."""

from __future__ import annotations

from typing import Any

from swe_loop.config import TargetConfig


def _tests_for(files: list[str], all_tests: list[str]) -> list[str]:
    """Tests whose path shares a module name with one of the files; all tests if nothing matches."""
    stems = {f.rsplit("/", 1)[-1].removesuffix(".py") for f in files}
    dirs = {f.rsplit("/", 2)[-2] for f in files if "/" in f}
    picked = [t for t in all_tests if any(s in t for s in stems | dirs)]
    return picked or list(all_tests)


def split_work_order(
    wo: dict[str, Any], cfg: TargetConfig, sites: list[dict] | None = None
) -> list[dict[str, Any]]:
    """Return one or more shard dicts (files, tests, acceptance, est_size, site_count).

    A shard never exceeds max_files_per_shard files or max_call_sites_per_shard sites. Files
    stay whole: a file with more sites than the cap becomes its own shard, over the cap, and is
    flagged `oversize` for the router to escalate.
    """
    max_files = int(cfg.router.get("max_files_per_shard", 3))
    max_sites = int(cfg.router.get("max_call_sites_per_shard", 6))
    files = list(wo["files"])
    per_file = {f: 0 for f in files}
    for s in sites or []:
        if s.get("file") in per_file:
            per_file[s["file"]] += 1
    for f in files:
        per_file[f] = per_file[f] or 1

    shards: list[dict[str, Any]] = []
    cur: list[str] = []
    cur_sites = 0
    for f in files:
        n = per_file[f]
        if cur and (len(cur) >= max_files or cur_sites + n > max_sites):
            shards.append(cur)
            cur, cur_sites = [], 0
        cur.append(f)
        cur_sites += n
    if cur:
        shards.append(cur)

    out = []
    for i, group in enumerate(shards):
        n_sites = sum(per_file[f] for f in group)
        out.append(
            {
                "shard_id": wo["shard_id"] if len(shards) == 1 else f"{wo['shard_id']}{i + 1}",
                "files": group,
                "tests": _tests_for(group, wo["tests"]),
                "acceptance": dict(wo["acceptance"]),
                "est_size": "XS" if n_sites <= 1 else ("S" if n_sites <= max_sites else "M"),
                "site_count": n_sites,
                "oversize": n_sites > max_sites,
            }
        )
    return out
