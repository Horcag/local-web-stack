"""Offline integration contracts for durable retries and shared HTTP search admission."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from service.research.models import DiscoverRequest, RunRequest
from service.research.router import create_router
from service.research.runner import Runner, coverage
from service.research.store import Store
from service.search import SearchBackend, create_search_router


@pytest.mark.parametrize(
    "changes",
    [
        {"required_entities": ["x" * 201]},
        {"required_entities": [" "]},
        {"language": ""},
        {"query": "x" * 1000, "subtopics": ["report"]},
        {"domains": ["nih.gov"], "variants": ["site:who.int report"]},
        {"domains": ["nih.gov"], "subtopics": ["site:who.int report"]},
    ],
)
def test_create_rejects_inputs_discovery_cannot_dispatch(integrated, changes):
    store, backend = integrated
    app = FastAPI()
    app.include_router(create_router(db_path=store.path, search_backend=backend))

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.post(
                "/research/runs", json={"title": "Boundary", "query": "health report", **changes}
            )
            assert response.status_code == 422

    asyncio.run(run())
    assert store.runs() == []
    backend._searx.assert_not_awaited()


@pytest.fixture
def integrated(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_WEB_SEARCH_DELAY_SECONDS", "0")
    monkeypatch.setenv("LOCAL_WEB_SEARCH_CACHE_TTL_SECONDS", "0")
    monkeypatch.setenv("LOCAL_WEB_SEARCH_ENGINES", "yandex")
    store = Store(tmp_path / "research.sqlite3")
    backend = SearchBackend(SimpleNamespace(configured=False))
    backend._searx = AsyncMock()
    return store, backend


def create_run(store, **changes):
    return store.create(
        RunRequest(
            **{
                "title": "Search integration",
                "query": "health evidence report",
                "max_pages": 1,
                "refine": False,
                "browser_fallback": False,
                **changes,
            }
        ).model_dump()
    )["id"]


def article():
    return {
        "url": "https://nih.gov/report",
        "title": "NIH health evidence report",
        "engine": "yandex",
    }


def receipt(store, run_id):
    return Store(store.path).searches(run_id)[0]["data"]


@pytest.mark.parametrize("failure", ["read_timeout", "providers_unavailable", "deadline"])
def test_failed_discovery_can_retry_and_persists_provider_receipts(
    integrated, monkeypatch, failure
):
    store, backend = integrated
    run_id = create_run(store)
    runner = Runner(store, search_backend=backend)
    if failure == "read_timeout":
        backend._searx.side_effect = [
            httpx.ReadTimeout("offline timeout"),
            {"results": [article()]},
        ]
        expected_error = "ReadTimeout"
    elif failure == "providers_unavailable":
        backend._searx.side_effect = [
            {"results": [], "unresponsive_engines": [["yandex", "suspended"]]},
            {"results": [article()]},
        ]
        expected_error = "providers_unavailable"
    else:
        real_timeout = asyncio.timeout
        deadlines = iter([0, 90])
        monkeypatch.setattr(
            "service.search.asyncio.timeout", lambda _: real_timeout(next(deadlines))
        )
        backend._pace = AsyncMock()

        async def upstream(*args):
            if backend._searx.await_count == 1:
                await asyncio.Future()
            return {"results": [article()]}

        backend._searx.side_effect = upstream
        expected_error = "search_deadline"

    async def run():
        first = await runner.discover(run_id, DiscoverRequest())
        failed = receipt(store, run_id)
        assert first["discovered_results"] == 0
        assert failed["error"] == expected_error
        assert failed["attempts"] == 1
        assert isinstance(failed["attempts"], int)
        assert failed["provider_attempts"]
        assert failed["lease"] == 0
        second = await runner.discover(run_id, DiscoverRequest())
        assert second["discovered_results"] == 1

    asyncio.run(run())
    persisted = receipt(store, run_id)
    assert persisted["attempts"] == 2
    assert "error" not in persisted
    assert persisted["provider_attempts"][0]["status"] == "completed"
    assert persisted["provider_attempts"][0]["provider"] == "searxng"
    assert persisted["actual_requests"] == 1
    assert store.run(run_id)["requests_used"] == 2
    assert coverage(store, run_id)["search_requests"] == 2
    source = Store(store.path).sources(run_id)[0]
    assert source["provenance"][0]["acquisition"]["provider"] == "searxng"
    assert backend._searx.await_count == 2


@pytest.mark.parametrize("failure", ["read_timeout", "providers_unavailable"])
def test_three_failed_discoveries_keep_integer_retry_count_and_fourth_does_no_io(
    integrated, failure
):
    store, backend = integrated
    run_id = create_run(store)
    runner = Runner(store, search_backend=backend)
    if failure == "read_timeout":
        backend._searx.side_effect = httpx.ReadTimeout("offline timeout")
    else:
        backend._searx.return_value = {
            "results": [],
            "unresponsive_engines": [["yandex", "timeout"]],
        }

    async def run():
        for attempt in range(1, 4):
            await runner.discover(run_id, DiscoverRequest())
            saved = receipt(store, run_id)
            assert saved["attempts"] == attempt
            assert isinstance(saved["attempts"], int)
            assert saved["error"]
            assert saved["provider_attempts"][0]["dispatched"] is True
        await runner.discover(run_id, DiscoverRequest())

    asyncio.run(run())
    assert backend._searx.await_count == 3
    assert receipt(store, run_id)["attempts"] == 3
    assert store.run(run_id)["requests_used"] == 3
    assert coverage(store, run_id)["search_requests"] == 3


def test_legitimate_empty_completed_search_does_not_retry(integrated):
    store, backend = integrated
    run_id = create_run(store)
    backend._searx.return_value = {"results": []}
    runner = Runner(store, search_backend=backend)

    async def run():
        for _ in range(2):
            result = await runner.discover(run_id, DiscoverRequest())
            assert result["discovered_results"] == 0

    asyncio.run(run())
    saved = receipt(store, run_id)
    assert "error" not in saved
    assert saved["attempts"] == 1
    assert saved["provider_attempts"][0]["status"] == "completed"
    assert saved["result_count"] == 0
    assert backend._searx.await_count == 1
    assert store.run(run_id)["requests_used"] == 1


def test_shared_http_backend_paces_both_routes_and_enforces_research_budget(
    integrated, monkeypatch
):
    store, backend = integrated
    now = [100.0]
    dispatches = []
    sleep = asyncio.sleep

    async def advance(delay):
        now[0] += delay
        await sleep(0)

    async def upstream(request, query, language, engines):
        dispatches.append((now[0], query, language, engines, request.page))
        return {"results": []}

    monkeypatch.setenv("LOCAL_WEB_SEARCH_DELAY_SECONDS", "2")
    monkeypatch.setattr("service.search.time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr("service.search.asyncio.sleep", advance)
    backend._searx.side_effect = upstream
    app = FastAPI()
    app.include_router(create_search_router(backend))
    app.include_router(create_router(db_path=store.path, search_backend=backend))
    query = 'please find health "evidence report" -ads'
    constraints = {
        "query": query,
        "language": "en",
        "domains": ["nih.gov"],
        "required_entities": ["NIH"],
        "engines": "yandex,mojeek",
        "refine": True,
        "browser_fallback": False,
    }

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            created = await client.post(
                "/research/runs",
                json={"title": "Paced run", "max_pages": 1, "max_requests": 1, **constraints},
            )
            assert created.status_code == 200
            run_id = created.json()["id"]
            direct, research = await asyncio.gather(
                client.post("/search", json=constraints),
                client.post(f"/research/runs/{run_id}/discover", json={}),
            )
            assert direct.status_code == research.status_code == 200
            assert direct.json()["actual_requests"] == 2
            coverage = research.json()["coverage"]
            assert coverage["requests_used"] == coverage["max_requests"] == 1
            assert coverage["search_requests"] == 1
            saved = receipt(store, run_id)
            assert saved["attempts"] == 1
            assert [a["status"] for a in saved["provider_attempts"]] == [
                "completed",
                "budget_exhausted",
            ]
            assert direct.json()["constraints"] == saved["constraints"]
            assert direct.json()["effective_query"] == saved["effective_query"]

    asyncio.run(run())
    assert len(dispatches) == 3
    assert all(b[0] - a[0] >= 2 for a, b in zip(dispatches, dispatches[1:]))
    for _, query, language, engines, page in dispatches:
        assert all(term in query for term in ('"evidence report"', "-ads", "site:nih.gov", "NIH"))
        assert (language, engines, page) == ("en", "yandex,mojeek", 1)


def test_http_disjoint_domain_constraints_rejected_before_io(integrated):
    _, backend = integrated
    app = FastAPI()
    app.include_router(create_search_router(backend))

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/search", json={"query": "health site:who.int", "domains": ["nih.gov"]}
            )
            assert response.status_code == 422
            assert "disjoint" in response.json()["detail"]

    asyncio.run(run())
    backend._searx.assert_not_awaited()


def test_unsupported_browser_language_preserves_budget_and_records_no_dispatch(
    integrated, monkeypatch
):
    store, backend = integrated
    backend.browser.configured = True
    backend._searx.return_value = {"results": []}
    browser = AsyncMock()
    monkeypatch.setattr("service.search.browser_search", browser)
    run_id = create_run(store, language="de", browser_fallback=True, max_requests=2)
    asyncio.run(Runner(store, search_backend=backend).discover(run_id, DiscoverRequest()))
    saved = receipt(store, run_id)
    assert saved["provider_attempts"][-1]["status"] == "rejected"
    assert saved["provider_attempts"][-1]["dispatched"] is False
    assert saved["actual_requests"] == saved["dispatches"] == 1
    assert store.run(run_id)["requests_used"] == 1
    browser.assert_not_awaited()
