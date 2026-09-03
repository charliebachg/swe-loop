"""Pandas migration detector: build an inventory from junit runs of the test suite.

Used twice, unchanged: once to build the ticket queue (Plan), once in the gate (T1) to prove
a repair session removed what it claimed to remove.

  r1  pandas 2.3.3, default        -> baseline failures (excluded)
  r2  pandas 2.3.3, -W error::FutureWarning -> forward-looking call sites
  r3  pandas 3.0.5, default        -> actual breakage

Output: inventory.json (one row per finding), summary.md
"""

import json
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path

S = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else S

# pandas warning classes we expect from 2.3.x -> 3.0 (message fragments, case-insensitive)
CLASSES = [
    ("downcasting", r"downcasting"),
    ("fillna-method", r"fillna.*method|method=.*fillna|\.(ffill|bfill)\(\)"),
    ("inplace", r"inplace"),
    ("chained-assignment", r"chained assignment|ChainedAssignment|Copy-on-Write|copy_on_write"),
    ("str-dtype", r"string dtype|str dtype|StringDtype|infer_string|dtype == object|object dtype"),
    ("groupby-observed", r"observed=|observed=False"),
    (
        "datetime-unit",
        r"unit=|datetime64\[ns\]|resolution|to_datetime.*infer|infer_datetime_format",
    ),
    ("offset-alias", r"'[A-Z]+' is deprecated|offset alias|freq=.*deprecated"),
    ("concat-empty", r"concat.*empty|empty entries|all-NA"),
    ("applymap", r"applymap"),
    (
        "is_categorical",
        r"is_categorical_dtype|is_datetime64tz_dtype|is_sparse|is_interval_dtype|is_period_dtype",
    ),
    ("delim-whitespace", r"delim_whitespace"),
    ("dataframe-swapaxes", r"swapaxes|swaplevel"),
    ("setting-item-incompat", r"incompatible dtype|Setting an item of incompatible"),
    ("value-counts", r"value_counts"),
    ("pandas-other", r"pandas|DataFrame|Series|\.dt\.|\.str\.|Index\b"),
]
PANDAS_HINT = re.compile(
    r"pandas|DataFrame|Series|dtype|fillna|inplace|groupby|to_datetime|concat", re.IGNORECASE
)


def parse(path):
    """junit xml -> {test_id: dict(outcome, message, text, file, line)}"""
    if not path.exists():
        return {}
    root = ET.parse(path).getroot()
    out = {}
    for tc in root.iter("testcase"):
        tid = f"{tc.get('classname')}::{tc.get('name')}"
        outcome, msg, text = "passed", "", ""
        for tag in ("failure", "error", "skipped"):
            el = tc.find(tag)
            if el is not None:
                outcome = (
                    "failed" if tag == "failure" else ("error" if tag == "error" else "skipped")
                )
                msg = el.get("message") or ""
                text = el.text or ""
                break
        out[tid] = {
            "outcome": outcome,
            "message": msg,
            "text": text,
            "testfile": tc.get("file") or "",
        }
    return out


import os

ROOT = os.environ.get("REPO_ROOT", "").rstrip("/") + "/" if os.environ.get("REPO_ROOT") else ""
FRAME = re.compile(r"^(\S+?\.py):(\d+): ?(\S*)\s*$", re.MULTILINE)


def frames(text):
    return [
        (m.group(1).replace(ROOT, ""), int(m.group(2)), m.group(3))
        for m in FRAME.finditer(text or "")
    ]


def locate(text, msg):
    """Return (file, line, func, where, testfile). where: superset | test-only | unknown."""
    fr = frames(text)
    sup = [f for f in fr if f[0].startswith("superset/")]
    tst = [f for f in fr if f[0].startswith("tests/")]
    if sup:
        site, where = sup[-1], "superset"
    elif tst:
        site, where = tst[-1], "test-only"
    else:
        site, where = ("", 0, ""), "unknown"
    return site[0], str(site[1]), site[2], where, (tst[0][0] if tst else "")


def classify(msg, text):
    blob = f"{msg}\n{text}"
    for name, rx in CLASSES:
        if re.search(rx, blob, re.IGNORECASE):
            return name
    return "unclassified"


def main():
    r1 = parse(S / "r1-p2-default.xml")
    r2 = parse(S / "r2-detail.xml") or parse(S / "r2-p2-werror.xml")
    r3 = parse(S / "r3-p3-default.xml")
    bad = {"failed", "error"}
    base = {t for t, r in r1.items() if r["outcome"] in bad}
    rows = []
    for tid, r in r2.items():
        if r["outcome"] in bad and tid not in base:
            f, ln, fn, where, tf = locate(r["text"], r["message"])
            rows.append(
                {
                    "source": "r2-forward",
                    "test": tid,
                    "file": f,
                    "line": ln,
                    "func": fn,
                    "where": where,
                    "testfile": tf,
                    "cls": classify(r["message"], r["text"]),
                    "pandas": bool(PANDAS_HINT.search(r["message"] + r["text"])),
                    "message": (r["message"] or "").strip()[:400],
                }
            )
    for tid, r in r3.items():
        if r["outcome"] in bad and tid not in base:
            f, ln, fn, where, tf = locate(r["text"], r["message"])
            rows.append(
                {
                    "source": "r3-break",
                    "test": tid,
                    "file": f,
                    "line": ln,
                    "func": fn,
                    "where": where,
                    "testfile": tf,
                    "cls": classify(r["message"], r["text"]),
                    "pandas": bool(PANDAS_HINT.search(r["message"] + r["text"])),
                    "message": (r["message"] or "").strip()[:400],
                }
            )
    (OUT / "inventory.json").write_text(json.dumps(rows, indent=1))

    def count(pred):
        return sum(1 for r in rows if pred(r))

    n = lambda d: sum(1 for r in d.values() if r["outcome"] in bad)
    lines = [
        "# pandas 3 migration inventory, unit tests",
        "",
        "| run | tests | failed/error |",
        "|---|---|---|",
        f"| r1 pandas 2.3.3 default (baseline) | {len(r1)} | {n(r1)} |",
        f"| r2 pandas 2.3.3 `-W error::FutureWarning` | {len(r2)} | {n(r2)} |",
        f"| r3 pandas 3.0.5 default | {len(r3)} | {n(r3)} |",
        "",
        (
            f"Rows after removing baseline failures: **{len(rows)}** "
            f"(forward-looking {count(lambda r: r['source'] == 'r2-forward')}, "
            f"actual breakage {count(lambda r: r['source'] == 'r3-break')}; "
            f"pandas-related {count(lambda r: r['pandas'])}, other {count(lambda r: not r['pandas'])})"
        ),
        "",
    ]
    for src in ("r2-forward", "r3-break"):
        sub = [r for r in rows if r["source"] == src]
        lines += [
            f"## {src}: by class",
            "",
            "| class | rows | distinct files | distinct tests |",
            "|---|---|---|---|",
        ]
        byc = defaultdict(list)
        for r in sub:
            byc[r["cls"]].append(r)
        for c, rs in sorted(byc.items(), key=lambda kv: -len(kv[1])):
            lines.append(
                f"| {c} | {len(rs)} | {len({r['file'] for r in rs if r['file']})} | {len({r['test'] for r in rs})} |"
            )
        lines += ["", f"### {src}: top source files", ""]
        for f, k in Counter(r["file"] for r in sub if r["file"]).most_common(15):
            lines.append(f"- `{f}` ({k})")
        lines += ["", f"### {src}: top test files", ""]
        for f, k in Counter(r["test"].split("::")[0] for r in sub).most_common(12):
            lines.append(f"- `{f}` ({k})")
        lines += ["", f"### {src}: sample messages", ""]
        seen = set()
        for r in sub:
            key = r["message"][:80]
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"- [{r['cls']}] `{r['file']}:{r['line']}` {r['message'][:160]}")
            if len(seen) >= 25:
                break
        lines.append("")
    (OUT / "summary.md").write_text("\n".join(lines))
    print("\n".join(lines[:12]))
    print(f"\nwrote {OUT / 'inventory.json'} ({len(rows)} rows) and {OUT / 'summary.md'}")


if __name__ == "__main__":
    main()
