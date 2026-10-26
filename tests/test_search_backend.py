"""Offline behavior contracts for bounded acquisition and honest search receipts."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from service.search import SearchBackend
from service.search_models import SearchRequest


def article(path="report", title="health evidence report", engine="yandex", **extra):
    return {"url": "https://nih.gov/" + path, "title": title, "engine": engine, **extra}


@pytest.fixture
def backend(monkeypatch):
    monkeypatch.setenv("LOCAL_WEB_SEARCH_DELAY_SECONDS", "0")
    monkeypatch.setenv("LOCAL_WEB_SEARCH_CACHE_TTL_SECONDS", "900")
    monkeypatch.setenv("LOCAL_WEB_SEARCH_ENGINES", "yandex")
    instance = SearchBackend(SimpleNamespace(configured=True))
    instance._searx = AsyncMock(return_value={"results": []})
    return instance


def execute(backend, **kwargs):
    return asyncio.run(backend.search(SearchRequest(query="health evidence report", **kwargs)))


def test_initial_refinement_browser_are_bounded_and_preserve_constraints(backend, monkeypatch):
    browser = AsyncMock(return_value={"results": [], "receipt": {"status": "completed"}})
    monkeypatch.setattr("service.search.browser_search", browser)
    request = SearchRequest(
        query='please find health "evidence report" -ads',
        language="en",
        page=4,
        domains=["nih.gov"],
        required_entities=["NIH"],
        engines="yandex,mojeek",
    )
    result = asyncio.run(backend.search(request))
    assert [a["stage"] for a in result["attempts"]] == ["initial", "refinement", "browser"]
    assert result["actual_requests"] == 3
    for call in backend._searx.call_args_list:
        passed, query, language, engines = call.args
        assert passed.page == 4
        assert language == "en"
        assert engines == "yandex,mojeek"
        assert all(term in query for term in ('"evidence report"', "-ads", "site:nih.gov", "NIH"))
    browser.assert_awaited_once_with(backend.browser, result["effective_query"], "en", 4)
    assert result["constraints"]["required_entities"] == ["nih"]


@pytest.mark.parametrize("allowed", [0, 1, 2])
def test_budget_denial_precedes_external_io(backend, monkeypatch, allowed):
    browser = AsyncMock(return_value={"results": [], "receipt": {"status": "completed"}})
    monkeypatch.setattr("service.search.browser_search", browser)
    admissions = iter([True] * allowed + [False])
    result = asyncio.run(
        backend.search(SearchRequest(query="please find health evidence"), lambda: next(admissions))
    )
    assert backend._searx.await_count == allowed
    browser.assert_not_awaited()
    assert result["actual_requests"] == allowed
    assert result["attempts"][-1]["status"] == "budget_exhausted"
    assert not result["attempts"][-1].get("dispatched")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"engines": "mojeek"},
        {"time_range": "year"},
        {"category": "science"},
        {"browser_fallback": False},
    ],
)
def test_browser_fallback_respects_explicit_provider_constraints(backend, monkeypatch, kwargs):
    browser = AsyncMock()
    monkeypatch.setattr("service.search.browser_search", browser)
    execute(backend, **kwargs)
    browser.assert_not_awaited()


def test_unconfigured_browser_is_not_dispatched(backend, monkeypatch):
    backend.browser.configured = False
    browser = AsyncMock()
    monkeypatch.setattr("service.search.browser_search", browser)
    execute(backend)
    browser.assert_not_awaited()


def test_unsupported_browser_locale_preserves_language_without_charging(backend, monkeypatch):
    browser = AsyncMock()
    admit = Mock(return_value=True)
    monkeypatch.setattr("service.search.browser_search", browser)
    result = asyncio.run(
        backend.search(SearchRequest(query="health evidence", language="de", refine=False), admit)
    )
    assert result["effective_language"] == "de"
    assert result["actual_requests"] == admit.call_count == 1
    assert result["attempts"][-1]["dispatched"] is False
    assert result["attempts"][-1]["reason"] == "unsupported_query_or_language"
    browser.assert_not_awaited()


def test_transport_preserves_language_page_time_range_and_engines(monkeypatch):
    response = httpx.Response(
        200, json={"results": []}, request=httpx.Request("GET", "https://nih.gov")
    )
    client = AsyncMock()
    client.get.return_value = response
    context = AsyncMock()
    context.__aenter__.return_value = client
    monkeypatch.setattr("service.search.httpx.AsyncClient", lambda **kwargs: context)
    request = SearchRequest(query="health evidence", language="en", page=3, time_range="month")
    asyncio.run(SearchBackend()._searx(request, request.query, "en", "mojeek"))
    assert client.get.call_args.kwargs["params"] == {
        "q": "health evidence",
        "format": "json",
        "pageno": "3",
        "language": "en",
        "time_range": "month",
        "engines": "mojeek",
    }
    context.__aexit__.assert_awaited_once()


def test_rank_before_slice_and_merge_duplicate_engine_evidence(backend):
    backend._searx.return_value = {
        "results": [
            article("noise", "holiday photos"),
            article("weak", "health evidence", score=10000),
            article("best?utm_source=foo", "health evidence report", engine="mojeek"),
            article("best", "health evidence report"),
            article("invalid", "health evidence report", url="http://127.0.0.1/private"),
        ]
    }
    result = execute(backend, max_results=1, browser_fallback=False)
    assert result["count"] == 1
    assert result["results"][0]["url"] == "https://nih.gov/best"
    assert result["results"][0]["engines"] == ["mojeek", "yandex"]
    assert result["rejections"] == {"insufficient_topic_match": 1, "invalid_public_url": 1}


def test_engine_receipts_separate_filtered_suspended_and_silent(backend):
    backend._searx.return_value = {
        "results": [article(), article("noise", "holiday photos", engine="mojeek")],
        "unresponsive_engines": [["bing", "suspended"], ["brave", "timeout"]],
    }
    result = execute(backend, engines="yandex,mojeek,bing,brave,mwmbl", browser_fallback=False)
    states = {s["engine"]: s for s in result["engine_status"]}
    assert states["yandex"]["status"] == "returned"
    assert states["mojeek"]["status"] == "filtered"
    assert states["mojeek"]["raw_count"] == 1
    assert states["mojeek"]["accepted_count"] == 0
    assert states["mojeek"]["attempted"] is True
    assert states["bing"]["status"] == "suspended"
    assert states["bing"]["attempted"] is False
    assert states["brave"]["attempted"] is None
    assert states["mwmbl"]["status"] == "silent_empty"
    assert states["mwmbl"]["reported"] is False
    assert states["mwmbl"]["attempted"] is None


def test_cache_hit_has_no_fake_dispatch_or_budget_charge(backend):
    backend._searx.return_value = {"results": [article()]}
    request = SearchRequest(query="health evidence report", max_results=1)
    first = asyncio.run(backend.search(request, lambda: True))
    second = asyncio.run(backend.search(request, lambda: pytest.fail("cache charged budget")))
    assert first["actual_requests"] == 1
    assert second["cache_hit"] is True
    assert second["attempts"] == []
    assert second["actual_requests"] == 0
    assert second["cached_attempts"] == first["attempts"]
    second["results"][0]["title"] = "mutated"
    third = asyncio.run(backend.search(request))
    assert third["results"][0]["title"] == "health evidence report"
    assert backend._searx.await_count == 1


@pytest.mark.parametrize(
    "change",
    [
        {"domains": ["nih.gov"]},
        {"required_entities": ["health"]},
        {"language": "en"},
        {"time_range": "year"},
        {"engines": "mojeek"},
        {"page": 2},
        {"max_results": 2},
    ],
)
def test_constraints_have_distinct_cache_keys(backend, change):
    backend._searx.return_value = {"results": [article()]}
    execute(backend, max_results=1, browser_fallback=False)
    kwargs = {"max_results": 1, "browser_fallback": False, **change}
    result = execute(backend, **kwargs)
    assert result["cache_hit"] is False
    assert backend._searx.await_count == 2


@pytest.mark.parametrize(
    "failure,expected",
    [
        (httpx.ConnectError("offline"), "ConnectError"),
        (ValueError("invalid JSON"), "ValueError"),
        (
            httpx.HTTPStatusError(
                "blocked",
                request=httpx.Request("GET", "https://nih.gov"),
                response=httpx.Response(429),
            ),
            "upstream_http_429",
        ),
    ],
)
def test_upstream_errors_have_actionable_failed_receipts(backend, failure, expected):
    backend._searx.side_effect = failure
    result = execute(backend, browser_fallback=False)
    assert result["status"] == "degraded"
    assert result["actual_requests"] == 1
    assert result["attempts"][0]["status"] == "failed"
    assert result["attempts"][0]["error"] == expected
    assert "elapsed_ms" in result["attempts"][0]
    assert result["search_coverage"] == "unknown"


@pytest.mark.parametrize("payload", [{"results": {}}, [], {"missing": []}])
def test_invalid_json_shape_fails_with_receipt(backend, monkeypatch, payload):
    response = httpx.Response(200, json=payload, request=httpx.Request("GET", "https://nih.gov"))
    client = AsyncMock()
    client.get.return_value = response
    context = AsyncMock()
    context.__aenter__.return_value = client
    monkeypatch.setattr("service.search.httpx.AsyncClient", lambda **kwargs: context)
    backend._searx = SearchBackend._searx.__get__(backend)
    result = execute(backend, browser_fallback=False)
    assert result["attempts"][0]["error"] == "ValueError"
    context.__aexit__.assert_awaited_once()


def test_cancellation_propagates_and_closes_http_client(backend, monkeypatch):
    async def run():
        started = asyncio.Event()

        async def blocking_get(*args, **kwargs):
            started.set()
            await asyncio.Future()

        client = AsyncMock()
        client.get.side_effect = blocking_get
        context = AsyncMock()
        context.__aenter__.return_value = client
        monkeypatch.setattr("service.search.httpx.AsyncClient", lambda **kwargs: context)
        backend._searx = SearchBackend._searx.__get__(backend)
        task = asyncio.create_task(backend.search(SearchRequest(query="health evidence")))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        context.__aexit__.assert_awaited_once()
        assert backend._cache == {}

    asyncio.run(run())


def test_deadline_cancels_upstream_and_records_incomplete_search(backend, monkeypatch):
    timeout = asyncio.timeout
    monkeypatch.setattr("service.search.asyncio.timeout", lambda seconds: timeout(0))
    result = execute(backend, browser_fallback=False)
    assert result["attempts"][-1]["status"] == "deadline"
    assert result["status"] == "degraded"
    assert result["actual_requests"] == 0
    backend._searx.assert_not_awaited()


def test_shared_backend_paces_concurrent_dispatches(backend, monkeypatch):
    now = [100.0]
    starts = []
    original_sleep = asyncio.sleep

    async def fake_sleep(delay):
        now[0] += delay
        await original_sleep(0)

    async def record(*args):
        starts.append(now[0])
        return {"results": []}

    monkeypatch.setenv("LOCAL_WEB_SEARCH_DELAY_SECONDS", "2")
    monkeypatch.setattr("service.search.time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr("service.search.asyncio.sleep", fake_sleep)
    backend._searx.side_effect = record

    async def run():
        await asyncio.gather(
            *(
                backend.search(
                    SearchRequest(
                        query="health evidence " + str(index),
                        refine=False,
                        browser_fallback=False,
                    )
                )
                for index in range(4)
            )
        )

    asyncio.run(run())
    assert starts == [100.0, 102.0, 104.0, 106.0]


def test_deadline_after_dispatch_closes_http_client_and_records_partial_attempt(
    backend, monkeypatch
):
    timeout = asyncio.timeout
    monkeypatch.setattr("service.search.asyncio.timeout", lambda seconds: timeout(0))
    backend._pace = AsyncMock()
    client = AsyncMock()

    async def pending_get(*args, **kwargs):
        await asyncio.Future()

    client.get.side_effect = pending_get
    context = AsyncMock()
    context.__aenter__.return_value = client
    monkeypatch.setattr("service.search.httpx.AsyncClient", lambda **kwargs: context)
    backend._searx = SearchBackend._searx.__get__(backend)
    result = execute(backend, browser_fallback=False)
    client.get.assert_awaited_once()
    context.__aexit__.assert_awaited_once()
    assert result["actual_requests"] == 1
    assert result["attempts"][-1]["status"] == "deadline"
    assert result["status"] == "degraded"
    assert backend._cache == {}


def test_category_only_transport_does_not_add_an_engine_pool(monkeypatch):
    response = httpx.Response(
        200, json={"results": []}, request=httpx.Request("GET", "https://nih.gov")
    )
    client = AsyncMock()
    client.get.return_value = response
    context = AsyncMock()
    context.__aenter__.return_value = client
    monkeypatch.setattr("service.search.httpx.AsyncClient", lambda **kwargs: context)
    request = SearchRequest(query="health evidence", category="news")
    asyncio.run(SearchBackend()._searx(request, request.query, "all", ""))
    params = client.get.call_args.kwargs["params"]
    assert params["categories"] == "news"
    assert "engines" not in params


def test_single_off_topic_result_is_filtered_without_a_batch_threshold(backend):
    backend._searx.return_value = {"results": [article(title="holiday photographs")]}
    result = execute(backend, engines="yandex", browser_fallback=False)
    assert result["count"] == 0
    assert result["dropped_engines"] == ["yandex"]
    assert result["rejections"] == {"insufficient_topic_match": 1}
    assert result["engine_status"][0]["raw_count"] == 1
    assert result["engine_status"][0]["status"] == "filtered"
