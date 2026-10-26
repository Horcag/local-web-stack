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


def test_robots_denial_does_not_bypass_policy_through_browser(monkeypatch):
    crawl = AsyncMock(
        return_value=SimpleNamespace(
            success=False, status_code=403, error_message="Access denied by robots.txt"
        )
    )
    runtime = SimpleNamespace(configured=True, read=AsyncMock())
    monkeypatch.setattr(api, "_crawl", crawl)
    monkeypatch.setattr(api.app.state, "browser_runtime", runtime)
    with TestClient(api.app) as client:
        response = client.post("/md", json={"url": URL, "check_robots_txt": True})
    assert response.status_code == 403
    assert response.json()["detail"]["reason"] == "robots_denied"
    crawl.assert_awaited_once()
    runtime.read.assert_not_awaited()


def test_cached_reads_do_not_hide_failed_browser_canary(monkeypatch):
    from service.crawl_monitor import CrawlMonitor

    monitor = CrawlMonitor(AsyncMock(), asyncio.Lock())
    monitor.failures = 2
    monkeypatch.setattr(api.app.state, "crawl_monitor", monitor, raising=False)
    # Avoid starting background work; this regression exercises a cache-hit /md response.
    monkeypatch.setattr(monitor, "start", lambda: None)
    monkeypatch.setattr(api, "_crawl", AsyncMock(return_value=result(cache_status="hit")))
    with TestClient(api.app) as client:
        assert client.post("/md", json={"url": URL}).status_code == 200
    assert monitor.failures == 2 and monitor.last_success is None
    assert monitor.snapshot()["status"] == "unhealthy"


def test_fresh_reads_prove_activity_without_erasing_failed_canary(monkeypatch):
    from service.crawl_monitor import CrawlMonitor

    monitor = CrawlMonitor(AsyncMock(), asyncio.Lock())
    monitor.failures = 1
    monitor.last_success = 1
    monkeypatch.setattr(api.app.state, "crawl_monitor", monitor, raising=False)
    monkeypatch.setattr(monitor, "start", lambda: None)
    monkeypatch.setattr(api, "_crawl", AsyncMock(return_value=result(cache_status="miss")))
    with TestClient(api.app) as client:
        assert client.post("/md", json={"url": URL}).status_code == 200
    assert monitor.failures == 1 and monitor.last_success == 1
    assert monitor.snapshot()["status"] == "healthy"
    monitor.failures = 2
    assert monitor.snapshot()["status"] == "unhealthy"


@pytest.mark.parametrize("failure", [ValueError("secret config"), TypeError("secret type")])
def test_configuration_failure_is_terminal_and_sanitized(monkeypatch, failure):
    runtime = SimpleNamespace(configured=True, read=AsyncMock())
    crawl = AsyncMock(side_effect=failure)
    monkeypatch.setattr(api, "_crawl", crawl)
    monkeypatch.setattr(api.app.state, "browser_runtime", runtime)
    with TestClient(api.app) as client:
        response = client.post("/md", json={"url": URL})
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["reason"] == "crawler_configuration_error"
    assert detail["recovery"]["exception_type"] == type(failure).__name__
    assert "secret" not in str(detail)
    runtime.read.assert_not_awaited()
    crawl.assert_awaited_once()
