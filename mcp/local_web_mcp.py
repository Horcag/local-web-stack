# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "httpx==0.28.1",
#   "mcp==1.28.1",
# ]
# ///

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP


SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://127.0.0.1:8088").rstrip("/")
SEARXNG_CLIENT_IP = "127.0.0.1"
CRAWL4AI_URL = os.environ.get("CRAWL4AI_URL", "http://127.0.0.1:11235").rstrip("/")
CRAWL4AI_API_TOKEN = os.environ.get("CRAWL4AI_API_TOKEN", "").strip()


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


MCP_TRANSPORT = (
    os.environ.get(
        "LOCAL_WEB_MCP_TRANSPORT",
        os.environ.get("MCP_TRANSPORT", "stdio"),
    )
    .strip()
    .lower()
)
MCP_HOST = os.environ.get("LOCAL_WEB_MCP_HOST", "127.0.0.1").strip() or "127.0.0.1"
MCP_PORT = _env_int("LOCAL_WEB_MCP_PORT", 8765)
MCP_PATH = os.environ.get("LOCAL_WEB_MCP_PATH", "/mcp").strip() or "/mcp"
SEARCH_TIMEOUT_SECONDS = _env_int("LOCAL_WEB_SEARCH_TIMEOUT_SECONDS", 135)
READ_TIMEOUT_SECONDS = _env_int("LOCAL_WEB_READ_TIMEOUT_SECONDS", 135)

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


@mcp.tool()
async def web_search(
    query: str,
    max_results: int = 8,
    category: str = "general",
    language: str = "auto",
    time_range: str = "",
    engines: str = "",
    page: int = 1,
    domains: list[str] | None = None,
    required_entities: list[str] | None = None,
    refine: bool = True,
    browser_fallback: bool = True,
) -> dict[str, Any]:
    """Search with shared language, domain/entity checks and bounded recovery receipts."""
    if not 1 <= page <= 100:
        raise ValueError("page must be between 1 and 100")
    body = {
        "query": query,
        "max_results": max(1, min(max_results, 20)),
        "category": category,
        "language": language,
        "time_range": time_range,
        "engines": engines,
        "page": page,
        "domains": domains or [],
        "required_entities": required_entities or [],
        "refine": refine,
        "browser_fallback": browser_fallback,
    }
    async with httpx.AsyncClient(timeout=SEARCH_TIMEOUT_SECONDS) as client:
        response = await client.post(
            f"{CRAWL4AI_URL}/search", json=body, headers=_crawl4ai_headers()
        )
        response.raise_for_status()
        return response.json()


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

    markdown = payload.get("markdown")
    if markdown is None:
        markdown = payload.get("result", "")
    if not isinstance(markdown, str):
        markdown = ""
    return {
        "url": url,
        "source": payload.get("source", "crawl4ai"),
        "method": payload.get("method"),
        "crawl4ai_url": CRAWL4AI_URL,
        "cache_mode": cache_mode,
        "effective_cache_mode": payload.get("effective_cache_mode", cache_mode),
        "markdown": markdown,
        "assessment": payload.get("assessment"),
        "next_action": payload.get("next_action"),
        "title": payload.get("title"),
        "captcha": payload.get("captcha"),
        "initial_captcha": payload.get("initial_captcha"),
        "browser_fallback": payload.get("browser_fallback"),
        "status_code": payload.get("status_code"),
        "initial_status_code": payload.get("initial_status_code"),
        "links": payload.get("links")
        if (payload.get("browser_fallback") or {}).get("status") == "accepted"
        else None,
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
            status["crawl4ai"] = (
                response.json()
                if response.headers.get("content-type", "").startswith("application/json")
                else response.text
            )
        except Exception as exc:  # noqa: BLE001
            status["crawl4ai"] = repr(exc)

    return status


def _register_research_tools() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from mcp_tools import register_research_tools

    register_research_tools(mcp, CRAWL4AI_URL, _crawl4ai_headers)


_register_research_tools()


if __name__ == "__main__":
    if MCP_TRANSPORT in {"http", "streamable-http", "streamable_http", "streaming"}:
        mcp.run(transport="streamable-http")
    elif MCP_TRANSPORT == "sse":
        mcp.run(transport="sse")
    else:
        mcp.run()
