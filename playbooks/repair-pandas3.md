# Repair one shard of a major-version dependency upgrade

## Overview

Fix the listed call sites so the code runs correctly on both the current and the new version of
the library, open one pull request against the fork, and report what you did in structured
output. The ticket names the files, the call sites, the tests, and the acceptance commands. You
are one of several sessions working in parallel on disjoint files; stay inside yours.

## Procedure

1. Check out the branch named in the ticket and create a working branch from it named
   `swe-loop/<shard>`.
2. Read the ticket's site table. For each site, read the library's own message and the
   surrounding function before changing anything.
3. Run the first acceptance command as given, on the current library version with warnings
   promoted to errors. Confirm it fails at the listed sites. If it does not fail, stop and
   report `context_sufficient: false` with what you observed.
4. Fix each site as the library's message prescribes. Keep the change local to the site.
5. Run every acceptance command in the ticket. All must exit 0. If one does not, fix and re-run.
   Do not modify a test to make it pass; report it instead.
6. Run `ruff format` and `ruff check` on the files you changed.
7. Commit with a conventional-commit message, push the working branch, and open one pull
   request against the fork's default branch using the repository's pull request template.
   Title: `fix(<scope>): <summary>` where scope is the module you changed.
8. Provide the structured output with `is_final=true`: the files changed, the sites fixed,
   the tests run and passed, the PR URL, and anything you could not do with its reason.

## Specifications

- The code runs on both library versions named in the ticket. The dependency range in
  `pyproject.toml` is not changed; the lower bound does not move.
- Only the files listed in the ticket are changed. No file under `tests/`, `.github/`,
  `superset/migrations/`, or `requirements/` is touched.
- Every acceptance command in the ticket exits 0 on your branch.
- The PR is small: one shard, one purpose, one conventional-commit title.

## Advice and Pointers

- Before building any test environment, read the knowledge note on the test environments for the
  two library versions. It says which packages to leave out and why; sessions that built first
  and read second lost the first attempt to missing system headers.
- `obj[col].method(value, inplace=True)` never works under copy-on-write. The library's message
  gives the two replacements; prefer `obj[col] = obj[col].method(value)`.
- Where `replace` used to downcast, call `.infer_objects(copy=False)` afterwards if the tests
  need the narrower dtype, and only then.
- The new `stack` implementation does not accept `dropna=`; use `future_stack=True` on the
  current version and drop the argument, then check the all-NaN rows the test expects.
- A column the new version infers as `str` no longer accepts `fillna(0)` or `.mean()`. Convert
  or filter explicitly; do not silence the error.
- If a fix depends on data you cannot see, say so in `needs_human` rather than guessing.

## Forbidden Actions

- Do not edit any file under `tests/`, `.github/`, `superset/migrations/`, or `requirements/`.
- Do not change dependency pins or the version range.
- Do not run the full test suite. Run the acceptance commands only.
- Do not open more than one pull request. Do not merge anything.
- Do not use `black`; the repository uses `ruff`.

## Required from User

- The ticket with its site table and acceptance commands.
- The repository, the base branch, and the two library versions.
- The pull request template, if the repository has one.
