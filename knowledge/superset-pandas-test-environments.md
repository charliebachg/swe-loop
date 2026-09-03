---
name: "Superset test environments for the pandas 2.3.3 and 3.0.5 acceptance commands"
trigger_description: "When a ticket's acceptance commands name .venv-p2 or .venv-p3, or when running Superset unit tests under a specific pandas version"
---
The acceptance commands in swe-loop tickets run `.venv-p2/bin/python` (pandas 2.3.3, the version the
lockfile pins) and `.venv-p3/bin/python` (pandas 3.0.5). Build both in the repository root with uv,
once, before running any test. Python 3.12 is what the reference environments use.

```
uv venv .venv-p2 --python 3.12
uv pip install --python .venv-p2/bin/python -r requirements/development.txt
uv venv .venv-p3 --python 3.12
uv pip install --python .venv-p3/bin/python -r requirements/development.txt
uv pip install --python .venv-p3/bin/python "pandas==3.0.5"
```

`requirements/development.txt` installs the repository itself in editable mode (`-e .`), so no
separate install step is needed. If `mysqlclient` fails to build, install the system package
`default-libmysqlclient-dev` and retry, or drop it: the unit tests named in the tickets do not import it.
Do not edit `pyproject.toml`, `requirements/*` or the lockfiles to get pandas 3 in: the pin lives in
`.venv-p3` only, and the code must keep running on both versions.

Run tests as the ticket states them, from the repository root, with `-c pytest.ini -p no:cacheprovider -o addopts=`,
and with `-W error::FutureWarning` on the 2.3.3 environment so a warning counts as a failure.
