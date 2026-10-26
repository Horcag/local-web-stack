"""Verified browser fallback shared by transport and content failures."""

import asyncio
from typing import Any
from urllib.parse import urlsplit

from service.captcha_browser import _at_target
from service.research.content import assess_content


async def browser_fallback(
    runtime: Any, url: str, payload: dict[str, Any], timeout: float = 30
) -> None:
    if runtime is None or not runtime.configured:
        return
    receipt: dict[str, Any] = {
        "status": "rejected",
        "captcha_recovery_allowed": False,
        "initial_assessment": payload["assessment"],
    }
    payload["browser_fallback"] = receipt
    try:
        async with asyncio.timeout(timeout):
            browser = await runtime.read(url, allow_captcha_recovery=False)
        if not isinstance(browser, dict):
            receipt["reason"] = "no_browser_result"
            return
        actual_url = browser.get("url")
        if not isinstance(actual_url, str):
            receipt["reason"] = "target_unverified"
            return
        actual = urlsplit(actual_url)
        if (
            not _at_target(actual_url, url)
            or actual.username is not None
            or actual.password is not None
        ):
            receipt["reason"] = "target_unverified"
            return
        status_code = browser.get("status_code")
        if type(status_code) is not int or not 200 <= status_code < 300:
            receipt["reason"] = "http_status_unverified"
            return
        body, title = browser.get("markdown"), browser.get("title")
        captcha = browser.get("captcha")
        if (
            not isinstance(body, str)
            or not isinstance(title, str)
            or captcha is not None
            and not isinstance(captcha, dict)
        ):
            receipt["reason"] = "invalid_browser_content"
            return
        assessment = assess_content(body, title, status_code, captcha)
        receipt.update(status_code=status_code, assessment=assessment, captcha=captcha)
        heading = next((line.strip() for line in body.splitlines() if line.strip()), "")
        if (
            assessment["status"] != "read"
            or browser.get("truncated")
            or any(
                value.strip(" #").lower() in {"privacy error", "your connection is not private"}
                for value in (title, heading)
            )
        ):
            receipt["reason"] = "content_unverified"
            return
        method = browser.get("method") or "browser"
        receipt.update(status="accepted", url=actual_url, method=method)
        payload.update(
            markdown=body,
            title=title,
            links=browser.get("links") or [],
            status_code=status_code,
            assessment=assessment,
            next_action=None,
            source="browser-fallback",
            method=method,
            extraction=browser.get("extraction"),
            network=browser.get("network"),
            headings=browser.get("headings", []),
            initial_captcha=payload["captcha"],
            captcha=captcha,
            metadata={
                **payload["metadata"],
                "title": title,
                "initial_captcha_recovery": payload["captcha"],
                "captcha_recovery": captcha,
                "browser_extraction": browser.get("extraction"),
                "browser_network": browser.get("network"),
            },
        )
    except TimeoutError:
        receipt["reason"] = "browser_read_timeout"
    except Exception:
        receipt["reason"] = "browser_read_failed"
