# swe-loop

Tickets in, verified pull requests out.

A session-then-gate loop on the Devin API for the class of work that dependency bots can open
but cannot finish: major-version upgrades. Every Devin session is followed by a deterministic
layer that decides what happens next. Devin scopes and repairs; code routes, verifies and
reports; a person merges.

Demonstrated on `apache/superset`'s pandas 2.3.3 to 3.0.5 bump ([apache/superset#42671](https://github.com/apache/superset/pull/42671)):
a three-line dependency PR that had been open for a month with the test suite red, because the
migration behind it had never been scoped. The target's own test suite, once its warning
filter is switched on, narrows 351 static `pd.*` call sites to 25 that actually change: 10 in
product code, 15 in test expectations. Those became the tickets.

## Run it

Replay mode needs no credentials. It renders the dashboard from committed data.

```
docker compose up
# open http://localhost:8000
```

Or without Docker:

```
uv venv && uv pip install -e ".[dev]"
python -m swe_loop seed      # fills an empty store from the committed run, or synthesises one
python -m swe_loop serve     # dashboard on :8000, intake on /intake/github
```

Live mode needs a Devin org-scoped service user key and a GitHub token that can read the fork.
Copy `.env.example` to `.env`, fill it in, set `SWE_LOOP_MODE=live`, then:

```
python -m swe_loop apply-config   # creates the playbooks and Knowledge notes on the org, once
python -m swe_loop run            # route, dispatch, poll, gate, reduce: one pass over open tickets
python -m swe_loop record data/replay/run.json   # capture the run so replay shows real data
```

Without `DEVIN_API_KEY` the mode is forced to replay. No key, no sessions.

## What happens, in order

```
event (GitHub webhook: a dependency bot's PR, a labelled issue, a failed check)
  │
  ▼ L0  normalise            code      any event becomes one work order; one adapter per source
  ▼ L1  triage session       Devin     scopes the ticket, decides one session or several; does not patch
  ▼     ticket store         SQLite    the row exists before any session does
  ▼ L2  route                code      policy from configs/*.yaml; refuse, human-only, or Devin, with the reason
  ▼ L3  shard                code      one file in one shard; caps by files and call sites
  ▼ L4  repair sessions      Devin     parallel, bounded by max_acu_limit, typed by structured_output_schema
  ▼ L5  gate                 code      T0: the oracle was not touched, the change is in scope, the artefacts exist
                                        T1: every acceptance command re-run from a clean worktree, by this process
  ▼ L6  Devin Review         Devin     requested only on work the gate passed
  ▼ L7  reduce               code      cross-shard conflicts; a person records the merge
  ▼ L9  report               code      the dashboard: every number is a query, every tile shows its SQL
```

The gate never trusts a session's own report. It checks out the PR head into a detached
worktree, diffs it against the base for the paths a session may not touch (`tests/`,
`.github/`, migrations, dependency files), and runs the ticket's acceptance commands itself.
Evidence is bound to the tree hash it was produced on; evidence from any other tree is
invisible. A session that finishes without structured output is a failure, not a pass.

## What the dashboard answers

One question, the one an engineering leader asks: *how would I know this is working?*

1. **The answer.** Verified changes (gate passed and a person merged) over decided tickets; ACU
   per verified change, median and p95; self-reported versus verified; budget against cap.
2. **Status, success and failure, progress.** The ticket board; the funnel with every drop named;
   the inventory burn-down.
3. **How we know it fixed it.** Per session: the Devin session, the PR, T0, T1, the gate's
   result, retries, ACU, session size. Every cell links to its evidence.
4. **Tripwires, pre-registered.** Oracle touched; merged without a human; retries p95; ACU p95
   against the cap; review minutes and 30-day survival marked as not measured rather than claimed.
5. **The routing table.** Per class of change: attempted, verified, cost, and whether the verdict
   was autonomous, assisted, or human-only. The artefact a team would hand over after week one.
6. **Escalations, with reasons, and the metrics not shown on purpose:** lines of code, PRs
   opened, acceptance rate, share of AI-authored code, tokens, self-reported time saved.

The dashboard measures the system, never a named engineer.

## The seam

Everything specific to a target lives in one file, `configs/superset-pandas3.yaml`: the
repository and branch, the trigger match, the detector and acceptance commands, the router
policy (forbidden paths, coverage gates, human-only classes, shard caps), and the session caps.
`configs/example-minimal.yaml` is a second seam; the router tests run against both to keep the
code target-agnostic.

## Layout

```
swe_loop/          one module per layer: intake, triage, router, shard, dispatch, poll, gate, reduce, report
  devin.py         the v3 client behind a transport; the fake transport replays fixtures
  store.py         the ticket store: nine tables, WAL, the headline queries
  detect/          the pandas warning detector; builds the queue and re-runs in the gate
configs/           the seams
playbooks/         triage and repair playbooks, six sections each
schemas/           structured output contracts (draft-07, self-contained)
knowledge/         repo-pinned Knowledge notes with trigger descriptions
fork_files/        SKILL.md and the PR template committed to the target fork
data/inventory/    the measured inventory and the tickets filed from it
data/replay/       a recorded run for replay mode
templates/         the dashboard
tests/             80 tests; the gate is tested on a real git fixture
```

## Receipts

Filled in as the run happens: the tickets on the fork, the pull requests, the Devin sessions,
and the run that failed.

## Development

```
uv pip install -e ".[dev]"
pytest -q
ruff check . && ruff format --check .
```

Built with coding assist tools, with Fable.
