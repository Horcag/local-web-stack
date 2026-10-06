# Repository contract

Implement the smallest complete change and preserve unrelated work. Read callers,
tests and the nearest module before editing. Keep secrets, browser profiles,
databases, logs and generated payloads outside Git.

Ownership boundaries:
- `service/`: HTTP application, browser lifecycle and crawl behavior.
- `mcp/`: MCP tools and transport; reuse service behavior rather than duplicating it.
- `tests/`: deterministic offline contract and regression tests; mock external I/O.
- `scripts/quality/`, `pyproject.toml`, `.github/`: shared development and CI gates.
- `docs/`: public operation and verification contracts.

Run `make quality` before handoff; `make format` applies Python formatting.
Update affected tests when behavior changes. Name executed cases and exact missing
acceptance evidence. Offline tests, image publication and live deployment are
separate milestones. Do not claim live browser acceptance from mocked tests.

Python modules normally stay at or below 400 lines. The source policy enforces
this budget without legacy exceptions. Split at meaningful ownership boundaries.

Use short scoped branches, review the final diff, and do not commit generated
artifacts. Workflow changes retain least permissions, immutable action pins,
bounded timeouts and finite artifact retention. See `docs/AGENT_WORKFLOW.md`.
