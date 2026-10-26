"""Bounded crawler execution: queue admission, fresh transport retry and receipts."""

import asyncio
from typing import Any, Awaitable, Callable

from fastapi import HTTPException

from service.crawl_defaults import _env_float
from service.crawl_request import CrawlRequest

_TRANSIENT = (
    "err_timed_out",
    "timeout",
    "timed out",
    "err_name_not_resolved",
    "err_proxy_connection_failed",
    "err_socks_connection_failed",
    "err_connection",
    "target closed",
    "browser has been closed",
    "browser closed",
    "connection closed",
    "connection reset",
)


def transient(error: BaseException | str) -> bool:
    return isinstance(error, (TimeoutError, OSError)) or any(
        marker in str(error).lower() for marker in _TRANSIENT
    )


async def execute_crawl(
    request: CrawlRequest,
    crawl: Callable[[CrawlRequest], Awaitable[Any]],
    lock: asyncio.Lock,
) -> tuple[Any, dict[str, Any]]:
    receipt: dict[str, Any] = {"attempts": [], "reason": "crawl_failed"}
    acquired = False
    try:
        if request.serialize_requests:
            try:
                await asyncio.wait_for(lock.acquire(), timeout=10)
                acquired = True
            except TimeoutError as exc:
                raise HTTPException(
                    503, detail={"reason": "crawler_busy"}, headers={"Retry-After": "5"}
                ) from exc
        async with asyncio.timeout(max(1, _env_float("LOCAL_WEB_CRAWL_TIMEOUT_SECONDS", 75))):
            for attempt in range(2 if request.method == "GET" else 1):
                # A retry starts a fresh browser and never reuses a failed cache result.
                effective = (
                    request.model_copy(update={"cache_mode": "bypass"}) if attempt else request
                )
                try:
                    result = await crawl(effective)
                    if getattr(result, "success", False):
                        receipt["attempts"].append({"status": "completed"})
                        receipt["reason"] = "recovered" if attempt else "completed"
                        return result, receipt
                    error = getattr(result, "error_message", "") or "Crawl failed"
                    retryable = transient(error)
                except Exception as exc:
                    retryable = transient(exc)
                receipt["attempts"].append({"status": "failed", "transient": retryable})
                if not retryable:
                    break
                await asyncio.sleep(0.25)
    except TimeoutError:
        receipt["reason"] = "crawl_timeout"
        receipt["attempts"].append({"status": "timeout"})
    finally:
        if acquired:
            lock.release()
    return None, receipt
