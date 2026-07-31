# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "httpx==0.28.1",
#   "mcp==1.28.1",
# ]
# ///

from __future__ import annotations

import os
import re
import time
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP


SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://127.0.0.1:8088").rstrip("/")
SEARXNG_CLIENT_IP = "127.0.0.1"
CRAWL4AI_URL = os.environ.get("CRAWL4AI_URL", "http://127.0.0.1:11235").rstrip("/")
CRAWL4AI_API_TOKEN = os.environ.get("CRAWL4AI_API_TOKEN", "").strip()
# Only engines that were measured to answer the actual query from this network.
# bing/google/brave are kept loadable in settings.yml but out of the pool, see
# the comments there.
DEFAULT_SEARCH_ENGINES = os.environ.get(
    "LOCAL_WEB_SEARCH_ENGINES",
    "mojeek,mwmbl,yandex",
).strip()


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


MCP_TRANSPORT = os.environ.get(
    "LOCAL_WEB_MCP_TRANSPORT",
    os.environ.get("MCP_TRANSPORT", "stdio"),
).strip().lower()
MCP_HOST = os.environ.get("LOCAL_WEB_MCP_HOST", "127.0.0.1").strip() or "127.0.0.1"
MCP_PORT = _env_int("LOCAL_WEB_MCP_PORT", 8765)
MCP_PATH = os.environ.get("LOCAL_WEB_MCP_PATH", "/mcp").strip() or "/mcp"
SEARCH_CACHE_TTL_SECONDS = _env_int("LOCAL_WEB_SEARCH_CACHE_TTL_SECONDS", 900)
SEARCH_TIMEOUT_SECONDS = _env_int("LOCAL_WEB_SEARCH_TIMEOUT_SECONDS", 35)
READ_TIMEOUT_SECONDS = _env_int("LOCAL_WEB_READ_TIMEOUT_SECONDS", 90)
# Smallest batch an engine has to return before a zero-overlap batch counts as
# noise rather than as a thin but legitimate answer.
OFF_TOPIC_MIN_RESULTS = _env_int("LOCAL_WEB_OFF_TOPIC_MIN_RESULTS", 3)

_SEARCH_CACHE: dict[tuple[tuple[str, str], ...], tuple[float, dict[str, Any]]] = {}

mcp = FastMCP(
    "local-web",
    host=MCP_HOST,
    port=MCP_PORT,
    streamable_http_path=MCP_PATH,
    json_response=True,
    stateless_http=True,
)


def _crawl4ai_headers() -> dict[str, str]:
    if not CRAWL4AI_API_TOKEN:
        return {}
    return {"Authorization": f"Bearer {CRAWL4AI_API_TOKEN}"}


def _searxng_headers() -> dict[str, str]:
    return {"X-Real-IP": SEARXNG_CLIENT_IP}


def _cache_key(params: dict[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(params.items()))


def _get_search_cache(params: dict[str, str]) -> dict[str, Any] | None:
    if SEARCH_CACHE_TTL_SECONDS <= 0:
        return None
    cached = _SEARCH_CACHE.get(_cache_key(params))
    if not cached:
        return None
    cached_at, payload = cached
    if time.monotonic() - cached_at > SEARCH_CACHE_TTL_SECONDS:
        _SEARCH_CACHE.pop(_cache_key(params), None)
        return None
    return payload


def _set_search_cache(params: dict[str, str], payload: dict[str, Any]) -> None:
    if SEARCH_CACHE_TTL_SECONDS <= 0:
        return
    _SEARCH_CACHE[_cache_key(params)] = (time.monotonic(), payload)


_TOKEN_RE = re.compile(r"\w{4,}", re.UNICODE)


def _matches_query(tokens: set[str], item: dict[str, Any]) -> bool:
    haystack = " ".join(
        str(item.get(key) or "") for key in ("title", "url", "content")
    ).lower()
    return any(token in haystack for token in tokens)


def _drop_off_topic_engines(
    query: str, items: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Drop engines whose entire batch shares no token with the query.

    An engine that is served an anti-scraping placeholder answers with a
    well-formed page of results for something else entirely - measured on bing,
    which replies to the first query term only. Nothing downstream can tell
    those from real hits, so they have to be cut here. One odd result is normal
    (translated pages, acronym expansions); a whole batch with zero overlap is
    not, hence the per-engine granularity and the batch-size floor.
    """
    tokens = set(_TOKEN_RE.findall(query.lower()))
    if not tokens:
        return items, []

    by_engine: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        by_engine.setdefault(str(item.get("engine") or ""), []).append(item)

    dropped = {
        engine
        for engine, batch in by_engine.items()
        if len(batch) >= OFF_TOPIC_MIN_RESULTS
        and not any(_matches_query(tokens, item) for item in batch)
    }
    if not dropped:
        return items, []
    kept = [item for item in items if str(item.get("engine") or "") not in dropped]
    return kept, sorted(dropped)


@mcp.tool()
async def web_search(
    query: str,
    max_results: int = 8,
    category: str = "general",
    language: str = "auto",
    time_range: str = "",
    engines: str = "",
) -> dict[str, Any]:
    """Search the web through the local SearXNG instance and return compact JSON results.

    ``category`` only applies when no engines are selected: SearXNG unions the
    ``categories`` and ``engines`` parameters instead of intersecting them
    (searx/webadapter.py, parse_generic), so sending both expands the category
    back into its full engine pool and silently annuls the engine filter.
    """
    max_results = max(1, min(max_results, 20))
    params: dict[str, str] = {"q": query, "format": "json"}
    if language and language != "auto":
        params["language"] = language
    if time_range:
        params["time_range"] = time_range
    selected_engines = engines.strip()
    if not selected_engines and category == "general":
        selected_engines = DEFAULT_SEARCH_ENGINES
    if selected_engines:
        params["engines"] = selected_engines
    else:
        params["categories"] = category

    cached_payload = _get_search_cache(params)
    if cached_payload is not None:
        payload = cached_payload
    else:
        async with httpx.AsyncClient(timeout=SEARCH_TIMEOUT_SECONDS) as client:
            response = await client.get(
                f"{SEARXNG_URL}/search",
                params=params,
                headers=_searxng_headers(),
            )
            response.raise_for_status()
            payload = response.json()
        _set_search_cache(params, payload)

    # Filter before slicing, so a noisy engine cannot push real hits out of the
    # window, and after the cache, so the cache keeps the raw upstream payload.
    on_topic, dropped_engines = _drop_off_topic_engines(
        query, payload.get("results", [])
    )

    results = []
    for item in on_topic[:max_results]:
        results.append(
            {
                "title": item.get("title"),
                "url": item.get("url"),
                "content": item.get("content"),
                "engine": item.get("engine"),
                "category": item.get("category"),
                "score": item.get("score"),
                "published_date": item.get("publishedDate") or item.get("published_date"),
            }
        )

    return {
        "query": query,
        "source": "searxng",
        "searxng_url": SEARXNG_URL,
        "engines": sorted({str(item["engine"]) for item in results if item["engine"]})
        or None,
        "requested_engines": selected_engines or None,
        "dropped_engines": dropped_engines or None,
        "count": len(results),
        "results": results,
    }


@mcp.tool()
async def read_url(url: str, cache_mode: str = "enabled") -> dict[str, Any]:
    """Read a URL through local Crawl4AI and return LLM-friendly markdown."""
    if cache_mode not in {"enabled", "bypass"}:
        cache_mode = "enabled"
    async with httpx.AsyncClient(timeout=READ_TIMEOUT_SECONDS) as client:
        response = await client.post(
            f"{CRAWL4AI_URL}/md",
            json={"url": url, "cache_mode": cache_mode},
            headers=_crawl4ai_headers(),
        )
        response.raise_for_status()
        payload = response.json()

    markdown = payload.get("markdown") or payload.get("result") or payload
    return {
        "url": url,
        "source": "crawl4ai",
        "crawl4ai_url": CRAWL4AI_URL,
        "cache_mode": cache_mode,
        "markdown": markdown,
    }


@mcp.tool()
async def local_web_health() -> dict[str, Any]:
    """Check local SearXNG and Crawl4AI availability."""
    status: dict[str, Any] = {
        "searxng_url": SEARXNG_URL,
        "crawl4ai_url": CRAWL4AI_URL,
        "searxng": "unknown",
        "crawl4ai": "unknown",
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.get(
                f"{SEARXNG_URL}/search",
                params={"q": "example", "format": "json"},
                headers=_searxng_headers(),
            )
            status["searxng"] = response.status_code
        except Exception as exc:  # noqa: BLE001
            status["searxng"] = repr(exc)

        try:
            response = await client.get(f"{CRAWL4AI_URL}/health", headers=_crawl4ai_headers())
            status["crawl4ai"] = response.json() if response.headers.get("content-type", "").startswith("application/json") else response.text
        except Exception as exc:  # noqa: BLE001
            status["crawl4ai"] = repr(exc)

    return status


if __name__ == "__main__":
    if MCP_TRANSPORT in {"http", "streamable-http", "streamable_http", "streaming"}:
        mcp.run(transport="streamable-http")
    elif MCP_TRANSPORT == "sse":
        mcp.run(transport="sse")
    else:
        mcp.run()
