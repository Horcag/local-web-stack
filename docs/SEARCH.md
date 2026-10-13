# Search behavior and validation

`POST /search`, MCP `web_search`, and research discovery share one application-owned
backend, pacing lock, ranking policy and browser adapter. The MCP layer forwards requests
to this service rather than maintaining a second search implementation.

## Requests and constraints

`language="auto"` selects `ru` when the query contains Cyrillic, otherwise `all`.
An explicit language is preserved. Unquoted `site.example.org` is normalized to
`site:example.org`; quoted text and exclusions are preserved.

`domains` constrains the result hostname to the named domain or a genuine child domain.
Query `site:` operators and structured domains intersect; incompatible scopes fail with
HTTP 422 before contacting providers. `required_entities` and quoted phrases must all
occur in the result evidence. Unquoted names are relevance anchors rather than an
implicit requirement that every requested organization appears in one document.

Each candidate must have a public HTTP(S) URL and enough matching title/snippet terms.
Filtering happens before truncation, including for one-result responses. Duplicate URLs
merge engine evidence. `quality_score` describes this lexical ranking; it does not certify
the source, document contents or dataset suitability.

## Bounded recovery and receipts

A search makes at most three dispatches within 90 seconds: the original query, one
optional removal of English search boilerplate, then one configured Yandex browser read.
Refinement preserves entities, domains, quotes, exclusions, language and page. Browser
fallback applies only to general searches selecting Yandex without a time range. Its
supported explicit locales are Russian, English and French; unsupported locales produce
a visible skipped receipt without consuming another dispatch. It makes no CAPTCHA model calls.

Provider starts share the configured minimum delay. Durable discovery validates its
current reservation token and lease immediately before each dispatch and charges extra
attempts against the run's existing request budget. Reservation retry counts remain
separate from `provider_attempts`; failed empty searches can retry up to the existing cap.

Responses include effective query/language, normalizations, constraints, raw and accepted
engine counts, rejected-result reasons, attempt receipts, actual dispatch count and
`degraded`. A silent engine does not prove that it executed; its `attempted` value is
unknown. Empty output does not prove absence of relevant sources. Search coverage remains
`unknown`. Fully successful responses may be cached; cache hits dispatch no provider requests.

The browser reader uses the explicitly configured protected CDP route and existing profile.
It creates one hidden target, attaches only to that target and closes it on completion or
cancellation. It never waits for unrelated tabs to initialize. A result requires a verified
main document, known successful HTTP status, matching query/page/language and organic DOM
cards. Generic page links and CAPTCHA pages cannot become search results. Document commit
and bounded body readiness are sufficient; delayed `DOMContentLoaded` does not block a
readable verified page. Search reads wait up to eight seconds for organic cards, rather
than accepting an already visible but unfinished search-page shell; this wait stays within
the existing read deadline.

## Relevance evaluation

Use the same frozen query cohort and result limit before and after a change. Preserve raw
responses and effective request parameters, including language defaults. Independently
label title/snippet evidence as irrelevant, topical or usable primary-source discovery;
leave uncertain labels unknown. Discovery labels do not certify retrieved document contents.

`scripts/quality/search_relevance.py` computes precision and primary-source hit bounds,
label coverage, empty/degraded rates, latency and domain escapes. Unknown labels widen
the bounds rather than counting as success. Validate strict domain/entity constraints and
specific false-positive regressions separately. Offline tests establish deterministic
contracts; installed source hashes and live searches establish runtime acceptance.
