---
name: pandas3-repair
description: Repair one shard of the pandas 2.3.3 to 3.0.5 migration so the code runs on both versions, verified by the ticket's acceptance commands. Use when a ticket carries a swe-loop work order block.
allowed-tools: bash, edit, git
argument-hint: <ticket number>
---

<!--
Licensed to the Apache Software Foundation (ASF) under one
or more contributor license agreements.  See the NOTICE file
distributed with this work for additional information
regarding copyright ownership.  The ASF licenses this file
to you under the Apache License, Version 2.0 (the
"License"); you may not use this file except in compliance
with the License.  You may obtain a copy of the License at

  http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing,
software distributed under the License is distributed on an
"AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
KIND, either express or implied.  See the License for the
specific language governing permissions and limitations
under the License.
-->

# pandas 3 repair, one shard

Read the ticket's work order block. It names the files, the sites, the tests, and the
acceptance commands. Then:

1. Reproduce: run the acceptance command with warnings as errors on the current version and
   confirm it fails at the listed sites.
2. Fix each site as the pandas message prescribes. Keep changes inside the listed files.
3. Verify: every acceptance command exits 0, on both versions.
4. Format: `ruff format` and `ruff check` on changed files. No black.
5. Deliver: one PR, title `fix(<module>): <summary>`, template filled, structured output
   provided with `is_final=true`.

Never edit `tests/`, `.github/`, `superset/migrations/`, or `requirements/`. Never move the
pandas lower bound. If a fix depends on context you cannot see, report it as `needs_human`.

Current state of the migration: !`git log --oneline -3`
