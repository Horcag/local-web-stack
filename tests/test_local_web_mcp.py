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

    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, url: str, **_kwargs) -> FakeResponse:
        self.calls.append(("GET", url))
        return FakeResponse(
            {
                "results": [
                    {
                        "title": "Result",
                        "url": "https://example.test",
                        "content": "Body",
                        "engine": "test",
                    }
                ]
            }
        )

    async def post(self, url: str, **_kwargs) -> FakeResponse:
        self.calls.append(("POST", url))
        return FakeResponse({"markdown": "# Example"})


class LocalWebMcpTests(unittest.TestCase):
    def setUp(self) -> None:
        local_web_mcp._SEARCH_CACHE.clear()
        FakeAsyncClient.calls.clear()

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


if __name__ == "__main__":
    unittest.main()
