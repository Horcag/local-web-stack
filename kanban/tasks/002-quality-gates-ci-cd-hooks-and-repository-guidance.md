---
id: 2
title: Quality gates, CI/CD, hooks and repository guidance
status: review
priority: high
created: 2026-10-06T14:04:58.743703979+04:00
updated: 2026-10-06T14:53:36.498075688+04:00
tags:
    - quality
    - ci
claimed_by: dale-maple
claimed_at: 2026-10-06T14:53:36.498165914+04:00
class: standard
---

Inspect public Horcag repositories and upstream implementations. Add portable formatting, lint, typing, tests, coverage, file budgets, pre-commit/pre-push and CI/image publication with pinned actions and compact evidence.

[[2026-10-06]] Tue 14:14
Quality scripts/config/docs and pinned CI/image workflows implemented in task-1-dale-maple. actionlint, lock freshness, scoped script lint/typing and negative artifact/file-budget probes pass. Current21cases pass; coverage49.76% requires runtime regression cases toward65%. Runtime formatter/typing cleanup belongs integration. Hooks installation deferred until integration.

[[2026-10-06]] Tue 14:20
Updated gate80% measured floor; full mypy18files, Ruff quality scripts, actionlint3workflows, hook config and lock freshness pass. Mandatory browser UI job wired Python3.12 with owned fixture scratch/screenshots7days. Read-only PR image build and package-write publication share build+route+real bundled Chromium smoke; pushes exact tested image. Artifact/file-budget/secret-key negative probes rejected and cleaned. Parent owns final integrated gate and installed hook receipt.

[[2026-10-06]] Tue 14:24
Quality/tooling/docs implementation ready; no commits. Shared gate80% coverage, case receipt module discovery and scratch override; pinned CI Python3.12/3.13 plus mandatory real browser UI/image smoke. Official reference revisions documented. actionlint, full mypy18files, hook config, lock freshness and source policy pass; negative policy probes cleaned. Strict Docker allowlist validated both COPY contexts. Read-only review reported Host/Origin bypass, interrupted search reservation and open-tab snapshot leak; parent owns regression fixes/final full gate and installed hooks.

[[2026-10-06]] Tue 14:53
Final integration:74 executed cases,86.87%coverage; Python3.12/3.13, real UI and image CI passed. Hooks installed and actually executed; Compose route regression covers the live defect.
