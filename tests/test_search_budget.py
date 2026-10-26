"""Real SQLite admission contracts under retries and competing workers."""

import asyncio
import json

import pytest

from service.research.models import RunRequest
from service.research.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "research.sqlite3")


def create(store, max_requests=10):
    return store.create(
        RunRequest(title="Budget", query="health evidence", max_requests=max_requests).model_dump()
    )["id"]


def expire(store, run_id):
    with store.connection() as db:
        row = db.execute("SELECT data FROM searches WHERE run_id=?", (run_id,)).fetchone()
        metadata = json.loads(row["data"])
        metadata["lease"] = 0
        db.execute("UPDATE searches SET data=? WHERE run_id=?", (json.dumps(metadata), run_id))


def test_first_dispatch_prepaid_additional_dispatches_charged_and_fourth_denied(store):
    run_id = create(store)
    token = store.reserve(run_id, "health evidence", 1)
    assert store.run(run_id)["requests_used"] == 1
    for count, expected_budget in [(1, 1), (2, 2), (3, 3)]:
        assert store.admit_search_attempt(run_id, "health evidence", 1, token)
        assert store.run(run_id)["requests_used"] == expected_budget
        assert store.searches(run_id)[0]["data"]["dispatches"] == count
    assert not store.admit_search_attempt(run_id, "health evidence", 1, token)
    assert store.run(run_id)["requests_used"] == 3


@pytest.mark.parametrize("maximum", [1, 2, 3])
def test_run_budget_limits_dispatches(store, maximum):
    run_id = create(store, maximum)
    token = store.reserve(run_id, "health evidence", 1)
    assert [store.admit_search_attempt(run_id, "health evidence", 1, token) for _ in range(4)] == [
        True
    ] * maximum + [False] * (4 - maximum)
    assert store.run(run_id)["requests_used"] == maximum


@pytest.mark.parametrize("invalid", ["foreign_token", "expired", "finished", "missing"])
def test_admission_requires_current_live_owner_and_query_lease(store, invalid):
    run_id = create(store)
    token = store.reserve(run_id, "health evidence", 1)
    if invalid == "expired":
        expire(store, run_id)
    elif invalid == "finished":
        store.search_result(run_id, "health evidence", 1, {"error": "cancelled"}, token)
    elif invalid == "foreign_token":
        token = "unrelated-worker"
    query = "missing query" if invalid == "missing" else "health evidence"
    assert not store.admit_search_attempt(run_id, query, 1, token)
    assert store.run(run_id)["requests_used"] == 1
    assert store.searches(run_id)[0]["data"].get("dispatches", 0) == 0


def test_cancelled_old_worker_cannot_finish_or_charge_new_lease(store):
    run_id = create(store)
    old = store.reserve(run_id, "health evidence", 1)
    assert store.admit_search_attempt(run_id, "health evidence", 1, old)
    expire(store, run_id)
    current = Store(store.path).reserve(run_id, "health evidence", 1)
    assert current and current != old
    assert not store.admit_search_attempt(run_id, "health evidence", 1, old)
    store.search_result(run_id, "health evidence", 1, {"error": "cancelled"}, old)
    receipt = store.searches(run_id)[0]
    assert receipt["status"] == "running"
    assert receipt["data"]["token"] == current
    assert receipt["data"]["lease"] > 0
    assert "error" not in receipt["data"]
    assert store.admit_search_attempt(run_id, "health evidence", 1, current)
    assert store.run(run_id)["requests_used"] == 2
    store.search_result(run_id, "health evidence", 1, {"error": "cancelled"}, current)
    assert store.searches(run_id)[0]["data"]["lease"] == 0
    assert not store.admit_search_attempt(run_id, "health evidence", 1, current)


def test_parallel_claims_and_dispatches_cannot_overflow_budget(store):
    run_id = create(store, 3)

    async def run():
        claims = await asyncio.gather(
            *(
                asyncio.to_thread(Store(store.path).reserve, run_id, "health evidence", 1)
                for _ in range(12)
            )
        )
        winners = [claim for claim in claims if claim]
        assert len(winners) == 1
        admissions = await asyncio.gather(
            *(
                asyncio.to_thread(
                    Store(store.path).admit_search_attempt, run_id, "health evidence", 1, winners[0]
                )
                for _ in range(12)
            )
        )
        assert admissions.count(True) == 3

    asyncio.run(run())
    assert store.run(run_id)["requests_used"] == 3
    assert store.searches(run_id)[0]["data"]["dispatches"] == 3


def test_source_claim_competes_atomically_with_additional_search_dispatch(store):
    run_id = create(store, 2)
    token = store.reserve(run_id, "health evidence", 1)
    assert store.admit_search_attempt(run_id, "health evidence", 1, token)
    store.add_source(
        run_id, "https://nih.gov/report", "Health report", {"query": "health evidence"}
    )

    async def run():
        outcomes = await asyncio.gather(
            asyncio.to_thread(Store(store.path).claim, run_id),
            asyncio.to_thread(
                Store(store.path).admit_search_attempt, run_id, "health evidence", 1, token
            ),
        )
        assert sum(bool(outcome) for outcome in outcomes) == 1

    asyncio.run(run())
    assert store.run(run_id)["requests_used"] == 2


def test_content_budget_denies_dispatch_even_when_first_was_prepaid(store):
    run_id = create(store)
    token = store.reserve(run_id, "health evidence", 1)
    with store.connection() as db:
        row = db.execute("SELECT data FROM runs WHERE id=?", (run_id,)).fetchone()
        data = json.loads(row["data"])
        data["content_chars"] = data["max_content_chars"]
        db.execute("UPDATE runs SET data=? WHERE id=?", (json.dumps(data), run_id))
    assert not store.admit_search_attempt(run_id, "health evidence", 1, token)
    assert store.run(run_id)["requests_used"] == 1
