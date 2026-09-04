# swe-loop

[![tests](https://github.com/charliebachg/swe-loop/actions/workflows/ci.yml/badge.svg)](https://github.com/charliebachg/swe-loop/actions/workflows/ci.yml)

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

Every row below comes out of the store, printed by `python -m swe_loop receipts`, so this table
and the app cannot drift apart. It is the current state, not a highlight reel: three changes
written and checked, two of them merged by a person, one waiting for a person to merge, and one
ticket the system refused to touch.

| ticket | issue | scope | went to | session | checks | Devin Review | where it is |
|---|---|---|---|---|---|---|---|
| #00001 | [#1](https://github.com/charliebachg/superset/issues/1) | `client_processing.py` | Devin | [feb8bfeb](https://app.devin.ai/sessions/feb8bfeb37ce445989356d0a7c4a1f71) | passed | 1 comment | waiting for a person, [#13](https://github.com/charliebachg/superset/pull/13) |
| #00002 | [#2](https://github.com/charliebachg/superset/issues/2) | `pivot.py` and 2 more | Devin | [0f160096](https://app.devin.ai/sessions/0f1600968c9c47c18012fe8476f4f1e7) | passed | 4 comments | merged, [#8](https://github.com/charliebachg/superset/pull/8) |
| #00003 | [#3](https://github.com/charliebachg/superset/issues/3) | `aggregate.py` and 2 more | Devin | [38e2b6e7](https://app.devin.ai/sessions/38e2b6e7d3994aa980d7379031b9117c) | passed | 3 comments | merged, [#9](https://github.com/charliebachg/superset/pull/9) |
| #00004 | [#5](https://github.com/charliebachg/superset/issues/5) | not scoped | a person | none | not run | not requested | with the team |

The one to read is `#00001`. Its scoping session read the ticket and declined: the three places
sat behind tests that encode pandas 2 behaviour, and a session may not edit tests, so it returned
three notes and the router filed it for a person. A maintainer answered on the issue with the
three prescribed fixes and confirmed the tests could stay as they were. That answer woke the same
session, which re-scoped the ticket in under two minutes; the repair session opened its pull
request six minutes later and the checks passed it. The system did not guess. It asked, and it
kept the conversation.

`#00004` went to the team and stays there. Fifteen test files assert pandas 2 results, and
whether a test or the code is wrong is a product decision. A session may not edit tests, so it
said so and stopped rather than making the tests agree with itself. The Report shows it as the
ticket that went to a person, with the reason.

Issue #4 has no row because its shard is deliberately sitting at its unfixed state. It was
repaired, merged, then put back by `reset-shard` so the whole loop can be run again from an open
issue without anyone editing the store by hand. That is what Settings does, and it is the only
honest way to demonstrate a loop whose work is already done.

The scan is the second way in. Pointed at the repository rather than at a ticket, one session
read the pandas-importing modules and filed three findings as tickets in its own group. Two
matched places the measured inventory already knew about, which is the check on it; one, at
`client_processing.py:754`, it found on its own.

Measured, not asserted: 25 unit tests failed on pandas 3.0.5 before any of this ran. With the
loop's four changes merged, 11 of them pass. Every one of the 14 that still fail is in a test
file, which is the ticket the system refused to take and handed to a person. It fixed what it
took on and nothing it declined. The test ids, the command and the three commits are in
`data/inventory/2026-09-04-closing/`; run it yourself and you get the same numbers.

Cost so far: $14.97 across nine sessions, 48 minutes of active AI work, five of the nine priced
from the console and the rest at the rate those five imply. Every session counts, including the
ones that only read a ticket and the one that went to a person.

## Why an agent

**Detection was never the missing piece.** Three tools were already running on
`apache/superset#42671` and none of them moved it. Dependabot opened the pull request and stopped,
because its job ends where code changes begin; the repository's own `.github/dependabot.yml`
carries a hand-written list of major upgrades the bot must skip, React among them, each with a
comment saying the application does not support it yet. A review bot read the diff on day one and
described the migration correctly, naming the file and the line. CI went red and stayed red. A
month later the pull request was exactly where it started.

So the gap is not finding the work. It is doing it, and being able to trust what comes back.

**Doing it cannot be scripted, and this repository proves it rather than asserting it.** pandas
ships no official migration tool, and its own guidance is a loop: switch the deprecation warnings
on, fix what fires, run it again, review what is left. A codemod is open loop. It applies a rule
and exits, and it never sees the result.

The work order for `client_processing.py` carried pandas' own prescribed remedy, quoted out of the
warning text: call `result.infer_objects(copy=False)`. The acceptance command rejected it. On 2.3.3
the `FutureWarning` is raised inside `replace` before any chained call can run, and `copy=` is
itself deprecated on 3.0.5. The session iterated to
`df.mask(df == "SUPERSET_PANDAS_NAN").infer_objects()`, clean on both versions
([#13](https://github.com/charliebachg/superset/pull/13)). The published rule is right in general
and wrong in this file. No static analysis finds that, because finding it means running the test
and reading what came back.

**And the work waits.** A change here opens a pull request, waits for checks, collects review
remarks, and revises. On `#00002` and `#00003` Devin Review's remarks went back into the repair
session that wrote the code, which revised, and the gate re-ran the acceptance commands before
anyone saw it. `#00001` waited on a person: its scoping session declined and asked a question, a
maintainer answered on the issue, and that answer woke the same session, which re-scoped in under
two minutes with everything it already knew. A process that exits when its command finishes cannot
do either.

**The gate is what makes the volume safe.** It is easy to point a model at 351 call sites and
produce 351 pull requests nobody can review. Every change here is checked out into a worktree the
session cannot write to, diffed for the paths a session may not touch, and run against the ticket's
own acceptance commands on both pandas versions. Work that does not pass never reaches a person.
That is the property that makes it reasonable to run sessions in parallel at all, and it is why
sessions are bounded by cost and shards are disjoint by file.

There was also no scan to consume. `pytest.ini` sets `filterwarnings = ignore`, so the instrument
that would have produced the finding was switched off. Step zero was building the detector,
`swe_loop/detect/`, which turns 351 static call sites into the 25 that change behaviour and hands
each one to a session with the test that surfaced it attached.

None of this is an argument that the agent should be trusted, and most of this repository is
deliberately not an agent. The detector, the router, the shard caps, the gate and every number on
the Report are code. Ticket `#00004` is the other half of the same point: fifteen test files assert
pandas 2 results, whether a test or the code is wrong is a product decision, and the system refused
it and said why. The agent is the part that reads a failing test and decides what to do about it.
Everything around it exists to check that decision.

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

Live mode needs three things: a Devin org-scoped service user key, a GitHub token that can read
and write the fork, and a local clone of the fork for the gate. The gate checks each pull request
out into a detached worktree of that clone and runs the acceptance commands through its own
interpreters, so the clone lives at `gate.repo_root` in the seam (by default `../superset-fork`,
beside this repository) and carries `.venv-p2` and `.venv-p3`, built as
`knowledge/superset-pandas-test-environments.md` describes. Settings shows whether it is ready
before a run, and a live run that cannot verify says so on the ticket instead of implying a pass.
In Docker, point `SWE_LOOP_FORK` at a clone whose two environments were built inside the
container.

Copy `.env.example` to `.env`, fill it in, set `SWE_LOOP_MODE=live`, then:

```
python -m swe_loop apply-config --dry-run   # what would be created on the org; creates nothing
python -m swe_loop apply-config             # the playbooks and Knowledge notes; the first live run does this itself
python -m swe_loop seed --as-new            # the fork's open tickets, untriaged
python -m swe_loop triage                   # one triage session per new ticket
python -m swe_loop triage --ticket tkt_A --answer "..."   # answer a question; wakes the same session
python -m swe_loop run                      # route, dispatch, poll, gate, review, reduce: one pass
python -m swe_loop review-followup --ticket tkt_B         # send Devin Review's remarks back, re-gate
python -m swe_loop cost --set 58d404d2=1.78 # the console's figure for a session
python -m swe_loop reset-shard --shard D    # put one shard back to its unfixed state, on the fork and in the store
python -m swe_loop record data/replay/run.json            # capture the run for replay, redacted
python -m swe_loop receipts                 # the table at the top of this file, from the store
```

Or open Automations and click Run: it is the same chain from a button, and the pages refresh
while it works. Without `DEVIN_API_KEY` the mode is forced to replay. No key, no sessions.

## What happens, in order

```
event (GitHub webhook: a dependency bot's PR, a labelled issue, a failed check)
  │
  ▼ intake            code      any event becomes one ticket; one adapter per source
  ▼ scan session      Devin     the other way in: reads the repository and files what it finds
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
| **Home** | the last run in five steps, what needs a person now with the buttons to answer, merge or dismiss, what is running, and what just happened |
| **Automations** | how work enters and what happens when it runs. Issues from the fork is on: Run pulls the repository's open issues with the label, makes a ticket of each new one, starts one triage session per ticket, routes them, starts the repair sessions, checks every pull request from a clean copy and asks Devin Review. Scan the repository is the other way in: a session reads the repository itself, finds places the upgrade changes behaviour, and files each as a ticket that goes through the same loop. Both keep a run history, and you can add your own |
| **Tickets** | every ticket, named by a number, in two views. The list groups them by source and gives each a one-line account of what it is doing right now. The pipeline view shows four steps per ticket: scoped, fixed, verified, merged. Open a ticket for what it covers, the sessions, the checks, the review and the merge button |
| **Report** | three rates, each a count over a stated denominator: verification pass rate, human intervention rate, acceptance rate. Under them six lightweight numbers to watch: sessions run, checks re-run here, output rejected for shape, work sent back to a session, AI working time per change, cost per change that passed. Under them every check with its exit code and its log, where the work went, progress against the counted list, what it cost, and the log itself |
| **Devin · Sessions** | every session: the ticket, what it was for, its status, cost, whether the checks passed, when it started and how long it took. Click a row for its timeline, the checks and what it claimed |
| **Devin · Playbooks** | the instructions each kind of session follows and the shape its answer must have. Add one and attach it to an automation |
| **Devin · Knowledge** | the notes a session is given about the repository, when each is read, and which of them are on the organisation where a session can reach them |
| **Devin · Insights** | the sessions as Devin records them: how many messages each took, how big it judged the piece, what it says the work touched, which sessions ran with no playbook, and its own analysis of what to change. Nothing on this page is our measurement, which is what separates it from the Report |
| **Settings** (gear) | the connected repository, what a session may never touch, the budget caps, the console's dollars per session, live connection checks, and Rerun a shard: one button puts a fixed shard back to its unfixed state on the repository and in the store, so the next Run does the whole thing again for real |

The Report answers the question an engineering leader asks: *how would I know this is
working?* Every number is a count with its denominator beside it, never a percentage on its own
and never a percentile: the run is small and the page says so. It also says plainly what it
cannot tell you, and which numbers it refuses to show, with the reason for each. The app
measures the system, never a named engineer; the person who merges is recorded as a hash.

## Cost on a self-serve plan

Self-serve Devin plans are billed in dollar credits. The API reports `acus_consumed` as 0.0 for
every session and the consumption endpoints return 0, verified on this organisation. So the loop
measures cost itself: active minutes from its own polls (the gaps between polls while a session
reported `working`, each gap capped at 60 seconds) times a rate per session kind. That makes the
figure a floor rather than a total: a session this app never polled to the end, because it was
terminated or the process watching it died, counts as less than it was, and the Report says so. The console's
per-session figures, entered on Settings or with `cost --set`, replace the computed dollars for
those sessions and refine the rate for the rest. On an ACU-metered plan the same pages show ACU.

## What it runs, and where

The acceptance commands are shell strings, and they come from the scoping session's structured
output, which is a model's writing. The loop runs them, so treat this as you would a build
script from a pull request: point it at a repository you trust, run it in the container rather
than on your laptop, and give the GitHub token only the repository it works on. The commands run
in a detached worktree that the session has no access to, which stops a session marking its own
homework; it does not sandbox the command itself.

The dashboard has no sign-in, and its buttons merge to GitHub with whatever token is loaded, so
`compose.yaml` publishes it on loopback only. Change that only on a network you trust.

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
playbooks/         the triage, repair and scan playbooks
schemas/           structured output contracts (draft-07, self-contained)
knowledge/         repo-pinned Knowledge notes with trigger descriptions
fork_files/        the skill and the PR template committed to the target fork
data/inventory/    the measured inventory and the tickets drafted from it
data/replay/       the recorded run, redacted
templates/         the app
static/            htmx, served by the app so the dashboard works with no internet
tests/             163 tests; the checks are tested against a real git fixture
```

## Development

```
uv pip install -e ".[dev]"
pytest -q
ruff check . && ruff format --check .
```

Built with coding assist tools, with Fable.
