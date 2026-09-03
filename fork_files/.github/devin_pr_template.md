### SUMMARY
<!-- One shard of the pandas 2.3.3 to 3.0.5 migration. Which files, which class of change, why. -->

### CALL SITES
<!-- file:line, what the library said, what changed -->

### ACCEPTANCE
<!-- Each command from the ticket and its exit code on this branch, on both versions -->

### NOT DONE, AND WHY
<!-- Anything reported as needs_human, with the reason -->

### CHECKLIST
- [ ] Runs on pandas 2.3.3 and 3.0.5; the lower bound did not move
- [ ] No file under tests/, .github/, superset/migrations/, or requirements/ was changed
- [ ] ruff format and ruff check pass on changed files
- [ ] Conventional-commit title
