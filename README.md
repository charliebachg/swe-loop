# swe-loop

Tickets in, verified pull requests out.

A session-then-gate loop on the Devin API for the class of work that dependency bots can open
but cannot finish: major-version upgrades. Demonstrated on `apache/superset`'s pandas 2.3.3 to
3.0.5 bump.

Every Devin session is followed by a deterministic layer that decides what happens next. Devin
scopes and repairs; code routes, verifies, and reports. A human merges.

Run and simulate instructions, the architecture, and the dashboard are documented below as the
project fills in.

## Status

Skeleton. The first artefact is in `data/inventory/`: the warning inventory the target
repository's own test suite produces once its warning filter is switched on.
