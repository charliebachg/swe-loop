---
name: "Superset frontend linting: oxlint and oxfmt, not eslint or prettier"
trigger_description: "When touching TypeScript or JavaScript under superset-frontend, or when a lint-frontend check fails"
---
Superset's frontend linting migrated off eslint and prettier to `oxlint` and `oxfmt`. Do not add eslint or prettier config or run them. Node is ^24 and npm ^11; `tsc` alone wants 8 GB of memory, so run it only when the ticket asks.
