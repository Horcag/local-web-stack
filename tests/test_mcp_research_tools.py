import asyncio
import importlib.util
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

spec = importlib.util.spec_from_file_location(
    "mcp_research_subject", Path(__file__).parents[1] / "mcp/local_web_mcp.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.mark.parametrize(
    "name,args,method,path",
    [
        ("research_create", {"title": "Study", "query": "Query"}, "POST", "/research/runs"),
        (
            "research_discover",
            {"run_id": "r", "queries": ["a"], "pages": 2},
            "POST",
            "/research/runs/r/discover",
        ),
        ("research_collect", {"run_id": "r"}, "POST", "/research/runs/r/collect"),
        ("research_status", {"run_id": "r"}, "GET", "/research/runs/r/coverage"),
        ("research_source", {"source_id": "s"}, "GET", "/research/sources/s"),
        (
            "research_source",
            {"source_id": "s", "action": "retry"},
            "POST",
            "/research/sources/s/retry",
        ),
        (
            "research_source",
            {"source_id": "s", "action": "skip"},
            "POST",
            "/research/sources/s/skip",
        ),
        (
            "research_import_browser_result",
            {
                "source_id": "s",
                "url": "https://example.com",
                "markdown": "actual",
                "method": "playwright-mcp",
            },
            "POST",
            "/research/sources/s/browser-result",
        ),
        ("research_export", {"run_id": "r"}, "GET", "/research/runs/r/export"),
        ("browser_open", {"url": "https://example.com"}, "POST", "/browser/tabs"),
        ("browser_snapshot", {"tab_id": "t"}, "GET", "/browser/tabs/t"),
        ("browser_action", {"tab_id": "t", "action": "scroll"}, "POST", "/browser/tabs/t/action"),
        ("browser_close", {"tab_id": "t"}, "DELETE", "/browser/tabs/t"),
    ],
)
def test_tools_route_to_durable_api(name, args, method, path):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"ok": True})

    original = httpx.AsyncClient
    with patch(
        "mcp_tools.httpx.AsyncClient",
        side_effect=lambda **kw: original(transport=httpx.MockTransport(respond), **kw),
    ):
        result = asyncio.run(module.mcp._tool_manager.get_tool(name).fn(**args))
    assert result == {"ok": True}
    assert calls[0].method == method
    assert calls[0].url.path == path


def test_source_list_keeps_content_out_of_context_and_filters_status():
    original = httpx.AsyncClient
    payload = {
        "sources": [
            {"id": "s", "status": "read", "markdown": "large body"},
            {"id": "b", "status": "blocked"},
        ]
    }
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=payload))
    with patch(
        "mcp_tools.httpx.AsyncClient", side_effect=lambda **kw: original(transport=transport, **kw)
    ):
        result = asyncio.run(
            module.mcp._tool_manager.get_tool("research_sources").fn("r", "read", 1)
        )
    assert result == {"sources": [{"id": "s", "status": "read"}], "total_matching": 1}


@pytest.mark.parametrize(
    "name,args",
    [
        ("research_sources", {"run_id": "r", "limit": 0}),
        ("research_source", {"source_id": "s", "action": "delete"}),
        ("research_export", {"run_id": "r", "format": "html"}),
    ],
)
def test_invalid_actions_rejected_without_network(name, args):
    with pytest.raises(ValueError):
        asyncio.run(module.mcp._tool_manager.get_tool(name).fn(**args))


def test_search_pagination_and_empty_content_contract():
    requests = []
    original = httpx.AsyncClient

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200, json={"results": [], "markdown": "", "assessment": {"status": "failed"}}
        )

    module._SEARCH_CACHE.clear()
    with patch.object(
        module.httpx,
        "AsyncClient",
        side_effect=lambda **kw: original(transport=httpx.MockTransport(respond), **kw),
    ):
        asyncio.run(module.web_search("paging", page=2))
        asyncio.run(module.web_search("paging", page=3))
        empty = asyncio.run(module.read_url("https://example.com"))
    assert [r.url.params["pageno"] for r in requests[:2]] == ["2", "3"]
    assert empty["markdown"] == ""
    assert empty["assessment"]["status"] == "failed"
    with pytest.raises(ValueError):
        asyncio.run(module.web_search("paging", page=0))
