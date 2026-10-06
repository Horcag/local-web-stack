# Delivery evidence — 2026-10-06

Runtime source: merge commit `639846121a386c3462ad98ace848d556d9cfcd7d`, incorporating
[PR #1](https://github.com/Horcag/local-web-stack/pull/1) and
[PR #6](https://github.com/Horcag/local-web-stack/pull/6).

- 74 executed offline cases, zero failures/skips, 86.87% service/MCP line coverage.
  Shared lock, file/artifact, Compose-route, format, lint and typing gates passed.
- [Python 3.12/3.13 and real desktop/mobile browser CI](https://github.com/Horcag/local-web-stack/actions/runs/37451839664)
  passed. Browser cases cover creation/discovery/collection, retry/skip, actual fixture-browser
  import, downloads, persistence, text/XSS handling, keyboard navigation and CDP tab preservation.
- [Packaged image and real Chromium CI](https://github.com/Horcag/local-web-stack/actions/runs/37451839893)
  passed. The final local image also passed the same packaged-route/Chromium smoke.
- Pre-commit and pre-push were installed in the canonical checkout and actually executed.
- Both installed containers matched all 18 runtime source files. The SHA256 digest of the
  sorted relative-path/content-hash manifest was
  `a5ce81ca1a51d6a06e645bc722f05be181f87903dc22f03252b612dd7f1581a3`.
- Live MCP listed 15 tools. A bounded two-page documentation search resumed previously failed
  receipts, discovered four unique URLs and saved 43,689 characters from two useful sources.
  Source URLs: https://docs.crawl4ai.com/advanced/session-management/ and
  https://docs.crawl4ai.com/core/quickstart/. Other engine failures remained recorded as gaps.
- Final live dashboard, generated API docs and the absence of the temporary acceptance run
  were checked after activation. The temporary run, four sources, screenshots, fixture
  profiles/databases/servers and registered scratch were removed after their checks.

The optional CDP route is unconfigured in the installed instance. Pending sources can be
handled through existing browser tools and result imports. Fixture CDP acceptance does not
establish access to a user's desktop profile or success on protected third-party sites.
Search-universe coverage remains unknown; forum traversal covers bounded discovered pages.
Image publication is configured for release/manual runs; this delivery did not publish a release tag.
