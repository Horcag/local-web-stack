import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from service import crawl_monitor


def success():
    return SimpleNamespace(success=True, status_code=200, markdown="# Example Domain\nCanary")


def test_canary_checks_real_crawl_without_cache_then_recovers_after_failures():
    async def run():
        crawl = AsyncMock(side_effect=[success(), OSError(), OSError(), success()])
        monitor = crawl_monitor.CrawlMonitor(crawl, asyncio.Lock())
        assert monitor.snapshot()["status"] == "starting"
        await monitor.check()
        assert monitor.snapshot()["status"] == "healthy"
        request = crawl.await_args.args[0]
        assert request.cache_mode == "bypass" and request.max_retries == 0
        await monitor.check()
        assert monitor.snapshot()["status"] == "healthy"
        await monitor.check()
        assert monitor.snapshot()["status"] == "unhealthy"
        await monitor.check()
        assert monitor.snapshot()["status"] == "healthy"
        assert not monitor.lock.locked()

    asyncio.run(run())


def test_200_error_page_is_not_a_successful_browser_canary():
    async def run():
        crawl = AsyncMock(
            return_value=SimpleNamespace(success=True, status_code=200, markdown="Access denied")
        )
        monitor = crawl_monitor.CrawlMonitor(crawl, asyncio.Lock())
        await monitor.check()
        await monitor.check()
        assert monitor.snapshot()["status"] == "unhealthy"
        assert monitor.last_success is None

    asyncio.run(run())


def test_canary_timeout_and_shutdown_release_owned_profile_lock(monkeypatch):
    async def run():
        cleaned = []

        async def crawl(_request):
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.append(True)

        monkeypatch.setattr(crawl_monitor, "CANARY_TIMEOUT_SECONDS", 0.01)
        monitor = crawl_monitor.CrawlMonitor(crawl, asyncio.Lock())
        await monitor.check()
        assert monitor.failures == 1 and not monitor.lock.locked()
        assert cleaned == [True]
        monitor.start()
        await asyncio.sleep(0.03)
        await monitor.close()
        assert monitor.task is None and not monitor.lock.locked()

    asyncio.run(run())


def test_stale_canary_cannot_claim_health(monkeypatch):
    monitor = crawl_monitor.CrawlMonitor(AsyncMock(), asyncio.Lock())
    monkeypatch.setattr(crawl_monitor.time, "monotonic", lambda: 200)
    monitor.last_success = 1
    assert monitor.snapshot()["status"] == "unhealthy"
