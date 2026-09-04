# swe-loop

[![tests](https://github.com/charliebachg/swe-loop/actions/workflows/ci.yml/badge.svg)](https://github.com/charliebachg/swe-loop/actions/workflows/ci.yml)

Tickets in, verified pull requests out.

A loop on the Devin API for the class of work that dependency bots can open but cannot
finish: major-version upgrades. Work arrives three ways: an issue on the repository, a session
sent to read the repository, or Devin's own code scanner. Devin scopes each ticket and writes
the fix. Code decides what happens next: it routes the ticket, checks the fix from a clean
checkout, asks Devin to review it, and reports. A person merges. Nothing reaches the base
branch on the AI's word alone.

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
and the app cannot drift apart. It is the current state, not a highlight reel.

| ticket | issue | scope | went to | session | checks | Devin Review | where it is |
|---|---|---|---|---|---|---|---|
| #00001 | [#1](https://github.com/charliebachg/superset/issues/1) | `client_processing.py` | Devin | [feb8bfeb](https://app.devin.ai/sessions/feb8bfeb37ce445989356d0a7c4a1f71) | passed | 1 comment | waiting for a person, [#13](https://github.com/charliebachg/superset/pull/13) |
| #00002 | [#2](https://github.com/charliebachg/superset/issues/2) | `pivot.py` and 2 more | Devin | [0f160096](https://app.devin.ai/sessions/0f1600968c9c47c18012fe8476f4f1e7) | passed | 4 comments | merged, [#8](https://github.com/charliebachg/superset/pull/8) |
| #00003 | [#3](https://github.com/charliebachg/superset/issues/3) | `aggregate.py` and 2 more | Devin | [38e2b6e7](https://app.devin.ai/sessions/38e2b6e7d3994aa980d7379031b9117c) | passed | 3 comments | merged, [#9](https://github.com/charliebachg/superset/pull/9) |
| #00004 | [#5](https://github.com/charliebachg/superset/issues/5) | not scoped | a person | none | not run | not requested | with the team |
| #00009 | filed by a scan | `core.py` | Devin | [5308ec47](https://app.devin.ai/sessions/5308ec47521f4fd68d0c85ab09e6aece) | passed | 0 comments | merged, [#14](https://github.com/charliebachg/superset/pull/14) |
| #00010 | filed by a scan | `result_set.py` | Devin | [03f72c31](https://app.devin.ai/sessions/03f72c310aa7448b890a96377eae5edd) | passed | no issues | merged, [#15](https://github.com/charliebachg/superset/pull/15) |
| #00011 | filed by a scan | `dataframe_utils.py` | Devin | [0b92cade](https://app.devin.ai/sessions/0b92cadef9fe4cb792270c54bc57f47b) | passed | no issues | merged, [#16](https://github.com/charliebachg/superset/pull/16) |
| #00012 | filed by a scan | `api.py` | a person | [61fb977b](https://app.devin.ai/sessions/61fb977bc53d4f72bfcdfede83171e3f) | passed | 0 comments | merged, [#17](https://github.com/charliebachg/superset/pull/17) |
| #00013 | filed by a scan | `views.py` | a person | [3e5f60a5](https://app.devin.ai/sessions/3e5f60a5c6164cfbbd93fd55ffe56391) | passed | no issues | merged, [#18](https://github.com/charliebachg/superset/pull/18) |
| #00016 | filed by a scan | `csv.py` | Devin | [dcdde9fa](https://app.devin.ai/sessions/dcdde9fa3d904261a66506391986bb8c) | passed | 1 comment | merged, [#19](https://github.com/charliebachg/superset/pull/19) |
| #00017 | filed by a scan | `slack_mixin.py` | Devin | [c6e85826](https://app.devin.ai/sessions/c6e858267bc54d3ea633c664fcfe37f6) | passed | see the pull request | merged, [#20](https://github.com/charliebachg/superset/pull/20) |
| #00018 | filed by a scan | `boxplot.py` | Devin | [1e7ba413](https://app.devin.ai/sessions/1e7ba41345b1442bb37aaf8e671afcf4) | passed | see the pull request | merged, [#21](https://github.com/charliebachg/superset/pull/21) |
| #00019 | filed by a scan | `compare.py` | Devin | [b9ee6224](https://app.devin.ai/sessions/b9ee6224f7864b04bc8ffe1199037d4b) | passed | no issues | merged, [#22](https://github.com/charliebachg/superset/pull/22) |

Nineteen tickets from three ways in, which is the point of the shape rather than a count: four
from the fork's own issues, ten from a session pointed at the repository, five from Devin's own
scanner. Twelve changes were written and checked; twelve passed. 11 were merged by a person,
1 is waiting on one, and 3 tickets were refused before anything was spent on them.

The one to read is `#00001`. Its scoping session read the ticket and declined: the three places
sat behind tests that encode pandas 2 behaviour, and a session may not edit tests, so it returned
three notes and the router filed it for a person. A maintainer answered on the issue with the
three prescribed fixes and confirmed the tests could stay as they were. A scoping session started
with that answer and re-scoped the ticket in under two minutes; the repair session opened its pull
request six minutes later and the checks passed it. The system did not guess. It asked, and it
kept the conversation.

`#00004` went to the team and stays there. Fifteen test files assert pandas 2 results, and
whether a test or the code is wrong is a product decision. A session may not edit tests, so it
said so and stopped rather than making the tests agree with itself.

`#00019` is the one that could not be checked, which is more useful than one that failed. Its
scoping session wrote `-W error::PerformanceWarning`, and `-W` takes a builtin name or a dotted
path: the class lives at `pandas.errors.PerformanceWarning`, so pytest rejected the filter and
nothing ran. The gate recorded that as work it could not verify, never as a pass, and the ticket
went to a person. A person corrected the command, the checks were re-run on the same change and
passed on both versions, and it merged. The change had been right the whole time; the instrument
was broken, and the system said so rather than guessing either way.

`#00012` and `#00013` came from Devin's scanner and were fixed by Devin's own remediation, one
call per finding. Both still went to a person first, because this repository requires an
automated security finding to name the capability row in `SECURITY.md` it violates and the
principal the attacker holds, and to be filed as a question when it cannot name both. The scanner
returns neither field, so every security finding here is a question until someone answers it.
Three still are.

Measured, not asserted: 25 unit tests failed on pandas 3.0.5 before any of this ran. With the
loop's first four changes merged, 11 of them pass. Every one of the 14 that still fail is in a
test file, which is the ticket the system refused to take and handed to a person. It fixed what
it took on and nothing it declined. The test ids, the command and the three commits are in
`data/inventory/2026-09-04-closing/`; run it yourself and you get the same numbers.

Cost: $62.75 across 29 sessions and 160 minutes of active AI work. Twenty-six are priced from
the console and the rest at the rate those imply. One cannot be priced at all: Devin's code scan
runs sub-sessions this app never polls and the console does not itemise them, so the app says
"not priced" rather than a false zero. Every session counts, including the ones that only read a
ticket and the ones that went to a person.

## Why an agent

**Detection was never the missing piece.** Three tools were already running on
`apache/superset#42671` and none of them moved it. Dependabot opened the pull request and stopped,
because its job ends where code changes begin. GitHub says as much about its own bot: some updates
[need code changes across your project](https://github.blog/changelog/2026-04-07-dependabot-alerts-are-now-assignable-to-ai-agents-for-remediation/),
and a coding agent picks up where Dependabot leaves off. This repository already knows it too: its
own `.github/dependabot.yml` carries a hand-written list of major upgrades the bot must skip, React
among them, each with a comment saying the application does not support it yet. A review bot read
the diff on day one and described the migration correctly, naming the file and the line. CI went red and stayed red. A
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
maintainer answered on the issue, and a scoping session started with that answer and re-scoped in
under two minutes with everything it already knew. A process that exits when its command finishes cannot
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

## What this uses of Devin

Devin is the worker at every step that needs judgement, and the primitives are used as they
ship rather than reimplemented:

| | |
|---|---|
| **Sessions** (v3) | scoping, repair, and the scan, each with a playbook, a structured output contract and `max_acu_limit` |
| **Playbooks** | the instructions per kind of session, editable on the Playbooks page |
| **Knowledge notes** | repo-pinned, trigger-retrieved, created on the organisation by `apply-config` |
| **Structured output** | a draft-07 schema per session kind. A terminal session with no structured output is a failure, not a pass |
| **Devin Review** | requested on work the checks passed; its remarks are posted back into the session that wrote the code |
| **Code scans** | Devin ships a scanner, so this runs it rather than describing one. A scan is started with an *area* to look in, never a defect to look for |
| **Remediation** | `findings/{id}/remediate`: Devin fixes its own finding and opens the pull request |
| **Auto-scan schedules** | the recurrence is Devin's, registered against the scan and backed by an Automation on the organisation. This app does not keep a timer of its own |
| **Session Insights** | Devin's own read of each session, on its own page, kept separate from anything this app measures |

**The schedule is theirs, and the watching is ours.** `POST` on a scan's `auto-scan` registers a
recurrence and Devin backs it with an Automation. It is created switched off, and the switch on
the Automations page moves Devin's, then shows what Devin answered. Devin has no outbound
webhook, so `swe-loop watch` polls: a tick that finds nothing does nothing, and never starts a
scan of its own, because that would make the timer beside the point. Because it reads state
rather than events, stopping the watcher loses nothing; the next tick picks up whatever the
schedule did meanwhile. The recurrence is when Devin looks, not
when it runs: a check that finds no commits since the last run spawns nothing, and one that does
spawns a full scan of the new commits. So a merge is what triggers the next scan, within the hour.
A run that extends a finished scan and finds nothing new changes no scan and no finding, so the
watcher also reads Devin's own sessions: one with `origin: automation`,
carrying the automation id Devin gave us and no parent, is one scheduled run, and it goes into the
run history with the time Devin says it started and finished. Otherwise a schedule that ran and
cleared the repository is indistinguishable from one that never fired. Sub-hourly recurrences need a Teams plan, so on a self-serve plan hourly
is the floor.

**Two Devin features this deliberately does not use, and why.** Devin Review with Autofix has no
API surface: `autofix` appears nowhere in the v3 spec and `PrReviewCreateRequest` carries one
field, `pr_url`. It also reacts to bots commenting on a pull request, and the checks here run
locally against a tree the session cannot reach, so there would be nothing for it to react to
without publishing the results as comments and letting the reviewed thing fix itself. Auto-Triage
is in the spec, as an automation action, but it requires a `slack_triage_config` with a source
channel and exactly one `slack:message` trigger; the sources here are issues, a scan session and
Devin's scanner. Where the API forces a choice this says so rather than claiming a preference.

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
python -m swe_loop triage --ticket tkt_A --answer "..."   # answer a question; wakes the ticket's session, or starts one with the answer
python -m swe_loop run                      # route, dispatch, poll, gate, review, reduce: one pass
python -m swe_loop review-followup --ticket tkt_B         # send Devin Review's remarks back, re-gate
python -m swe_loop cost --set 58d404d2=1.78 # the console's figure for a session
python -m swe_loop reset-shard --shard D    # put one shard back to its unfixed state, on the fork and in the store
python -m swe_loop record data/replay/run.json            # capture the run for replay, redacted
python -m swe_loop receipts                 # the table at the top of this file, from the store
python -m swe_loop schedule                 # hand the code scan's recurrence to Devin, switched off
python -m swe_loop schedule --on            # arm it;  --remove takes it off Devin again
python -m swe_loop watch                    # wait for Devin's schedule to fire, then run the loop
```

Or open Automations and click Run: it is the same chain from a button, and the pages refresh
while it works. Without `DEVIN_API_KEY` the mode is forced to replay. No key, no sessions.

## What happens, in order

```
event (GitHub webhook: a dependency bot's PR, a labelled issue, a failed check)
  │
  ▼ intake            code      any event becomes one ticket; one adapter per source
  ▼ scan session      Devin     the second way in: reads the repository and files what it finds
  ▼ code scan         Devin     the third: Devin's own scanner, on a recurrence Devin holds
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
ticket shows the question, and the answer goes to that ticket's session, which keeps its context
when it can still be woken; when it cannot, a fresh session starts with the answer in hand.
Devin Review's remarks on a passed PR are posted back to the repair session that opened it; the
revised head is gated again before anyone sees it.

## The app

One process, one SQLite file, a sidebar. Written for someone who has never run an AI coding
agent: each step says who does it, the AI or a person, and every session is priced.

| page | shows |
|---|---|
| **Home** | the last run in five steps, what needs a person now with the buttons to answer, merge or dismiss, what is running, and what just happened |
| **Automations** | how work enters and what happens when it runs, in three rows. Issues from repo pulls the repository's open issues with the label, makes a ticket of each new one, starts one scoping session per ticket, routes them, starts the repair sessions, checks every pull request from a clean copy and asks Devin Review. Scan agent points a session at the repository itself, which finds places the upgrade changes behaviour and files each as a ticket through the same loop. Devin security scan runs Devin's own scanner, on a recurrence registered on Devin and switched from here. All three keep a run history, and you can add your own |
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
tests/             198 tests; the checks are tested against a real git fixture
```

## Development

```
uv pip install -e ".[dev]"
pytest -q
ruff check . && ruff format --check .
```

Built with coding assist tools, with Fable.
