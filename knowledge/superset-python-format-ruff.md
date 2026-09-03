---
name: "Superset Python formatting: ruff, not black"
trigger_description: "When formatting or linting Python in apache/superset, or when a pre-commit hook or CI lint check fails on Python style"
---
Superset formats Python with `ruff format` and lints with `ruff check`. There is no black hook; several docs still say otherwise, read the config not the prose. Run `ruff format <files>` and `ruff check <files>` on changed files before committing. Configuration is in `pyproject.toml` under `[tool.ruff]`.
