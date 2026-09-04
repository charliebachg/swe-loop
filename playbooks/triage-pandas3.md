# Triage a dependency-upgrade ticket

## Overview

You are scoping, not fixing. Given a ticket about a major-version dependency upgrade in this
repository, decide what the work is, how to verify it, and whether it fits one session or must
be split. The result is a structured verdict. You will not change any code.

## Procedure

1. Read the ticket body. If it contains a `swe-loop work order` block, treat its `files`,
   `tests` and `acceptance` as given and verify them rather than re-deriving them.
2. Read the inventory file named in the ticket, if any. Each row is a call site with the test
   that exposed it and the library's own message about what changes.
3. For each call site, read the surrounding function. Decide whether the fix is prescribed by
   the library's message (mechanical) or depends on surrounding context (semantic).
4. Group call sites by module and by class of change. A group is a candidate shard.
5. For each group, decide whether one session can finish it in under three hours with a clear
   acceptance command. If not, split it and say why.
6. Name the acceptance command for each group as a pytest invocation over the impacted tests
   only, with warnings promoted to errors, and a second invocation on the new library version.
7. List anything that needs a person under `needs_human`, and say which kind it is with
   `blocking`. Set `blocking: true` when a session must not do the work at all: the site is a
   test or CI file, or the change is one the repository reserves for a person. Set
   `blocking: false` when the work can be done but someone should sign it off, for example a
   semantic change the named tests would not catch. A note asking for a second opinion is not a
   refusal: the change is still written, and the review is forced.
8. Provide the structured output with `is_final=true`. The session is done when that call has
   been made. Do not open a pull request. Do not push.

## Specifications

- Every call site in the ticket appears in exactly one shard or in `needs_human`.
- Every shard has an acceptance command that exits non-zero today and is expected to exit zero
  after the fix.
- `est_size` reflects your own estimate of a single session's effort: XS under 30 minutes, S
  under an hour, M under three hours. Anything larger must be split.
- `context_sufficient` is false, with `missing` filled in, if you could not determine the fix
  for any site from the code and the library's notes alone.

## Advice and Pointers

- The library's warning message usually names the replacement. Quote it in `prescribed_fix`.
- A test that passes on the new version but warned on the old one is a silent behaviour change,
  not a non-issue. Flag it for human review.
- Prefer shards that keep one file in one shard. Two sessions editing the same file will conflict.
- The repository formats Python with ruff, not black; lint config lives in `pyproject.toml`.

## Forbidden Actions

- Do not modify any file.
- Do not open, comment on, or close pull requests or issues.
- Do not install packages or change dependency files.
- Do not run the full test suite; run only the tests named for the sites you are scoping.

## Required from User

- The ticket text, including its work order block when present.
- The inventory path, or the statement that there is none.
- The repository, branch, and the two library versions.
