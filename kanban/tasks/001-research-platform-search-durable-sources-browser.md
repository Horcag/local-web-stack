---
id: 1
title: 'Research platform: search, durable sources, browser fallback and dashboard'
status: in-progress
priority: critical
created: 2026-10-06T14:04:58.712119792+04:00
updated: 2026-10-06T14:39:29.049260119+04:00
tags:
    - research
    - platform
claimed_by: dale-maple
claimed_at: 2026-10-06T14:39:29.049380523+04:00
class: standard
---

Deliver paginated and expanded search, durable deduplicated sources, bounded collection of forum threads, extraction diagnostics, explicit browser handoffs and imports, coverage and an accessible local dashboard. Preserve existing MCP tools. Validate offline regressions and live source retrieval.

[[2026-10-06]] Tue 14:12
Architecture implemented in isolated research-platform checkout. Backend, UI and quality lanes active. Parent integrates MCP pagination, diagnosis and optional CDP tabs preserving existing sessions.

[[2026-10-06]] Tue 14:36
PR #1 merged after Quality Python 3.12/3.13, real browser UI and container smoke all passed for head 985f30c. Local hooks installed and pre-push gate passed. Final live MCP/API acceptance in progress.

[[2026-10-06]] Tue 14:39
Live MCP lists15 tools and dashboard/image smoke passes. Actual research discovery exposed missing SEARXNG_URL in crawl4ai Compose environment; fixing service route before accepting end-to-end collection.
