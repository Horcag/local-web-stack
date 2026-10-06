import asyncio
import os
import re
import time
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx

from .content import assess_content, canonical_url


_SEARCH_LOCK = asyncio.Lock()
_LAST_SEARCH = 0.0
_search_clock = time.monotonic


async def paced_search(client, url, **kwargs):
    """Serialize starts across runs in this service process, without SQL locks."""
    global _LAST_SEARCH
    try:
        delay = min(60.0, max(0.0, float(os.environ.get("LOCAL_WEB_SEARCH_DELAY_SECONDS", "1"))))
    except ValueError:
        delay = 1.0
    if not delay:
        return await client.get(url, **kwargs)
    async with _SEARCH_LOCK:
        await asyncio.sleep(max(0.0, delay - (_search_clock() - _LAST_SEARCH)))
        _LAST_SEARCH = _search_clock()
        return await client.get(url, **kwargs)


class Runner:
    def __init__(self, store, browser_reader=None):
        self.store = store
        self.browser_reader = browser_reader

    async def discover(self, run_id, request):
        run = self.store.run(run_id)
        queries = list(dict.fromkeys([*run["queries"], *request.queries]))[: run["max_queries"]]
        pages = min(request.pages or run["max_pages"], run["max_pages"])
        added = 0
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            for query in queries:
                search_query = query
                if run.get("domains"):
                    search_query += " (" + " OR ".join("site:" + d for d in run["domains"]) + ")"
                for page in range(1, pages + 1):
                    reservation = self.store.reserve(run_id, query, page)
                    if not reservation:
                        continue
                    receipt = {}
                    try:
                        response = await paced_search(
                            client,
                            os.environ.get("SEARXNG_URL", "http://127.0.0.1:8088").rstrip("/")
                            + "/search",
                            params={
                                "q": search_query,
                                "format": "json",
                                "pageno": page,
                                "language": run.get("language", "all"),
                            },
                            headers={"X-Real-IP": "127.0.0.1"},
                        )
                        response.raise_for_status()
                        payload = response.json()
                        results = payload.get("results", [])
                        receipt = {
                            "result_count": len(results),
                            "engine_errors": payload.get("unresponsive_engines", []),
                            "number_of_results": payload.get("number_of_results"),
                        }
                        for result in results:
                            try:
                                source, created = self.store.add_source(
                                    run_id,
                                    result.get("url", ""),
                                    result.get("title", ""),
                                    {
                                        "kind": "search",
                                        "query": query,
                                        "page": page,
                                        "engine": result.get("engine"),
                                        "engines": result.get("engines", []),
                                        "snippet": result.get("content", ""),
                                    },
                                    return_created=True,
                                )
                                added += int(created)
                            except ValueError:
                                continue
                        if not results:
                            break
                    except asyncio.CancelledError:
                        receipt = {"error": "cancelled", "result_count": 0}
                        raise
                    except Exception as exc:
                        receipt = {"error": str(exc), "result_count": 0}
                    finally:
                        self.store.search_result(
                            run_id, query, page, receipt, reservation=reservation
                        )
        return {"discovered_results": added, "coverage": coverage(self.store, run_id)}

    async def collect(self, run_id, batch_size):
        completed = []
        async with httpx.AsyncClient(timeout=90, follow_redirects=False) as client:
            for _ in range(batch_size):
                claimed = self.store.claim(run_id)
                if not claimed:
                    break
                source, token = claimed
                try:
                    headers = {}
                    if os.environ.get("CRAWL4AI_API_TOKEN"):
                        headers["Authorization"] = "Bearer " + os.environ["CRAWL4AI_API_TOKEN"]
                    response = await client.post(
                        os.environ.get("CRAWL4AI_URL", "http://127.0.0.1:11235").rstrip("/")
                        + "/md",
                        json={"url": source["url"]},
                        headers=headers,
                    )
                    # HTTP gate errors must retain their classification even without JSON.
                    if response.status_code >= 400:
                        payload = {
                            "markdown": "",
                            "title": "",
                            "status_code": response.status_code,
                            "error": response.text[:1000],
                        }
                    else:
                        payload = response.json()
                    if payload.get("url"):
                        canonical_url(str(payload["url"]))
                    result = await self.assess(source, payload, "crawl4ai")
                    finished = self.store.finish(source["id"], result, token)
                    completed.append(finished)
                    if finished["status"] in {"read", "partial"}:
                        self.forum_links(source, result if "links" in result else payload)
                except asyncio.CancelledError:
                    self.store.finish(
                        source["id"],
                        {"status": "queued", "error": "cancelled", "next_action": "collect"},
                        token,
                    )
                    raise
                except Exception as exc:
                    completed.append(
                        self.store.finish(
                            source["id"],
                            {
                                "status": "failed",
                                "error": str(exc),
                                "reasons": ["reader_error"],
                                "next_action": "retry",
                            },
                            token,
                        )
                    )
        return {"sources": completed, "coverage": coverage(self.store, run_id)}

    async def assess(self, source, payload, method):
        markdown = payload.get("markdown") or ""
        if isinstance(markdown, dict):
            markdown = markdown.get("raw_markdown", "")
        if not isinstance(markdown, str):
            markdown = ""
        title = str(payload.get("title") or source["title"])
        raw_status = payload.get("status_code")
        status_code = (
            raw_status if isinstance(raw_status, int) and 100 <= raw_status <= 599 else 200
        )
        result = dict(
            assess_content(markdown, title, status_code),
            markdown=markdown,
            title=title,
            method=method,
            error=payload.get("error"),
        )
        if payload.get("truncated") and result["status"] == "read":
            result.update(status="partial", reasons=["reader_truncated"])
        fallback_needed = result["status"] in {"blocked", "partial"} or (
            result["status"] == "failed"
            and (not markdown or status_code >= 500)
            and status_code != 404
        )
        if fallback_needed:
            fallback_error = None
            if self.browser_reader and self.store.reserve(source["run_id"]):
                try:
                    browser = await self.browser_reader(source["url"])
                    if browser:
                        if (
                            browser.get("url")
                            and canonical_url(browser["url"]) != source["canonical_url"]
                        ):
                            raise ValueError("Browser result URL differs from source")
                        result = dict(
                            assess_content(
                                browser.get("markdown", ""),
                                browser.get("title", title),
                                browser.get("status_code") or 200,
                            ),
                            markdown=browser.get("markdown", ""),
                            title=browser.get("title", title),
                            method=browser.get("method", "browser"),
                            error=None,
                            links=browser.get("links", []),
                            provenance=[
                                *source["provenance"],
                                {
                                    "kind": "browser",
                                    "method": browser.get("method", "browser"),
                                    "url": source["url"],
                                },
                            ],
                        )
                        if browser.get("truncated") and result["status"] == "read":
                            result.update(status="partial", reasons=["reader_truncated"])
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    fallback_error = str(exc)
            if result["status"] in {"blocked", "partial", "failed"}:
                result.update(
                    status="browser_pending",
                    next_action="Use existing browser tools and import via browser-result",
                    assessment_status=result["status"],
                    error=fallback_error or result.get("error"),
                )
        result.setdefault("next_action", "retry" if result["status"] == "failed" else None)
        return result

    def forum_links(self, source, payload):
        links = payload.get("links") or payload.get("metadata", {}).get("links") or {}
        if isinstance(links, dict):
            links = links.get("internal", [])
        for link in links if isinstance(links, list) else []:
            if not isinstance(link, dict):
                continue
            label = str(link.get("text", "")).strip().lower()
            rel = str(link.get("rel", "")).lower()
            url = urljoin(source["url"], link.get("href") or link.get("url") or "")
            if (
                label not in {"next", "next page", "следующая", "далее", "›", "»"}
                and "next" not in rel
            ):
                continue
            if urlsplit(url).hostname != urlsplit(source["url"]).hostname:
                continue
            parsed = urlsplit(url)
            pagination = any(
                key.lower() in {"page", "pageno", "start", "offset"}
                for key in parse_qs(parsed.query)
            ) or bool(
                re.search(r"/(?:page[-/]?\d+|thread|topic|forum|discussion)", parsed.path, re.I)
            )
            if not pagination:
                continue
            try:
                self.store.add_source(
                    source["run_id"],
                    url,
                    label,
                    {
                        "kind": "forum_next_page",
                        "parent_source_id": source["id"],
                        "label": label,
                        "inference": "same_domain_pagination_link",
                    },
                    forum=True,
                )
            except ValueError:
                continue


def coverage(store, run_id):
    run = store.run(run_id)
    sources = store.sources(run_id)
    counts = {
        status: sum(s["status"] == status for s in sources)
        for status in [
            "queued",
            "reading",
            "read",
            "partial",
            "blocked",
            "browser_pending",
            "failed",
            "skipped",
        ]
    }
    searches = store.searches(run_id)
    hashes = [s["content_sha256"] for s in sources if s.get("content_sha256")]
    return {
        "run_id": run_id,
        "counts": counts,
        "discovered": len(sources),
        "successful": counts["read"],
        "unique_content_hashes": len(set(hashes)),
        "content_duplicates": len(hashes) - len(set(hashes)),
        "attempted": sum(s["attempts"] > 0 for s in sources),
        "search_coverage": "unknown",
        "forum_coverage": "bounded_discovered_pages_only",
        "search_requests": len(searches),
        "searches": searches,
        "engine_errors": [e for s in searches for e in s["data"].get("engine_errors", [])],
        "requests_used": run["requests_used"],
        "max_requests": run["max_requests"],
        "content_chars": run["content_chars"],
        "max_content_chars": run["max_content_chars"],
        "max_sources": run["max_sources"],
        "forum_pages": run["forum_pages"],
        "max_forum_pages": run["max_forum_pages"],
    }
