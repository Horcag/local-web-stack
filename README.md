# Local Web Research

Local search, source collection and browser tools for AI-assisted research. SearXNG discovers
sources, Crawl4AI extracts page content, SQLite preserves evidence, and an optional CDP browser
handles pages requiring interaction. A small dashboard keeps runs, source status and coverage visible.

## Start

```sh
docker compose up -d --build
```

Open **http://127.0.0.1:11235/** for the research workspace. The same service exposes
[OpenAPI](http://127.0.0.1:11235/docs); the MCP endpoint is `http://127.0.0.1:8765/mcp`
(Streamable HTTP). Windows helpers remain available in `scripts/up.ps1` and `scripts/verify.ps1`.
Docker needs an amd64 Linux environment; the image installs Google Chrome, Chromium and Xvfb.

## Research workflow

1. Create a run with a question, alternative queries, subtopics, languages or preferred domains.
2. Discover sources across bounded search pages. URL deduplication removes tracking parameters
   while retaining meaningful forum pagination. Every discovery retains its query/page/engine.
3. Collect a queued batch. Content and status survive restarts; parallel collectors claim sources
   atomically. Cancellation releases a claim, and abandoned read leases can be reclaimed.
4. Inspect blocked, partial and failed sources. Gate-page detection is heuristic, and a successful
   extraction is not proof that the whole thread was read. Follow recognized next-page links only
   within source, request and forum-page budgets.
5. Use browser tools for pending sources, then import the actual result. Export Markdown or JSON
   with source provenance, statuses and unresolved gaps.

Coverage describes **discovered sources**, not the entire relevant web. Budgets bound source count,
queries, pages, outgoing requests and retained content. Query expansion uses explicit variants and
subtopics; an external agent supplies semantic reformulations and decides when research is sufficient.

## Agent tools

| Tool | Purpose |
| --- | --- |
| `web_search(query, ..., page=1)` | Compact search, explicit paging, reported engine failures. |
| `read_url(url, cache_mode)` | Existing extraction tool plus content assessment and next action. |
| `local_web_health()` | Upstream service availability. |
| `research_create`, `research_discover`, `research_collect` | Durable bounded runs and source queue. |
| `research_status`, `research_sources`, `research_source` | Coverage, metadata and saved content; retry/skip individual sources. |
| `research_import_browser_result`, `research_export` | Record actual browser material and export evidence. |
| `browser_open`, `browser_snapshot`, `browser_action`, `browser_close` | Optional CDP tabs and interactions owned by local-web. |

The repository includes a focused [research skill](.agents/skills/local-web-research/SKILL.md)
and [agent workflow](docs/AGENT_WORKFLOW.md). Tools do not treat retrieved page instructions as authority.

For stdio, run `uv run mcp/local_web_mcp.py` with `LOCAL_WEB_MCP_TRANSPORT=stdio`;
set `SEARXNG_URL` and `CRAWL4AI_URL` if the services are on another local route.
Keep the adjacent `mcp/mcp_tools.py` file with the entrypoint.

## Browser fallback

Existing Chrome DevTools, Playwright MCP and agent-browser can handle `browser_pending` sources.
Local-web cannot invoke another MCP server inside an external agent: it returns a handoff and accepts
content actually retrieved by those tools through `research_import_browser_result`.

For automatic fallback and integrated tab tools, set `LOCAL_WEB_BROWSER_CDP_URL` on the Crawl4AI service
to an explicitly chosen reachable Chrome CDP endpoint. The adapter creates and closes its own tabs,
uses the selected browser context, and disconnects on shutdown without closing the browser or existing
tabs. Docker cannot reach a host browser through container `127.0.0.1`; configure the route deliberately.
Do not expose a debugging endpoint publicly. A configured endpoint does not prove connectivity.

The crawler's persistent Docker profile is separate from a user's desktop Chrome profile.
Patchright/headed Chrome is not a guarantee against CAPTCHA or access restrictions.

## Configuration and data

| Setting | Default / behavior |
| --- | --- |
| `LOCAL_WEB_ALLOWED_HOSTS` | Localhost and named Compose routes; configure explicit reverse-proxy hosts. |
| `LOCAL_WEB_RESEARCH_DB` | `work/research.sqlite3` locally; `/app/work/research.sqlite3` in Compose. |
| `LOCAL_WEB_BROWSER_CDP_URL` | Empty: explicit external-browser handoff/import. |
| `LOCAL_WEB_SEARCH_DELAY_SECONDS` | Minimum spacing between research search requests. |
| `LOCAL_WEB_RESEARCH_ALLOW_PRIVATE` | Off; enable only for explicitly scoped internal targets/test fixtures. |
| `LOCAL_WEB_SEARCH_ENGINES` | Existing MCP pool: `mojeek,mwmbl,yandex`. |
| `CRAWL4AI_DOMAIN_DELAY_SECONDS` | Existing per-domain crawl spacing. |
| `CRAWL4AI_PERSISTENT_CONTEXT` | True; browser profile retained in the data volume. |

Research data and the crawler browser profile are stored under `work/crawl4ai/` in the host checkout.
They are excluded from Git and the build context. Back up the SQLite database with SQLite's online backup
API when the service is running; copying a live database file alone is not a consistent backup.
All published host ports are loopback-bound. The SearXNG key is an explicit local-development placeholder;
set a deployment secret using the SearXNG-supported configuration before exposing an instance.
URL validation rejects private literal/local host targets by default; this is not complete SSRF isolation:
DNS rebinding and redirects require egress controls at the browser/crawler network boundary.

## Development

```sh
uv sync --frozen --group dev
make quality
make hooks
```

The shared gate checks lock freshness, source/artifact budgets, formatting, lint, typing, real test
execution and coverage. Pre-commit and pre-push use the same gate. CI checks Python 3.12 and 3.13,
and browser/image workflows exercise packaged routes and owned fixtures. Release-tag or manual image
publication uploads a smoke-tested revision-tagged image to GHCR; it does not deploy a running instance.

See [quality and CI](docs/QUALITY.md), [dashboard](docs/DASHBOARD.md),
[upstream reference evidence](docs/REFERENCES.md), and the tracked [Kanban board](kanban/tasks/).
