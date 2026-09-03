# swe-loop

Tickets in, verified pull requests out.

A loop on the Devin API for the class of work that dependency bots can open but cannot
finish: major-version upgrades. Devin scopes each ticket and writes the fix. Code decides what
happens next: it routes the ticket, checks the fix from a clean checkout, asks Devin to review
it, and reports. A person merges. Nothing reaches the base branch on the AI's word alone.

The app calls itself Backstop, because that is its job: it stands behind the AI and catches
what should not go through.

Demonstrated on `apache/superset`'s pandas 2.3.3 to 3.0.5 bump
([apache/superset#42671](https://github.com/apache/superset/pull/42671)): a three-line
dependency PR that had been open for a month with the test suite red, because the migration
behind it had never been scoped. Superset's own test suite, once its warning filter is switched
on, narrows 351 static `pd.*` call sites to 25 that actually change behaviour: 10 in product
code, 15 in test expectations. Those became the tickets.

## What it did

Five tickets on the fork, four pull requests, four merges, one refusal. Every line below is a
row in the app and a record in `data/replay/run.json`.

| ticket | scope | route | Devin session | gate | Devin Review | result |
|---|---|---|---|---|---|---|
| [#4](https://github.com/charliebachg/superset/issues/4) D | `models/helpers.py`, 1 site | Devin | [58d404d2](https://app.devin.ai/sessions/58d404d2369545e3803db32660cb84ba), 6 min | pass | no issues | [#7](https://github.com/charliebachg/superset/pull/7) merged `284769ae` |
| [#2](https://github.com/charliebachg/superset/issues/2) B | pivot, rolling, utils, 3 sites | Devin | [0f160096](https://app.devin.ai/sessions/0f1600968c9c47c18012fe8476f4f1e7), 12 min | pass, twice | 3 remarks, sent back; 4 on the revision | [#8](https://github.com/charliebachg/superset/pull/8) merged `4b99c166` |
| [#3](https://github.com/charliebachg/superset/issues/3) C | aggregate, histogram, resample, 3 sites | Devin | [38e2b6e7](https://app.devin.ai/sessions/38e2b6e7d3994aa980d7379031b9117c), 12 min | pass, twice | 2 remarks, sent back; 3 on the revision | [#9](https://github.com/charliebachg/superset/pull/9) merged `9d5a9f4f` |
| [#1](https://github.com/charliebachg/superset/issues/1) A | `charts/client_processing.py`, 3 sites | human first, then Devin | [feb8bfeb](https://app.devin.ai/sessions/feb8bfeb37ce445989356d0a7c4a1f71), 6 min | pass | 1 remark | [#10](https://github.com/charliebachg/superset/pull/10) merged `f36fa91c` |
| [#5](https://github.com/charliebachg/superset/issues/5) E | 15 test expectations in 9 files | human only | none | | | open, for a person |

The one to read is A. Its triage session read the ticket and declined: the three sites sat
behind tests that encode pandas 2 behaviour, and sessions are not allowed to edit tests, so the
verdict was `needs_human: 3` and the router filed it as human-only. A maintainer answered on
the ticket with the three prescribed fixes and confirmed the tests could stay as they were. The
answer woke the same triage session, which re-scoped the ticket into one work order in under
two minutes; the repair session opened its PR six minutes later and the gate passed it. The
system did not guess; it asked, and it remembered the conversation.

E was refused outright and stays refused. Fifteen test files assert pandas 2 results, and
whether a test or the code is wrong is a product decision. The Report shows it as the one
ticket of five that went to a person, with the reason.

Cost, for the ten sessions: 56 minutes of active AI work. The console prices seven of them at
$14.78; the loop's own minute rate puts all ten at $18.58. That is the price of four merged
fixes and one correct refusal.

## Run it

Replay mode needs no credentials. It renders the app from the recorded run.

```
docker compose up
# open http://localhost:8000
```

Or without Docker:

```
uv venv && uv pip install -e ".[dev]"
python -m swe_loop seed      # fills an empty store from the recorded run
python -m swe_loop serve     # the app on :8000, intake on /intake/github
```

Live mode needs a Devin org-scoped service user key and a GitHub token that can read the fork.
Copy `.env.example` to `.env`, fill it in, set `SWE_LOOP_MODE=live`, then:

```
python -m swe_loop apply-config --dry-run   # what would be created on the org; creates nothing
python -m swe_loop apply-config             # the playbooks and Knowledge notes, once, idempotent
python -m swe_loop seed --as-new            # the fork's open tickets, untriaged
python -m swe_loop triage                   # one triage session per new ticket
python -m swe_loop triage --ticket tkt_A --answer "..."   # answer a question; wakes the same session
python -m swe_loop run                      # route, dispatch, poll, gate, review, reduce: one pass
python -m swe_loop review-followup --ticket tkt_B         # send Devin Review's remarks back, re-gate
python -m swe_loop cost --set 58d404d2=1.78 # the console's figure for a session
python -m swe_loop record data/replay/run.json            # capture the run for replay, redacted
```

Or open Automations and click Run: it is the same chain from a button, and the pages refresh
while it works. Without `DEVIN_API_KEY` the mode is forced to replay. No key, no sessions.

## What happens, in order

```
event (GitHub webhook: a dependency bot's PR, a labelled issue, a failed check)
  │
  ▼ intake            code      any event becomes one ticket; one adapter per source
  ▼ triage session    Devin     reads the ticket, decides one session or several, or asks
  ▼ ticket store      SQLite    the row exists before any session does
  ▼ route             code      policy from configs/*.yaml: refuse, human-only, or Devin, with the reason
  ▼ shard             code      one file in one shard; caps by files and call sites
  ▼ repair sessions   Devin     parallel, bounded by max_acu_limit, typed by structured_output_schema
  ▼ gate              code      T0: the tests were not touched, the change is in scope
                                 T1: every acceptance command re-run from a clean checkout, by this process
  ▼ Devin Review      Devin     requested only on work the gate passed; remarks go back to the session
  ▼ merge             code      cross-shard conflicts; a person records the merge
  ▼ report            code      the app: every number is a query
```

The gate never trusts a session's own report. It checks out the PR head into a detached
worktree, diffs it against the base for the paths a session may not touch (`tests/`,
`.github/`, migrations, dependency files), and runs the ticket's acceptance commands itself.
Evidence is bound to the tree hash it was produced on; evidence from any other tree is
invisible. A session that finishes without structured output is a failure, not a pass.

Two things a session can do that the loop handles without a person: ask a question, and
receive review remarks. A triage session that needs a decision ends its turn waiting; the
ticket shows the question, and the answer goes to that same session, which keeps its context.
Devin Review's remarks on a passed PR are posted back to the repair session that opened it; the
revised head is gated again before anyone sees it.

## The app

One process, one SQLite file, a sidebar. Written for someone who has never run an AI coding
agent: each step says who does it, the AI or a person, and every session is priced.

| page | shows |
|---|---|
| **Home** | the last run in five steps, what needs a person now with the buttons to answer, merge or dismiss, what is running, and what just happened; the equivalent engineer-hours line explains its basis on hover |
| **Automations** | how work enters and what happens when it runs. The default automation, Issues from the fork, is on: Run pulls the repository's open issues with the label, makes a ticket of each new one, starts one triage session per ticket, routes them, starts the repair sessions, checks every PR from a clean checkout and asks Devin Review; the page follows along and keeps each run's history. Add your own with a trigger, a playbook and a session limit. Scan, a session that finds the work itself, is listed for the next version |
| **Tickets** | every ticket in two views. The list groups them by source (General, Scan Agent, Others) and gives each a one-line account of what it is doing right now, with its live session and its PR a click away. The pipeline view shows the eight steps per ticket, who does each, and where it stands. Open a ticket for its scope, the sessions, the gate's receipts, the review and the merge control |
| **Report** | the funnel from sites to merges, cost per verified change, where the gate said no, the burn-down against the inventory, session sizes, receipts, tripwires, routing and refusals |
| **Devin · Sessions** | every session with its status, minutes, dollars and size; click a row for its timeline, structured output and verdicts |
| **Devin · Playbooks** | the procedures sessions follow, with their structured output schema and last output; add one, attach it to an automation |
| **Settings** (gear) | the connected repository, the budget caps, the console's dollars per session, live connection checks |

Further pages sit at `/devin/knowledge`, `/devin/review`, `/devin/insights`, `/devin/integrations`
and `/devin/next`: the notes sessions retrieve, the review results, Session Insights, the org's
side of the integration, and where Computer Use, DeepWiki, Security Swarm and MCP would plug in.

The Report answers the question an engineering leader asks: *how would I know this is
working?* The app measures the system, never a named engineer; the person who merges is
recorded as a hash.

## Cost on a self-serve plan

Self-serve Devin plans are billed in dollar credits. The API reports `acus_consumed` as 0.0 for
every session and the consumption endpoints return 0, verified on this organisation. So the loop
measures cost itself: active minutes from its own polls (the gaps between polls while a session
reported `working`, each gap capped at 60 seconds) times a rate per session kind. The console's
per-session figures, entered on Settings or with `cost --set`, replace the computed dollars for
those sessions and refine the rate for the rest. On an ACU-metered plan the same pages show ACU.

## The seam

Everything specific to a target lives in one file, `configs/superset-pandas3.yaml`: the
repository and base branch, the trigger match, the acceptance commands, the router policy
(forbidden paths, coverage gates, human-only classes, shard caps), and the session caps.
`configs/example-minimal.yaml` is a second seam; the router tests run against both to keep the
code target-agnostic.

On the fork the base branch is `master`, not Dependabot's: that branch was 762 commits behind
by the time the work started, so the fixes land where the upgrade will. The fork carries two
files for the sessions, `.agents/skills/pandas3-repair/SKILL.md` and `.github/devin_pr_template.md`,
and the six issues the loop worked from.

## Layout

```
swe_loop/          one module per step: intake, triage, router, shard, dispatch, poll, gate, followup, reduce, report
  devin.py         the v3 client behind a transport; the fake transport replays fixtures
  store.py         the ticket store: SQLite, WAL, the headline queries
  cost.py          active minutes and dollars per session
  v2.py            the view models behind every page
  charts.py        inline SVG: bars, funnel, dot strip, squares, stacked bar, histogram
  detect/          the pandas warning detector that built the inventory
configs/           the seams
playbooks/         triage and repair playbooks
schemas/           structured output contracts (draft-07, self-contained)
knowledge/         repo-pinned Knowledge notes with trigger descriptions
fork_files/        the skill and the PR template committed to the target fork
data/inventory/    the measured inventory and the tickets drafted from it
data/replay/       the recorded run, redacted
templates/         the app
tests/             129 tests; the gate is tested on a real git fixture
```

## Development

```
uv pip install -e ".[dev]"
pytest -q
ruff check . && ruff format --check .
```

Built with coding assist tools, with Fable.
