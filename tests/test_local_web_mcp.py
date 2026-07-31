from __future__ import annotations

import asyncio
import importlib.util
import inspect
from pathlib import Path
import unittest
from unittest.mock import patch


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
    calls: list[tuple[str, str]] = []
    get_calls: list[tuple[str, dict[str, str]]] = []
    get_params: list[dict[str, str]] = []
    search_results: list[dict[str, str]] = []

    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, url: str, **kwargs) -> FakeResponse:
        self.calls.append(("GET", url))
        self.get_calls.append((url, kwargs.get("headers", {})))
        self.get_params.append(kwargs.get("params", {}))
        results = self.search_results or [
            {
                "title": "Result",
                "url": "https://example.test",
                "content": "Body",
                "engine": "test",
            }
        ]
        return FakeResponse({"results": list(results)})

    async def post(self, url: str, **_kwargs) -> FakeResponse:
        self.calls.append(("POST", url))
        return FakeResponse({"markdown": "# Example"})


class LocalWebMcpTests(unittest.TestCase):
    def setUp(self) -> None:
        local_web_mcp._SEARCH_CACHE.clear()
        FakeAsyncClient.calls.clear()
        FakeAsyncClient.get_calls.clear()
        FakeAsyncClient.get_params.clear()
        FakeAsyncClient.search_results = []

    def test_streamable_http_is_stateless_json(self) -> None:
        self.assertTrue(local_web_mcp.mcp.settings.stateless_http)
        self.assertTrue(local_web_mcp.mcp.settings.json_response)

    def test_tools_are_async_to_avoid_blocking_the_mcp_event_loop(self) -> None:
        self.assertTrue(inspect.iscoroutinefunction(local_web_mcp.web_search))
        self.assertTrue(inspect.iscoroutinefunction(local_web_mcp.read_url))
        self.assertTrue(inspect.iscoroutinefunction(local_web_mcp.local_web_health))

    def test_search_and_read_use_async_http(self) -> None:
        with patch.object(local_web_mcp.httpx, "AsyncClient", FakeAsyncClient):
            search = asyncio.run(local_web_mcp.web_search("example", engines="test"))
            read = asyncio.run(local_web_mcp.read_url("https://example.test"))

        self.assertEqual(search["count"], 1)
        self.assertEqual(read["markdown"], "# Example")
        self.assertEqual(
            FakeAsyncClient.calls,
            [
                ("GET", f"{local_web_mcp.SEARXNG_URL}/search"),
                ("POST", f"{local_web_mcp.CRAWL4AI_URL}/md"),
            ],
        )

    def test_searxng_requests_include_the_local_client_ip(self) -> None:
        with patch.object(local_web_mcp.httpx, "AsyncClient", FakeAsyncClient):
            asyncio.run(local_web_mcp.web_search("example", engines="test"))
            asyncio.run(local_web_mcp.local_web_health())

        searxng_headers = [
            headers
            for url, headers in FakeAsyncClient.get_calls
            if url.startswith(local_web_mcp.SEARXNG_URL)
        ]
        self.assertEqual(
            searxng_headers,
            [
                {"X-Real-IP": local_web_mcp.SEARXNG_CLIENT_IP},
                {"X-Real-IP": local_web_mcp.SEARXNG_CLIENT_IP},
            ],
        )

    def test_categories_is_never_sent_together_with_engines(self) -> None:
        # SearXNG unions the two parameters, so sending both expands the
        # category back into its full pool and annuls the engine filter.
        with patch.object(local_web_mcp.httpx, "AsyncClient", FakeAsyncClient):
            asyncio.run(local_web_mcp.web_search("example", engines="test"))
            asyncio.run(local_web_mcp.web_search("example"))

        explicit, defaulted = FakeAsyncClient.get_params
        self.assertEqual(explicit["engines"], "test")
        self.assertNotIn("categories", explicit)
        self.assertEqual(defaulted["engines"], local_web_mcp.DEFAULT_SEARCH_ENGINES)
        self.assertNotIn("categories", defaulted)

    def test_categories_is_sent_when_no_engines_are_selected(self) -> None:
        with patch.object(local_web_mcp.httpx, "AsyncClient", FakeAsyncClient):
            asyncio.run(local_web_mcp.web_search("example", category="news"))

        params = FakeAsyncClient.get_params[0]
        self.assertEqual(params["categories"], "news")
        self.assertNotIn("engines", params)

    def test_reported_engines_are_the_ones_that_answered(self) -> None:
        FakeAsyncClient.search_results = [
            {
                "title": "Body",
                "url": "https://a.test",
                "content": "",
                "engine": "yandex",
            }
        ]
        with patch.object(local_web_mcp.httpx, "AsyncClient", FakeAsyncClient):
            search = asyncio.run(
                local_web_mcp.web_search("body", engines="yandex,mojeek")
            )

        self.assertEqual(search["engines"], ["yandex"])
        self.assertEqual(search["requested_engines"], "yandex,mojeek")

    def test_engine_answering_a_different_query_is_dropped(self) -> None:
        # Bing's anti-scraping placeholder: a full batch of well-formed results
        # that has nothing to do with the query.
        FakeAsyncClient.search_results = [
            {
                "title": f"Nokia {i}",
                "url": "https://nokia.test",
                "content": "",
                "engine": "bing",
            }
            for i in range(3)
        ] + [
            {
                "title": "Quantum teleportation",
                "url": "https://arxiv.test",
                "content": "",
                "engine": "yandex",
            }
        ]
        with patch.object(local_web_mcp.httpx, "AsyncClient", FakeAsyncClient):
            search = asyncio.run(
                local_web_mcp.web_search("quantum teleportation experiment")
            )

        self.assertEqual(search["dropped_engines"], ["bing"])
        self.assertEqual([item["engine"] for item in search["results"]], ["yandex"])

    def test_a_thin_off_topic_batch_is_kept(self) -> None:
        # One odd result is normal; only a whole batch with zero overlap is not.
        FakeAsyncClient.search_results = [
            {
                "title": "Nokia",
                "url": "https://nokia.test",
                "content": "",
                "engine": "mojeek",
            }
        ]
        with patch.object(local_web_mcp.httpx, "AsyncClient", FakeAsyncClient):
            search = asyncio.run(
                local_web_mcp.web_search("quantum teleportation experiment")
            )

        self.assertIsNone(search["dropped_engines"])
        self.assertEqual(search["count"], 1)


if __name__ == "__main__":
    unittest.main()
