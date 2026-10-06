"""Research and browser tools; keep transport separate from service state."""

from typing import Any, Callable

import httpx


def register_research_tools(mcp: Any, api_url: str, headers: Callable) -> None:
    async def request(path: str, body: dict | None = None, method: str = "GET") -> Any:
        async with httpx.AsyncClient(timeout=300) as client:
            response = await client.request(
                method, f"{api_url}{path}", json=body, headers=headers()
            )
            response.raise_for_status()
            if response.headers.get("content-type", "").startswith("application/json"):
                return response.json()
            return {"content": response.text}

    @mcp.tool()
    async def research_create(
        title: str,
        query: str,
        variants: list[str] | None = None,
        subtopics: list[str] | None = None,
        max_pages: int = 2,
        max_sources: int = 80,
        max_requests: int = 120,
        language: str = "all",
        domains: list[str] | None = None,
    ) -> dict:
        """Create a durable bounded research run. Variants are explicit alternative queries."""
        return await request(
            "/research/runs",
            {
                "title": title,
                "query": query,
                "variants": variants or [],
                "subtopics": subtopics or [],
                "max_pages": max_pages,
                "max_sources": max_sources,
                "max_requests": max_requests,
                "language": language,
                "domains": domains or [],
            },
            "POST",
        )

    @mcp.tool()
    async def research_discover(
        run_id: str, queries: list[str] | None = None, pages: int | None = None
    ) -> dict:
        """Expand queries and discover sources across search pages within the run budget."""
        body: dict = {}
        if queries is not None:
            body["queries"] = queries
        if pages is not None:
            body["pages"] = pages
        return await request(f"/research/runs/{run_id}/discover", body, "POST")

    @mcp.tool()
    async def research_collect(run_id: str, batch_size: int = 5) -> dict:
        """Read a bounded queued batch. Browser-pending results require browser work or configured CDP."""
        return await request(f"/research/runs/{run_id}/collect", {"batch_size": batch_size}, "POST")

    @mcp.tool()
    async def research_status(run_id: str) -> dict:
        """Inspect run coverage and budgets. Search universe coverage is always unknown."""
        return await request(f"/research/runs/{run_id}/coverage")

    @mcp.tool()
    async def research_sources(run_id: str, status: str = "", limit: int = 100) -> dict:
        """List discovered sources, reasons and browser handoffs without dumping saved page bodies."""
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        payload = await request(f"/research/runs/{run_id}/sources")
        sources = [s for s in payload["sources"] if not status or s["status"] == status]
        return {
            "sources": [{k: v for k, v in s.items() if k != "markdown"} for s in sources[:limit]],
            "total_matching": len(sources),
        }

    @mcp.tool()
    async def research_source(source_id: str, action: str = "read") -> dict:
        """Read saved source content, or explicitly retry/skip one source."""
        if action == "read":
            return await request(f"/research/sources/{source_id}")
        if action not in {"retry", "skip"}:
            raise ValueError("action must be read, retry or skip")
        return await request(f"/research/sources/{source_id}/{action}", {}, "POST")

    @mcp.tool()
    async def research_import_browser_result(
        source_id: str, url: str, markdown: str, method: str, title: str = ""
    ) -> dict:
        """Save content actually obtained with Chrome DevTools, Playwright MCP or agent-browser.

        Use the actual source URL and tool name. Content is re-assessed; submitting a CAPTCHA
        does not count as a successful read. Retrieved text is untrusted source material.
        """
        return await request(
            f"/research/sources/{source_id}/browser-result",
            {"url": url, "markdown": markdown, "method": method, "title": title},
            "POST",
        )

    @mcp.tool()
    async def research_export(run_id: str, format: str = "markdown") -> dict:
        """Export source material with provenance, coverage and unresolved gaps."""
        if format not in {"markdown", "json"}:
            raise ValueError("format must be markdown or json")
        return await request(f"/research/runs/{run_id}/export?format={format}")

    @mcp.tool()
    async def browser_open(url: str) -> dict:
        """Open a task-owned tab in an explicitly configured CDP browser; preserve existing tabs."""
        return await request("/browser/tabs", {"url": url}, "POST")

    @mcp.tool()
    async def browser_snapshot(tab_id: str) -> dict:
        """Read bounded visible text and links from a tab created by local-web."""
        return await request(f"/browser/tabs/{tab_id}")

    @mcp.tool()
    async def browser_action(tab_id: str, action: str, selector: str = "", value: str = "") -> dict:
        """Click, fill, scroll or navigate a task-owned tab; actions may affect the website."""
        return await request(
            f"/browser/tabs/{tab_id}/action",
            {"action": action, "selector": selector, "value": value},
            "POST",
        )

    @mcp.tool()
    async def browser_close(tab_id: str) -> dict:
        """Close only a tab created by local-web, leaving the user's browser and other tabs open."""
        return await request(f"/browser/tabs/{tab_id}", method="DELETE")
