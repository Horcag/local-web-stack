import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import FastAPI

from service.research import create_router
from service.research.content import assess_content, canonical_url
from service.research.models import DiscoverRequest, RunRequest
from service.research.runner import Runner, coverage
from service.research.store import Store

TEXT = "This is substantive source evidence about research findings and limitations. " * 20


class ResearchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        delay = patch.dict(os.environ, {"LOCAL_WEB_SEARCH_DELAY_SECONDS": "0"})
        delay.start()
        self.addCleanup(delay.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "research.sqlite3"
        self.store = Store(self.path)
        self.run = self.store.create(
            RunRequest(
                title="Test", query="topic", subtopics=["detail"], variants=["variant"]
            ).model_dump()
        )
        self.app = FastAPI()
        self.app.include_router(create_router(db_path=self.path))

    def tearDown(self):
        self.temp.cleanup()

    def source(self, url="https://example.org/thread?page=1", **kwargs):
        return self.store.add_source(self.run["id"], url, "Source", {"kind": "search"}, **kwargs)

    async def request(self, method, path, **kwargs):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test"
        ) as client:
            return await client.request(method, "/research" + path, **kwargs)

    def test_canonical_persistence_and_provenance(self):
        first = self.source("https://EXAMPLE.org/thread?page=2&utm_source=x#post")
        other = self.store.add_source(
            self.run["id"], "https://example.org/thread?page=2", "other", {"kind": "forum"}
        )
        self.assertEqual(first["id"], other["id"])
        self.assertEqual(2, len(Store(self.path).source(first["id"])["provenance"]))
        self.assertNotEqual(first["id"], self.source()["id"])
        self.assertEqual("https://example.org/", canonical_url("https://example.org"))
        for url in [
            "file:///tmp/x",
            "http://localhost/",
            "http://127.0.0.1/",
            "https://user:pass@example.org/",
        ]:
            with self.assertRaises(ValueError):
                canonical_url(url)

    def test_assessment_gate_discussion_empty_partial(self):
        self.assertEqual(
            "blocked", assess_content("# Verify you are human", "Just a moment")["status"]
        )
        self.assertEqual("blocked", assess_content(TEXT, "Article", 403)["status"])
        self.assertEqual(
            "read",
            assess_content("This article discusses CAPTCHA. " + TEXT, "CAPTCHA research")["status"],
        )
        self.assertEqual("partial", assess_content("A short extract")["status"])
        self.assertEqual("failed", assess_content("")["status"])

    async def test_retry_skip_import_and_export(self):
        source = self.source()
        path = "/sources/" + source["id"]
        mismatch = await self.request(
            "POST",
            path + "/browser-result",
            json={"url": "https://other.org/", "markdown": TEXT, "method": "chrome"},
        )
        self.assertEqual(409, mismatch.status_code)
        imported = await self.request(
            "POST",
            path + "/browser-result",
            json={"url": source["url"], "markdown": TEXT, "method": "chrome"},
        )
        self.assertEqual("read", imported.json()["status"])
        self.assertEqual("browser_import", imported.json()["provenance"][-1]["kind"])
        gate = await self.request(
            "POST",
            path + "/browser-result",
            json={"url": source["url"], "markdown": "Verify you are human", "method": "chrome"},
        )
        self.assertEqual("blocked", gate.json()["status"])
        retry = await self.request("POST", path + "/retry")
        self.assertEqual(1, retry.json()["retry_count"])
        skip = await self.request("POST", path + "/skip")
        self.assertEqual("skipped", skip.json()["status"])
        export = await self.request("GET", "/runs/" + self.run["id"] + "/export")
        self.assertIn("Search coverage: unknown", export.text)
        data = await self.request("GET", "/runs/" + self.run["id"] + "/export?format=json")
        self.assertEqual("skipped", data.json()["sources"][0]["status"])

    async def test_atomic_claim_and_cancel_requeue(self):
        source = self.source()
        claim = self.store.claim(self.run["id"])
        self.assertIsNone(Store(self.path).claim(self.run["id"]))
        self.store.finish(source["id"], {"status": "queued"}, claim[1])

        class Client:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def post(self, *args, **kw):
                raise asyncio.CancelledError()

        with patch("service.research.runner.httpx.AsyncClient", Client):
            with self.assertRaises(asyncio.CancelledError):
                await Runner(self.store).collect(self.run["id"], 1)
        self.assertEqual("queued", self.store.source(source["id"])["status"])
        self.assertEqual(2, self.store.source(source["id"])["attempts"])

    async def test_expanded_paginated_discovery_receipts(self):
        calls = []

        class Client:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def get(self, url, **kw):
                calls.append(kw["params"])
                return httpx.Response(
                    200,
                    request=httpx.Request("GET", url),
                    json={
                        "results": [
                            {"url": "https://example.org/a", "title": "A", "engine": "test"}
                        ],
                        "unresponsive_engines": [["engine", "timeout"]],
                    },
                )

        with patch("service.research.runner.httpx.AsyncClient", Client):
            first = await Runner(self.store).discover(self.run["id"], DiscoverRequest())
            second = await Runner(self.store).discover(self.run["id"], DiscoverRequest())
        self.assertEqual(1, first["discovered_results"])
        self.assertEqual(0, second["discovered_results"])
        self.assertEqual(6, len(calls))
        self.assertEqual({"topic", "variant", "topic detail"}, {c["q"] for c in calls})
        self.assertEqual({1, 2}, {c["pageno"] for c in calls})
        self.assertEqual(1, len(self.store.sources(self.run["id"])))
        self.assertEqual(6, len(self.store.sources(self.run["id"])[0]["provenance"]))
        self.assertEqual(6, len(coverage(self.store, self.run["id"])["engine_errors"]))

    async def test_browser_fallback_forum_bounds_and_budgets(self):
        source = self.source()

        async def browser(url):
            return {"markdown": TEXT, "title": "Forum", "method": "cdp"}

        runner = Runner(self.store, browser)
        result = await runner.assess(source, {"markdown": "Verify you are human"}, "crawl4ai")
        self.assertEqual("read", result["status"])
        self.assertEqual("cdp", result["method"])
        pending = await Runner(self.store).assess(
            source, {"markdown": "Verify you are human"}, "crawl4ai"
        )
        self.assertEqual("browser_pending", pending["status"])
        runner.forum_links(
            source,
            {
                "links": {
                    "internal": [
                        {"href": "?page=2", "text": "Next"},
                        {"href": "https://other.org/?page=3", "text": "Next"},
                    ]
                }
            },
        )
        self.assertEqual(2, len(self.store.sources(self.run["id"])))
        for n in range(3, 30):
            runner.forum_links(
                source, {"metadata": {"links": [{"href": "?page=" + str(n), "text": "Next"}]}}
            )
        self.assertEqual(13, len(self.store.sources(self.run["id"])))
        run = self.store.create(
            RunRequest(title="Small", query="x", max_requests=1, max_content_chars=100).model_dump()
        )
        small = self.store.add_source(run["id"], "https://example.org/small", "", {})
        claim = self.store.claim(run["id"])
        self.assertFalse(self.store.reserve(run["id"]))
        result = self.store.finish(small["id"], {"status": "read", "markdown": TEXT}, claim[1])
        self.assertEqual("partial", result["status"])
        self.assertEqual(100, len(result["markdown"]))
        self.assertEqual(100, self.store.run(run["id"])["content_chars"])

    async def test_collect_network_read_and_failure(self):
        self.source()

        class Client:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def post(self, url, **kw):
                return httpx.Response(
                    200,
                    request=httpx.Request("POST", url),
                    json={"markdown": TEXT, "title": "Good"},
                )

        with patch("service.research.runner.httpx.AsyncClient", Client):
            result = await Runner(self.store).collect(self.run["id"], 1)
        self.assertEqual("read", result["sources"][0]["status"])
        self.assertEqual(1, result["coverage"]["successful"])
        self.assertEqual(1, result["coverage"]["attempted"])
        self.assertEqual("unknown", result["coverage"]["search_coverage"])

    async def test_concurrent_claims_and_global_query_budget(self):
        for n in range(12):
            self.source("https://example.org/" + str(n))
        claims = await asyncio.gather(
            *(asyncio.to_thread(Store(self.path).claim, self.run["id"]) for _ in range(20))
        )
        claimed = [c for c in claims if c]
        self.assertEqual(12, len(claimed))
        self.assertEqual(12, len({c[0]["id"] for c in claimed}))
        self.assertEqual(12, self.store.run(self.run["id"])["requests_used"])
        self.assertEqual(
            12, sum(s["status"] == "reading" for s in self.store.sources(self.run["id"]))
        )
        run = self.store.create(RunRequest(title="Bound", query="one", max_queries=2).model_dump())
        self.assertTrue(self.store.reserve(run["id"], "one", 1))
        self.assertTrue(self.store.reserve(run["id"], "two", 1))
        self.assertFalse(self.store.reserve(run["id"], "three", 1))
        self.assertFalse(self.store.reserve(run["id"], "one", 3))

    async def test_failed_network_and_cancelled_discovery_receipts(self):
        self.source()

        class Client:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def post(self, *args, **kw):
                raise httpx.ConnectError("offline")

            async def get(self, *args, **kw):
                raise asyncio.CancelledError()

        with patch("service.research.runner.httpx.AsyncClient", Client):
            result = await Runner(self.store).collect(self.run["id"], 1)
            with self.assertRaises(asyncio.CancelledError):
                await Runner(self.store).discover(self.run["id"], DiscoverRequest())
        self.assertEqual("failed", result["sources"][0]["status"])
        self.assertIn("offline", result["sources"][0]["error"])
        self.assertEqual("finished", self.store.searches(self.run["id"])[0]["status"])
        self.assertEqual("cancelled", self.store.searches(self.run["id"])[0]["data"]["error"])

    async def test_import_while_reading_rejected_and_retry_bounded(self):
        source = self.source()
        path = "/sources/" + source["id"]
        claim = self.store.claim(self.run["id"])
        response = await self.request(
            "POST",
            path + "/browser-result",
            json={"url": source["url"], "markdown": TEXT, "method": "chrome"},
        )
        self.assertEqual(409, response.status_code)
        self.store.finish(source["id"], {"status": "failed"}, claim[1])
        for _ in range(5):
            self.assertEqual(200, (await self.request("POST", path + "/retry")).status_code)
        self.assertEqual(409, (await self.request("POST", path + "/retry")).status_code)
        with self.store.connection() as db:
            db.execute(
                "UPDATE sources SET status='reading',lease=0,token='old' WHERE id=?",
                (source["id"],),
            )
        reclaimed = self.store.claim(self.run["id"])
        self.assertIsNotNone(reclaimed)
        self.assertNotEqual("old", reclaimed[1])
        self.assertEqual(
            "reading", self.store.finish(source["id"], {"status": "read"}, "old")["status"]
        )

    async def test_concurrent_discover_and_collect_operations(self):
        calls = []

        class Client:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def get(self, url, **kw):
                await asyncio.sleep(0)
                calls.append(("search", kw["params"]["q"], kw["params"]["pageno"]))
                return httpx.Response(
                    200,
                    request=httpx.Request("GET", url),
                    json={
                        "results": [
                            {
                                "url": "https://example.org/" + kw["params"]["q"].replace(" ", "-"),
                                "title": "A",
                            }
                        ]
                    },
                )

            async def post(self, url, **kw):
                await asyncio.sleep(0)
                calls.append(("read", kw["json"]["url"]))
                return httpx.Response(
                    200,
                    request=httpx.Request("POST", url),
                    json={"markdown": TEXT, "title": "Read"},
                )

        with patch("service.research.runner.httpx.AsyncClient", Client):
            await asyncio.gather(
                *(
                    Runner(Store(self.path)).discover(self.run["id"], DiscoverRequest())
                    for _ in range(2)
                )
            )
            await asyncio.gather(
                *(Runner(Store(self.path)).collect(self.run["id"], 5) for _ in range(2))
            )
        searches = [c for c in calls if c[0] == "search"]
        reads = [c for c in calls if c[0] == "read"]
        self.assertEqual(6, len(searches))
        self.assertEqual(6, len(set(searches)))
        self.assertEqual(3, len(reads))
        self.assertEqual(3, len(set(reads)))
        self.assertEqual(3, coverage(self.store, self.run["id"])["successful"])

    async def test_empty_fallback_truncation_and_navigation_filter(self):
        source = self.source()
        called = []

        async def browser(url):
            called.append(url)
            return {"markdown": TEXT, "title": "Visible", "truncated": True, "method": "cdp"}

        runner = Runner(self.store, browser)
        result = await runner.assess(source, {"markdown": [], "status_code": None}, "crawl4ai")
        self.assertEqual("browser_pending", result["status"])
        self.assertIn("reader_truncated", result["reasons"])
        self.assertEqual(1, len(called))
        missing = await runner.assess(source, {"markdown": "", "status_code": 404}, "crawl4ai")
        self.assertEqual("failed", missing["status"])
        self.assertEqual(1, len(called))
        runner.forum_links(source, {"links": [{"href": "/news/next-article", "text": "Next"}]})
        self.assertEqual(1, len(self.store.sources(self.run["id"])))
        self.assertEqual("read", assess_content(TEXT, status_code=None)["status"])
