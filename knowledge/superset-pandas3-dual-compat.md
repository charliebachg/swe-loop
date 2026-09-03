---
name: "pandas 3 migration in Superset must stay compatible with 2.3.3"
trigger_description: "When fixing pandas FutureWarnings, copy-on-write, string dtype, or any pandas 3 breakage in apache/superset"
---
The maintainers asked that the pandas lower bound not move: the range is `>=2.3.3, <3.1`. Every fix must run on both 2.3.3 and 3.0.5. Prefer replacements the warning message names. `obj[col].method(inplace=True)` on a column selection is a silent no-op under copy-on-write; rewrite as assignment. Columns the new version infers as `str` reject numeric `fillna` and `.mean()`; convert explicitly. Never edit tests to make them pass; report the site instead.
