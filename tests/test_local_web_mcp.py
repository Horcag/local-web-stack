from __future__ import annotations

import asyncio
import importlib.util
import inspect
from pathlib import Path
import unittest
from unittest.mock import patch

import httpx

MODULE_PATH = Path(__file__).parents[1] / "mcp" / "local_web_mcp.py"
SPEC = importlib.util.spec_from_file_location("local_web_mcp", MODULE_PATH)
assert SPEC and SPEC.loader
local_web_mcp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(local_web_mcp)


class FakeResponse:
    status_code = 200
    headers = {"content-type": "application/json"}
    text = "ok"

    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class FakeAsyncClient:
    calls = []
    search_payload = {}

    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return FakeResponse({"results": []})

    async def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return FakeResponse(
            self.search_payload if url.endswith("/search") else {"markdown": "# Evidence"}
        )


class LocalWebMcpTests(unittest.TestCase):
    def setUp(self):
        FakeAsyncClient.calls.clear()
        FakeAsyncClient.search_payload = {
            "count": 1,
            "results": [{"title": "Health evidence", "url": "https://nih.gov/report"}],
            "actual_requests": 1,
            "attempts": [{"stage": "initial", "status": "completed"}],
            "requested_engines": "yandex,mojeek",
            "engines": ["yandex"],
        }

    def test_streamable_http_is_stateless_json(self):
        self.assertTrue(local_web_mcp.mcp.settings.stateless_http)
        self.assertTrue(local_web_mcp.mcp.settings.json_response)

    def test_tools_are_async_to_avoid_blocking_the_mcp_event_loop(self):
        for fn in (
            local_web_mcp.web_search,
            local_web_mcp.read_url,
            local_web_mcp.local_web_health,
        ):
            self.assertTrue(inspect.iscoroutinefunction(fn))

    def test_search_and_read_use_async_shared_service_http(self):
        with patch.object(local_web_mcp.httpx, "AsyncClient", FakeAsyncClient):
            search = asyncio.run(
                local_web_mcp.web_search("health evidence", engines="yandex,mojeek")
            )
            read = asyncio.run(local_web_mcp.read_url("https://nih.gov/report"))
        self.assertEqual(search, FakeAsyncClient.search_payload)
        self.assertEqual(read["markdown"], "# Evidence")
        self.assertEqual(
            [(method, url) for method, url, _ in FakeAsyncClient.calls],
            [
                ("POST", f"{local_web_mcp.CRAWL4AI_URL}/search"),
                ("POST", f"{local_web_mcp.CRAWL4AI_URL}/md"),
            ],
        )

    def test_search_forwards_all_explicit_constraints_and_authenticated_route(self):
        with (
            patch.object(local_web_mcp.httpx, "AsyncClient", FakeAsyncClient),
            patch.object(local_web_mcp, "CRAWL4AI_API_TOKEN", "fixture-token"),
        ):
            asyncio.run(
                local_web_mcp.web_search(
                    'health "evidence report"',
                    max_results=5,
                    category="science",
                    language="en",
                    time_range="month",
                    engines="mojeek",
                    page=3,
                    domains=["nih.gov"],
                    required_entities=["NIH"],
                    refine=False,
                    browser_fallback=False,
                )
            )
        kwargs = FakeAsyncClient.calls[0][2]
        self.assertEqual(kwargs["headers"], {"Authorization": "Bearer fixture-token"})
        self.assertEqual(
            kwargs["json"],
            {
                "query": 'health "evidence report"',
                "max_results": 5,
                "category": "science",
                "language": "en",
                "time_range": "month",
                "engines": "mojeek",
                "page": 3,
                "domains": ["nih.gov"],
                "required_entities": ["NIH"],
                "refine": False,
                "browser_fallback": False,
            },
        )

    def test_shared_backend_receipts_and_cache_metadata_survive_mcp(self):
        FakeAsyncClient.search_payload.update(
            count=0,
            results=[],
            cache_hit=True,
            actual_requests=0,
            attempts=[],
            cached_attempts=[{"stage": "initial", "status": "completed"}],
            dropped_engines=["mojeek"],
            search_coverage="unknown",
            status="degraded",
            engine_status=[{"engine": "mojeek", "status": "filtered", "attempted": True}],
        )
        with patch.object(local_web_mcp.httpx, "AsyncClient", FakeAsyncClient):
            result = asyncio.run(local_web_mcp.web_search("health evidence"))
        self.assertEqual(FakeAsyncClient.search_payload, result)

    def test_health_searxng_probe_includes_local_client_ip(self):
        with patch.object(local_web_mcp.httpx, "AsyncClient", FakeAsyncClient):
            asyncio.run(local_web_mcp.local_web_health())
        method, url, kwargs = FakeAsyncClient.calls[0]
        self.assertEqual(method, "GET")
        self.assertEqual(url, f"{local_web_mcp.SEARXNG_URL}/search")
        self.assertEqual(kwargs["headers"], {"X-Real-IP": local_web_mcp.SEARXNG_CLIENT_IP})

    def test_invalid_page_rejected_before_network(self):
        with patch.object(local_web_mcp.httpx, "AsyncClient", FakeAsyncClient):
            for page in (0, 101):
                with self.assertRaises(ValueError):
                    asyncio.run(local_web_mcp.web_search("health evidence", page=page))
        self.assertEqual([], FakeAsyncClient.calls)

    def test_service_http_error_is_not_returned_as_success(self):
        original = httpx.AsyncClient
        transport = httpx.MockTransport(
            lambda request: httpx.Response(503, json={"error": "unavailable"})
        )
        with patch.object(
            local_web_mcp.httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: original(transport=transport, **kwargs),
        ):
            with self.assertRaises(httpx.HTTPStatusError):
                asyncio.run(local_web_mcp.web_search("health evidence"))


if __name__ == "__main__":
    unittest.main()
