"""Periodic browser canary using the same crawler and profile lock as real reads."""

import asyncio
from contextlib import suppress
import time
from typing import Any, Awaitable, Callable

from service.crawl_request import CrawlRequest

CANARY_TIMEOUT_SECONDS = 25


class CrawlMonitor:
    def __init__(self, crawl: Callable[[CrawlRequest], Awaitable[Any]], lock: asyncio.Lock):
        self.crawl = crawl
        self.lock = lock
        self.task: asyncio.Task | None = None
        self.last_success: float | None = None
        self.last_fresh_read: float | None = None
        self.last_check: float | None = None
        self.failures = 0

    def observe_success(self) -> None:
        self.last_success = time.monotonic()
        self.failures = 0

    def observe_fresh_read(self) -> None:
        # A real navigation can prove activity, but cannot erase failed canary checks.
        self.last_fresh_read = time.monotonic()

    def snapshot(self) -> dict[str, Any]:
        successes = [
            stamp for stamp in (self.last_success, self.last_fresh_read) if stamp is not None
        ]
        age = None if not successes else time.monotonic() - max(successes)
        status = "starting" if age is None else "healthy"
        if self.failures >= 2 or age is not None and age > 180:
            status = "unhealthy"
        return {
            "status": status,
            "consecutive_failures": self.failures,
            "last_success_age_seconds": None if age is None else round(age, 1),
        }

    async def check(self) -> None:
        acquired = False
        try:
            # Busy user traffic is evidence of activity; never jump the request queue.
            try:
                await asyncio.wait_for(self.lock.acquire(), timeout=1)
                acquired = True
            except TimeoutError:
                return
            self.last_check = time.monotonic()
            request = CrawlRequest.model_validate(
                {
                    "url": "https://example.com/",
                    "cache_mode": "bypass",
                    "max_retries": 0,
                    "page_timeout": 15000,
                    "domain_delay_seconds": 0,
                }
            )
            async with asyncio.timeout(CANARY_TIMEOUT_SECONDS):
                result = await self.crawl(request)
            body = getattr(result, "markdown", "")
            body = getattr(body, "raw_markdown", body) or ""
            if (
                not getattr(result, "success", False)
                or getattr(result, "status_code", None) != 200
                or "example domain" not in str(body).lower()
            ):
                raise ValueError("canary_content_unverified")
            self.observe_success()
        except Exception:
            self.failures += 1
        finally:
            if acquired:
                self.lock.release()

    async def run(self) -> None:
        while True:
            await self.check()
            await asyncio.sleep(60)

    def start(self) -> None:
        self.task = asyncio.create_task(self.run(), name="local-web-crawl-canary")

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            self.task = None
