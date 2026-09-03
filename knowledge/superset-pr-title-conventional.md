---
name: "Superset PR titles are regex-enforced conventional commits"
trigger_description: "When opening a pull request or writing a commit message for apache/superset"
---
PR titles must match `^(build|chore|ci|docs|feat|fix|perf|refactor|style|test|other)(\(.+\))?(\!)?:\s.+`. Use `fix(<module>): <summary>` for a repair. A non-matching title fails a required check. Fill the repository's pull request template; a `devin_pr_template.md` variant is honoured when present.
