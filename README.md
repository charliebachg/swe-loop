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
python -m swe_loop apply-config --dry-run   # what would be created on the org, and what is already there; creates nothing
python -m swe_loop apply-config   # creates the playbooks and Knowledge notes on the org, once
python -m swe_loop triage         # one triage session per new ticket; the verdict, validated by code, becomes work orders
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

## The app

One process, one SQLite file, a sidebar of modules. Each module owns one kind of object; the
others link to it.

| module | owns |
|---|---|
| **Home** | what is happening now, what needs a person, what just happened |
| **Automations** | a list of configs for how work enters and how sessions are managed on it: trigger, target, playbook, cap per session, concurrency; Repair (run now) and Scan (next) are seeded, more can be added |
| **Tickets** | the tickets, grouped by source, with the router's decision and reason; a detail panel shows each ticket's sites, the library's messages and the acceptance commands |
| **Tracker** | each ticket's run through the layers: sessions as stages, the gate's receipts, the review stage, the merge control |
| **Report** | effectiveness: verified changes, cost per verified change, said-done versus verified, tripwires, the routing table, what is deliberately not shown |
| **Devin · Sessions** | every session as Devin sees it: status, ACU, size, parent and children, time left |
| **Devin · Playbooks** | a list of the procedures sessions follow, each with its structured output schema and last output; new ones can be added and attached to an automation |
| **Devin · Knowledge** | the notes a session retrieves by trigger, grouped by agent |
| **Devin · Insights** | Session Insights: size and ACU per session |
| **Devin · Review** | Devin Review requests, made only after a gate pass |
| **Devin · Integrations** | the org's side: GitHub App, secrets, security profile, snapshot |
| **Devin · Next** | Computer Use, DeepWiki, Security Swarm, a scan session, an evaluator, MCP: where each plugs in |
| **Settings** (gear) | our side: the connected repository, the seam, budget caps, live connection checks |

The Report answers the question an engineering leader asks: *how would I know this is
working?* Every number on it is a query; every tile shows its SQL on request. The dashboard
measures the system, never a named engineer.

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
templates/         the app: one template per module
tests/             90 tests; the gate is tested on a real git fixture
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

## Cost on a self-serve plan

Self-serve Devin plans are billed in dollar credits; the API reports `acus_consumed` as 0.0 for every session and the consumption endpoints return 0 (verified on this organisation). The pages therefore show minutes the AI was actively working, measured from the loop's own polls (gaps between polls while the session reported `working`, each gap capped at 60 s; `swe_loop/cost.py`). Enter the credits figure from the console (Settings, Plans) on the Settings page once and every minute is priced at that rate. On an ACU-metered plan the same pages show ACU.

Built with coding assist tools, with Fable.
