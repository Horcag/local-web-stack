# Quality contract

Install uv 0.11.13 and Python 3.12 or 3.13. Run `make setup`, `make hooks`, then
`make quality`. Without Make, the equivalents are `uv sync --frozen --group dev`,
`uv run --frozen pre-commit install --hook-type pre-commit --hook-type pre-push`,
and `uv run --frozen python scripts/quality/check.py`.

The same command runs before commit, before push, and in CI. Docker Compose CLI is required; configuration validation runs offline without a daemon or service startup. The route guard rejects host loopback/published-port addresses for cross-service traffic. It checks lock freshness
with `uv lock --check --offline`; frozen sync alone does not verify freshness.
Ruff format and correctness lint cover service, MCP, tests and quality scripts.
Mypy checks application/MCP and quality scripts, including untyped function bodies;
third-party missing annotations are tolerated. Tests run verbosely with coverage and
JUnit case receipts. No browser download, service startup or external network is
needed for offline tests. Dependency installation in CI requires network. A separate
mandatory Python 3.12 browser UI job installs Chromium and exercises desktop/mobile
flows on owned fixtures; screenshots are retained for reviewers for seven days.

Coverage fails below 80% across service and MCP code. Increase it as runtime seams
get regression coverage; do not exclude new uncovered business code to pass.
Receipts default to ignored `work/quality/`; set `LOCAL_WEB_QUALITY_DIR` for owned scratch; CI retains them for seven days for PR
reviewers. Local receipts are replaceable after review, not durable release evidence.

The source policy checks tracked and new unignored files. Python files normally
stay within 400 lines, including tests. `service/crawl4ai_api.py` began at 463 lines and was split at request/configuration
and browser-launch boundaries during this delivery; no legacy exceptions remain.
Files above 1 MB, binary installers/archives/databases/private keys and environment
secret files are rejected. Credential matching catches common keys and is a useful
accident guard, not proof that every possible secret is absent.

Container publication runs on `v*` tags or manual dispatch after the shared quality
gate and packaged application/browser smoke test. It publishes immutable `sha-<revision>` tags to GHCR;
it does not deploy a running service. Pull requests touching image inputs run the same smoke automatically.
The smoke verifies health/dashboard/browser status and mocked `/md` HTTP contracts,
then launches bundled Chromium against an owned in-memory page without network. Runtime images omit dev tools.
