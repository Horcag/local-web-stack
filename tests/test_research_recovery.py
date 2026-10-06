import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from service.research.models import RunRequest
from service.research.store import Store


class SearchRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "research.sqlite3"
        self.store = Store(self.path)
        self.run = self.store.create(RunRequest(title="Recovery", query="topic").model_dump())

    def expire(self):
        with self.store.connection() as db:
            row = db.execute("SELECT data FROM searches").fetchone()
            data = json.loads(row["data"])
            data["lease"] = 0
            db.execute("UPDATE searches SET data=?", (json.dumps(data),))

    async def test_reopen_after_crash_and_stale_completion(self):
        token = self.store.reserve(self.run["id"], "topic", 1)
        self.assertFalse(Store(self.path).reserve(self.run["id"], "topic", 1))
        self.expire()
        reopened = Store(self.path)
        recovered = reopened.reserve(self.run["id"], "topic", 1)
        self.assertTrue(recovered)
        self.assertNotEqual(token, recovered)
        reopened.search_result(
            self.run["id"], "topic", 1, {"error": "old_worker"}, reservation=token
        )
        self.assertEqual("running", reopened.searches(self.run["id"])[0]["status"])
        reopened.search_result(
            self.run["id"], "topic", 1, {"result_count": 4}, reservation=recovered
        )
        receipt = Store(self.path).searches(self.run["id"])[0]
        self.assertEqual(2, receipt["data"]["attempts"])
        self.assertEqual(4, receipt["data"]["result_count"])
        self.assertEqual(0, receipt["data"]["lease"])
        self.assertFalse(reopened.reserve(self.run["id"], "topic", 1))

    async def test_concurrent_retry_claim_bounded_attempts_and_budget(self):
        run_id = self.run["id"]
        token = self.store.reserve(run_id, "topic", 1)
        self.store.search_result(run_id, "topic", 1, {"error": "cancelled"}, reservation=token)
        claims = await asyncio.gather(
            *(asyncio.to_thread(Store(self.path).reserve, run_id, "topic", 1) for _ in range(12))
        )
        winners = [claim for claim in claims if claim]
        self.assertEqual(1, len(winners))
        self.store.search_result(run_id, "topic", 1, {"error": "offline"}, reservation=winners[0])
        third = self.store.reserve(run_id, "topic", 1)
        self.assertTrue(third)
        self.store.search_result(run_id, "topic", 1, {"error": "offline"}, reservation=third)
        self.assertFalse(self.store.reserve(run_id, "topic", 1))
        self.assertEqual(3, self.store.run(run_id)["requests_used"])
        small = self.store.create(
            RunRequest(title="Budget", query="x", max_requests=1).model_dump()
        )
        token = self.store.reserve(small["id"], "x", 1)
        self.store.search_result(small["id"], "x", 1, {"error": "failed"}, reservation=token)
        self.assertFalse(self.store.reserve(small["id"], "x", 1))

    async def test_legacy_running_receipt_recovered_without_migration(self):
        with self.store.connection() as db:
            db.execute(
                "INSERT INTO searches VALUES (?,?,?,?,?)",
                (self.run["id"], "topic", 1, "running", "{}"),
            )
        token = Store(self.path).reserve(self.run["id"], "topic", 1)
        self.assertTrue(token)
        self.assertEqual(2, self.store.searches(self.run["id"])[0]["data"]["attempts"])
