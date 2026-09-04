# Did the changes actually fix anything

`r3_failing_ids.txt` is the 25 unit tests that failed on pandas 3.0.5 before any of this ran,
taken from the measured inventory. Re-run them at three commits of the fork and the answer is
the same every time.

From the fork root, with the two environments built as
`knowledge/superset-pandas-test-environments.md` describes:

```
PYTHONHASHSEED=0 SUPERSET_SECRET_KEY=not-a-secret \
.venv-p3/bin/python -m pytest -c pytest.ini -p no:cacheprovider -q --tb=no -o addopts= \
  $(tr '\n' ' ' < r3_failing_ids.txt)
```

| commit | what it is | result |
|---|---|---|
| `452817f2a1` | before any fix | 25 failed |
| `9d5a9f4f29` | the four changes merged | 11 passed, 14 failed |
| `7969faf635` | master with one change re-offered and one put back, for a walk through of the merge step | 10 passed, 15 failed |

All 14 that still fail are in test files. They are the sites of the one ticket the system refused
to take, because a session may not edit tests, and they are open on the fork for a person to
decide. The loop fixed every product-code failure it took on, and none of what it declined.
