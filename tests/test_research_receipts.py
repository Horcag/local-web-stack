import asyncio
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from service.research.models import RunRequest
from service.research.runner import coverage, paced_search
from service.research.store import Store


class ReceiptTests(unittest.IsolatedAsyncioTestCase):
    async def test_hash_history_dedup_and_concurrent_provenance(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = Store(Path(temporary) / "research.sqlite3")
            run = store.create(RunRequest(title="Receipts", query="x").model_dump())
            source = store.add_source(run["id"], "https://example.org/one", "", {"kind": "search"})
            claimed, token = store.claim(run["id"])
            store.add_source(run["id"], source["url"], "", {"kind": "later_discovery"})
            result = store.finish(
                source["id"],
                {
                    "status": "read",
                    "markdown": "retained text",
                    "method": "browser",
                    "provenance": [*claimed["provenance"], {"kind": "browser"}],
                },
                token,
            )
            self.assertEqual(
                {"search", "later_discovery", "browser"}, {p["kind"] for p in result["provenance"]}
            )
            self.assertEqual(hashlib.sha256(b"retained text").hexdigest(), result["content_sha256"])
            self.assertEqual("browser", result["history"][0]["method"])
            second = store.add_source(run["id"], "https://example.org/two", "", {})
            for _ in range(55):
                store.finish(
                    second["id"],
                    {"status": "read", "markdown": "retained text", "method": "import"},
                )
            self.assertEqual(50, len(store.source(second["id"])["history"]))
            result = coverage(store, run["id"])
            self.assertEqual(1, result["unique_content_hashes"])
            self.assertEqual(1, result["content_duplicates"])

    async def test_shared_search_pacing(self):
        class Client:
            async def get(self, url, **kwargs):
                return url

        async def sleep(delay):
            delays.append(delay)

        delays = []
        with (
            patch.dict("os.environ", {"LOCAL_WEB_SEARCH_DELAY_SECONDS": "1"}),
            patch("service.research.runner._LAST_SEARCH", 10),
            patch("service.research.runner._search_clock", return_value=10.2),
            patch("service.research.runner.asyncio.sleep", sleep),
        ):
            results = await asyncio.gather(
                paced_search(Client(), "one"), paced_search(Client(), "two")
            )
        self.assertEqual(["one", "two"], results)
        self.assertAlmostEqual(0.8, delays[0])
        self.assertAlmostEqual(1.0, delays[1])
