# Research dashboard

The service serves the dependency-free dashboard at `/`. Its assets are in `service/static/`; no CDN, frontend build, font download, or package install is required.

Create a titled research question, optionally provide query variants, subtopics, preferred domains, search language, and a source budget. Choose **Discover sources** to search, then **Collect next 5** to process one bounded batch. Saved research runs can be reopened from the sidebar. Refresh reloads durable API state; automatic refresh runs only while sources are actively being read and the tab is visible.

Coverage cards describe this run's discovered sources. Collected percentage uses discovered sources as the denominator. The broader relevant web universe is explicitly unknown. Search or filter the source library, open an original URL, or inspect a source to see saved content, collection reasons, attempts, retry count, and discovery provenance.

Browser availability is shown from `/browser/status`. When collection requires a browser, the dashboard leaves the source pending. Use a suitable configured Chrome DevTools, Playwright MCP, or agent-browser session to collect content, then import its final URL, actual page title, method and Markdown in the source drawer. Importing records an actual browser result; the interface does not launch those browser tools. Retry queues a source for another collection batch; skip records an explicit source decision.

Markdown and JSON exports use the server's export endpoints. Saved content and all source metadata are rendered with DOM `textContent`, never interpreted as HTML. Original links accept only HTTP(S) and open with `noopener noreferrer`. The interface supports keyboard navigation, native modal focus containment and Escape dismissal, visible focus rings, accessible input labels, status announcements, and a skip link. At narrow widths the run navigation becomes horizontal and the source table scrolls without hiding columns.

## API surface

The UI uses `/research/runs`, `/research/runs/{id}`, `/discover`, `/collect`, `/sources`, `/coverage`, `/export?format=markdown|json`, and `/research/sources/{id}` with `/retry`, `/skip`, and `/browser-result`. Collection submits `batch_size: 5`. Errors remain visible and failed operations can be retried without losing saved evidence.

## Smoke checklist

- Create a run; discover and collect one batch; reload and reopen it.
- Filter/search sources; inspect provenance and saved content.
- Verify browser-required sources remain pending; import actual collected content.
- Retry, collect again, and skip a source; export Markdown and JSON.
- Use Tab, Enter, and Escape; check mobile width and long URLs.
- Confirm `<script>` in imported content displays as text and does not execute.

Stable test selectors: `run-form`, `title`, `query`, `discover`, `collect`, `source-count`, and `source-filter` via `data-testid`.

## Deterministic browser acceptance

Run `.venv/bin/python scripts/quality/browser_smoke.py` with `CRAWL4_AI_BASE_DIRECTORY` pointing to task scratch. The script serves its own local search/crawler responses and browser pages; it never researches external sites or connects to an existing user browser. Playwright Chromium must already be installed. `--screenshot /outside/repository/desktop.png` optionally records desktop and mobile siblings for a named reviewer.

The smoke exercises the actual form, discovery, bounded collection (one read source and one browser-pending challenge), source filtering and safe text details, skip/retry/recollection, import of content and title actually extracted from its fixture browser page, Markdown and JSON downloads, persisted-run reopening, modal Escape and keyboard focus. It checks the 1440 px desktop and 390 px mobile layouts for document overflow and fails on dashboard JavaScript errors. A browser launched by this test exposes its own CDP endpoint so the test verifies real open/fill/click/snapshot/close operations while preserving another test-owned tab. All profiles, databases, exports, pages and both fixture-server threads are cleaned on exit; optional screenshots are caller-owned evidence.

Local acceptance passed under the storage wrapper after adding the actual page title to browser imports. A challenge title must not be reused to assess clean browser content. No screenshot was retained by the default run.
