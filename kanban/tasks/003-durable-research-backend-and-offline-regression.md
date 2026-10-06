---
id: 3
title: Durable research backend and offline regression tests
status: done
priority: high
created: 2026-10-06T14:06:39.319251591+04:00
updated: 2026-10-06T14:50:38.724353504+04:00
started: 2026-10-06T14:50:38.724866752+04:00
completed: 2026-10-06T14:50:38.724866752+04:00
parent: 1
class: standard
---

[[2026-10-06]] Tue 14:14
Implemented service/research durable SQLite API and 12 offline regressions. Fresh unittest discovery 12/12 passed; Ruff lint and format checks passed; diff check clean. Modules below 400 lines. Child scope ready for parent integration; retain in-progress until merge.

[[2026-10-06]] Tue 14:20
Final improvements: retained markdown SHA256, bounded 50 outcome receipts, concurrent discovery provenance union, accurate new-source count, shared default1s search pacing, duplicate-content coverage. 14 offline tests pass; Ruff lint/format and diff checks pass. Source discovery title preserved for import assessment. Ready for parent commit.

[[2026-10-06]] Tue 14:24
Durability review fixed: search reservations persist 300s leases/tokens/attempts, expired or error/cancelled searches retry atomically up to3 within request budget, successful/live searches remain protected, stale workers cannot overwrite new receipts. Fresh research discovery ran17 tests all passed; Ruff and diff checks pass. Store300 lines. Temporary recovery script removed.

[[2026-10-06]] Tue 14:50
Parent integration evidence: merged main 639846121a386c3462ad98ace848d556d9cfcd7d via PRs #1/#6. Python 3.12/3.13, UI and image CI passed; final 74 cases, 86.87% coverage. Live MCP 15 tools resumed failed two-page search, discovered 4 sources, read 2 documents totaling 43689 characters; temporary run cleaned. Backend subtask complete.
