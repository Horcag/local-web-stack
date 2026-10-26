"""Transport failures recover automatically without fabricating successful reads."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from service import crawl4ai_api as api
from service import crawl_execution as execution

TEXT = "Verified source evidence from the requested article. " * 40
URL = "https://source.test/article"


def result(**extra):
    return SimpleNamespace(success=True, markdown=TEXT, status_code=200, metadata={}, **extra)


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError(),
        OSError("secret"),
        SimpleNamespace(success=False, error_message="Page.goto: net::ERR_NAME_NOT_RESOLVED"),
    ],
)
def test_transient_failure_restarts_crawler_with_bypassed_cache(monkeypatch, failure):
    crawl = AsyncMock(side_effect=[failure, result()])
    monkeypatch.setattr(api, "_crawl", crawl)
    with TestClient(api.app) as client:
        response = client.post("/md", json={"url": URL})
    assert response.status_code == 200
    assert response.json()["markdown"] == TEXT
    assert response.json()["recovery"]["reason"] == "recovered"
    assert crawl.await_count == 2
    assert crawl.await_args_list[1].args[0].cache_mode == "bypass"


@pytest.mark.parametrize(
    "failure",
    [RuntimeError("private token"), SimpleNamespace(success=False, error_message="private token")],
)
def test_primary_failure_runs_verified_browser_fallback(monkeypatch, failure):
    browser = {"url": URL, "title": "Article", "markdown": TEXT, "status_code": 200}
    runtime = SimpleNamespace(configured=True, read=AsyncMock(return_value=browser))
    monkeypatch.setattr(api.app.state, "browser_runtime", runtime)
    monkeypatch.setattr(api, "_crawl", AsyncMock(side_effect=[failure]))
    with TestClient(api.app) as client:
        response = client.post("/md", json={"url": URL})
    assert response.status_code == 200
    payload = response.json()
    assert payload["source"] == "browser-fallback"
    assert payload["assessment"]["status"] == "read"
    assert payload["recovery"]["attempts"] == [{"status": "failed", "transient": False}]
    assert "private token" not in str(payload)
    runtime.read.assert_awaited_once_with(URL, allow_captcha_recovery=False)


def test_all_routes_exhausted_returns_error_with_sanitized_receipts(monkeypatch):
    monkeypatch.setattr(api, "_crawl", AsyncMock(side_effect=OSError("password=secret")))
    runtime = SimpleNamespace(configured=True, read=AsyncMock(side_effect=OSError("token")))
    monkeypatch.setattr(api.app.state, "browser_runtime", runtime)
    with TestClient(api.app) as client:
        response = client.post("/md", json={"url": URL})
    assert response.status_code == 502
    payload = response.json()["detail"]
    assert len(payload["recovery"]["attempts"]) == 2
    assert payload["browser_fallback"]["reason"] == "browser_read_failed"
    assert "secret" not in str(payload) and "token" not in str(payload)


def test_post_is_never_retried_or_replayed_in_browser(monkeypatch):
    crawl = AsyncMock(side_effect=TimeoutError())
    runtime = SimpleNamespace(configured=True, read=AsyncMock())
    monkeypatch.setattr(api, "_crawl", crawl)
    monkeypatch.setattr(api.app.state, "browser_runtime", runtime)
    with TestClient(api.app) as client:
        assert client.post("/md", json={"url": URL, "method": "POST"}).status_code == 502
    crawl.assert_awaited_once()
    runtime.read.assert_not_awaited()


def test_primary_deadline_cancels_crawl_and_releases_profile_lock(monkeypatch):
    async def run():
        cancelled = []

        async def crawl(_request):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)

        monkeypatch.setattr(execution, "_env_float", lambda *args: 0.01)
        # Permit a small deterministic test deadline.
        monkeypatch.setattr(execution, "max", lambda *_: 0.01, raising=False)
        lock = asyncio.Lock()
        value, receipt = await execution.execute_crawl(
            api.CrawlRequest.model_validate({"url": URL}), crawl, lock
        )
        assert value is None and receipt["reason"] == "crawl_timeout"
        assert cancelled == [True] and not lock.locked()

    asyncio.run(run())


def test_caller_cancellation_propagates_and_releases_lock():
    async def run():
        lock = asyncio.Lock()
        task = asyncio.create_task(
            execution.execute_crawl(
                api.CrawlRequest.model_validate({"url": URL}),
                AsyncMock(side_effect=asyncio.CancelledError),
                lock,
            )
        )
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not lock.locked()

    asyncio.run(run())
