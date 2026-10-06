# Agent and contributor workflow

1. Read the requested outcome, current Git state, module owners and existing tests.
2. Make a scoped reversible change using current module boundaries.
3. Update affected regression tests and run the shared `make quality` gate.
4. Review the diff and report behavior, exact executed cases, coverage and gaps.
5. For delivery, distinguish source revision, CI, published image and actual runtime
   acceptance. Verify the target requested by the task before claiming completion.

Install both hooks with `make hooks`. Hooks are local guardrails; CI remains required
because hooks can be bypassed. Keep logs, profiles and build outputs outside Git.
Use offline mocks for deterministic tests and a separately scoped browser/image
smoke for operational acceptance. Do not stop or overwrite unrelated work.
