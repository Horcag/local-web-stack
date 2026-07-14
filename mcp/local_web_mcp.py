# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "httpx==0.28.1",
#   "mcp==1.28.1",
# ]
# ///

from __future__ import annotations

import os
import time
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP


SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://127.0.0.1:8088").rstrip("/")
CRAWL4AI_URL = os.environ.get("CRAWL4AI_URL", "http://127.0.0.1:11235").rstrip("/")
CRAWL4AI_API_TOKEN = os.environ.get("CRAWL4AI_API_TOKEN", "").strip()
DEFAULT_SEARCH_ENGINES = os.environ.get(
    "LOCAL_WEB_SEARCH_ENGINES",
    "bing,google,mojeek,presearch",
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


@mcp.tool()
async def web_search(
    query: str,
    max_results: int = 8,
    category: str = "general",
    language: str = "auto",
    time_range: str = "",
    engines: str = "",
) -> dict[str, Any]:
    """Search the web through the local SearXNG instance and return compact JSON results."""
    max_results = max(1, min(max_results, 20))
    params: dict[str, str] = {
        "q": query,
        "format": "json",
        "categories": category,
    }
    if language and language != "auto":
        params["language"] = language
    if time_range:
        params["time_range"] = time_range
    selected_engines = engines.strip()
    if not selected_engines and category == "general":
        selected_engines = DEFAULT_SEARCH_ENGINES
    if selected_engines:
        params["engines"] = selected_engines

    cached_payload = _get_search_cache(params)
    if cached_payload is not None:
        payload = cached_payload
    else:
        async with httpx.AsyncClient(timeout=SEARCH_TIMEOUT_SECONDS) as client:
            response = await client.get(f"{SEARXNG_URL}/search", params=params)
            response.raise_for_status()
            payload = response.json()
        _set_search_cache(params, payload)

    results = []
    for item in payload.get("results", [])[:max_results]:
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
        "engines": selected_engines or None,
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
