---
name: "Superset Python lockfiles are compiled with uv, never hand-edited"
trigger_description: "When a change touches Python dependencies, pyproject.toml, or anything under requirements/"
---
Never hand-edit `requirements/*.txt`; they are compiled by `./scripts/uv-pip-compile.sh` and lockfile drift fails the build. For a migration ticket, do not change dependency pins at all: the dependency bump is a separate PR, and the version range's lower bound does not move.
