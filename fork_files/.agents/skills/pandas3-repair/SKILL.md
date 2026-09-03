---
name: pandas3-repair
description: Repair one shard of the pandas 2.3.3 to 3.0.5 migration so the code runs on both versions, verified by the ticket's acceptance commands. Use when a ticket carries a swe-loop work order block.
allowed-tools: bash, edit, git
argument-hint: <ticket number>
---

# pandas 3 repair, one shard

Read the ticket's work order block. It names the files, the sites, the tests, and the
acceptance commands. Then:

1. Reproduce: run the acceptance command with warnings as errors on the current version and
   confirm it fails at the listed sites.
2. Fix each site as the pandas message prescribes. Keep changes inside the listed files.
3. Verify: every acceptance command exits 0, on both versions.
4. Format: `ruff format` and `ruff check` on changed files. No black.
5. Deliver: one PR, title `fix(<module>): <summary>`, template filled, structured output
   provided with `is_final=true`.

Never edit `tests/`, `.github/`, `superset/migrations/`, or `requirements/`. Never move the
pandas lower bound. If a fix depends on context you cannot see, report it as `needs_human`.

Current state of the migration: !`git log --oneline -3`
