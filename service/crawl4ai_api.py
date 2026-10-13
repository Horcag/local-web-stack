from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse, urlsplit

from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig
from fastapi import FastAPI, HTTPException


from service.crawl_defaults import (
    _env_bool as _env_bool,
    _env_int as _env_int,
    _env_float as _env_float,
)
from service.crawl_request import CrawlRequest as CrawlRequest
from service.captcha_solver import CaptchaSolver
from service.captcha_browser import _at_target, recover_captcha
from service.research.content import assess_content

app = FastAPI(title="Local Crawl4AI API", version="0.2.0")
_CRAWL_LOCK = asyncio.Lock()
_DOMAIN_LOCKS: dict[str, asyncio.Lock] = {}
_DOMAIN_LAST_REQUEST: dict[str, float] = {}
_BROWSER_FALLBACK_TIMEOUT_SECONDS = 30


def _cache_mode(value: Literal["bypass", "enabled"]) -> CacheMode:
    return CacheMode.BYPASS if value == "bypass" else CacheMode.ENABLED


def _proxy_config(request: CrawlRequest) -> dict[str, str] | None:
    if not request.proxy_server:
        default_proxy = os.environ.get("CRAWL4AI_DEFAULT_PROXY", "").strip()
        if default_proxy:
            return {"server": default_proxy}
        return None
    proxy = {"server": request.proxy_server}
    if request.proxy_username:
        proxy["username"] = request.proxy_username
    if request.proxy_password:
        proxy["password"] = request.proxy_password
    return proxy


def build_browser_config(request: CrawlRequest) -> BrowserConfig:
    if request.use_persistent_context and request.user_data_dir:
        Path(request.user_data_dir).mkdir(parents=True, exist_ok=True)

    kwargs: dict[str, Any] = {
        "browser_type": request.browser_type,
        "headless": request.headless,
        "use_persistent_context": request.use_persistent_context,
        "user_data_dir": request.user_data_dir if request.use_persistent_context else None,
        "viewport_width": request.viewport_width,
        "viewport_height": request.viewport_height,
        "device_scale_factor": request.device_scale_factor,
        "accept_downloads": request.accept_downloads,
        "ignore_https_errors": request.ignore_https_errors,
        "java_script_enabled": request.java_script_enabled,
        "cookies": request.cookies,
        "headers": {str(key): str(value) for key, value in request.headers.items()},
        "text_mode": request.text_mode,
        "light_mode": request.light_mode,
        "memory_saving_mode": request.memory_saving_mode,
        "max_pages_before_recycle": request.max_pages_before_recycle,
        "verbose": request.verbose,
        "enable_stealth": request.enable_stealth,
        "user_agent_mode": request.user_agent_mode if request.user_agent_mode else None,
        # No extra CLI flags. Patchright already tweaks Playwright's default
        # args and adds --disable-blink-features=AutomationControlled itself,
        # while --disable-web-security is a command-flag leak detectors read.
        "extra_args": [],
    }

    optional_values = {
        "chrome_channel": request.chrome_channel,
        "channel": request.channel,
        "downloads_path": request.downloads_path,
        "storage_state": request.storage_state,
        "user_agent": request.user_agent,
        "proxy_config": _proxy_config(request),
    }
    kwargs.update({key: value for key, value in optional_values.items() if value is not None})
    return BrowserConfig(**kwargs)


def build_run_config(request: CrawlRequest) -> CrawlerRunConfig:
    kwargs: dict[str, Any] = {
        "word_count_threshold": request.word_count_threshold,
        "only_text": request.only_text,
        "css_selector": request.css_selector,
        "target_elements": request.target_elements,
        "excluded_tags": request.excluded_tags,
        "excluded_selector": request.excluded_selector,
        "remove_forms": request.remove_forms,
        "prettiify": request.prettiify,
        "parser_type": request.parser_type,
        "locale": request.locale,
        "timezone_id": request.timezone_id,
        "cache_mode": _cache_mode(request.cache_mode),
        "session_id": request.session_id,
        "wait_until": request.wait_until,
        "page_timeout": request.page_timeout,
        "wait_for": request.wait_for,
        "wait_for_timeout": request.wait_for_timeout,
        "wait_for_images": request.wait_for_images,
        "delay_before_return_html": request.delay_before_return_html,
        "mean_delay": request.mean_delay,
        "max_range": request.max_range,
        "semaphore_count": request.semaphore_count,
        "scan_full_page": request.scan_full_page,
        "scroll_delay": request.scroll_delay,
        "max_scroll_steps": request.max_scroll_steps,
        "process_iframes": request.process_iframes,
        "flatten_shadow_dom": request.flatten_shadow_dom,
        "remove_overlay_elements": request.remove_overlay_elements,
        "remove_consent_popups": request.remove_consent_popups,
        "screenshot": request.screenshot,
        "pdf": request.pdf,
        "capture_mhtml": request.capture_mhtml,
        "exclude_external_images": request.exclude_external_images,
        "exclude_all_images": request.exclude_all_images,
        "exclude_external_links": request.exclude_external_links,
        "exclude_social_media_links": request.exclude_social_media_links,
        "exclude_domains": request.exclude_domains,
        "exclude_internal_links": request.exclude_internal_links,
        "check_robots_txt": request.check_robots_txt,
        "method": request.method,
        "max_retries": request.max_retries,
        "magic": request.magic,
        "simulate_user": request.simulate_user,
        "override_navigator": request.override_navigator,
    }

    proxy = _proxy_config(request)
    if proxy:
        kwargs["proxy_config"] = proxy

    return CrawlerRunConfig(**kwargs)


def _domain_for(url: str) -> str:
    return urlparse(url).netloc.lower()


async def _wait_for_domain_budget(domain: str, delay_seconds: float) -> None:
    if delay_seconds <= 0:
        return
    now = asyncio.get_running_loop().time()
    last_request = _DOMAIN_LAST_REQUEST.get(domain)
    if last_request is not None:
        sleep_for = delay_seconds - (now - last_request)
        if sleep_for > 0:
            await asyncio.sleep(sleep_for)
    _DOMAIN_LAST_REQUEST[domain] = asyncio.get_running_loop().time()


def _markdown_text(result: Any) -> str:
    markdown = getattr(result, "markdown", None)
    if markdown is None:
        return ""
    if hasattr(markdown, "raw_markdown"):
        return markdown.raw_markdown or ""
    return str(markdown)


async def _crawl(request: CrawlRequest) -> Any:
    url = str(request.url)
    domain = _domain_for(url)
    domain_lock = _DOMAIN_LOCKS.setdefault(domain, asyncio.Lock())
    async with domain_lock:
        await _wait_for_domain_budget(domain, request.domain_delay_seconds)

    browser_config = build_browser_config(request)
    if request.browser_type == "undetected":
        from crawl4ai import UndetectedAdapter
        from crawl4ai.async_crawler_strategy import AsyncPlaywrightCrawlerStrategy

        adapter = UndetectedAdapter()
        strategy = AsyncPlaywrightCrawlerStrategy(
            browser_config=browser_config, browser_adapter=adapter
        )
        crawler = AsyncWebCrawler(crawler_strategy=strategy, config=browser_config)
    else:
        crawler = AsyncWebCrawler(config=browser_config)

    solver = CaptchaSolver.from_env()
    captcha_receipt: dict[str, Any] = {}
    if solver.enabled:

        async def captcha_hook(page, **_kwargs):
            captcha_receipt.update(await recover_captcha(page, solver, url))
            return page

        crawler.crawler_strategy.set_hook("before_retrieve_html", captcha_hook)

    # start()/close() in try/finally instead of "async with": when the browser
    # fails to launch, __aenter__ raises and __aexit__ never runs, leaking the
    # already-spawned Playwright node driver (~140 MB) for the process lifetime.
    try:
        await crawler.start()
        effective_request = (
            request.model_copy(update={"cache_mode": "bypass"}) if solver.enabled else request
        )
        result = await crawler.arun(
            url=url,
            config=build_run_config(effective_request),
        )
        # The cache write precedes these verification receipts in Crawl4AI.
        if solver.enabled:
            result.metadata = dict(getattr(result, "metadata", None) or {})
            result.metadata["captcha_cache_mode"] = "bypass"
        if captcha_receipt:
            result.metadata = dict(getattr(result, "metadata", None) or {})
            result.metadata["captcha_recovery"] = captcha_receipt
        return result
    finally:
        with suppress(Exception):
            await crawler.close()


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "healthy", "service": "local-crawl4ai"}


async def _browser_fallback(url: str, payload: dict[str, Any]) -> None:
    runtime = getattr(app.state, "browser_runtime", None)
    if runtime is None or not runtime.configured:
        return
    receipt: dict[str, Any] = {
        "status": "rejected",
        "captcha_recovery_allowed": False,
        "initial_assessment": payload["assessment"],
    }
    payload["browser_fallback"] = receipt
    try:
        async with asyncio.timeout(_BROWSER_FALLBACK_TIMEOUT_SECONDS):
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
            initial_captcha=payload["captcha"],
            captcha=captcha,
            metadata={
                **payload["metadata"],
                "title": title,
                "initial_captcha_recovery": payload["captcha"],
                "captcha_recovery": captcha,
            },
        )
    except TimeoutError:
        receipt["reason"] = "browser_read_timeout"
    except Exception:
        receipt["reason"] = "browser_read_failed"


@app.post("/md")
async def markdown(request: CrawlRequest) -> dict[str, Any]:
    try:
        if request.serialize_requests:
            async with _CRAWL_LOCK:
                result = await _crawl(request)
        else:
            result = await _crawl(request)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=repr(exc)) from exc

    if not getattr(result, "success", False):
        error = getattr(result, "error_message", None) or "Crawl failed"
        raise HTTPException(status_code=502, detail=error)

    markdown_str = _markdown_text(result)

    metadata = getattr(result, "metadata", None) or {}
    captcha = metadata.get("captcha_recovery")
    initial_status = getattr(result, "status_code", None)
    status_code = initial_status
    if captcha and captcha.get("status") == "solved":
        final_status = captcha.get("final_status_code")
        if isinstance(final_status, int) and 100 <= final_status <= 599:
            status_code = final_status
    assessment = assess_content(
        markdown_str,
        metadata.get("title") or "",
        status_code or 200,
        captcha,
    )
    payload = {
        "url": str(request.url),
        "cache_mode": request.cache_mode,
        "status_code": status_code,
        "initial_status_code": initial_status,
        "effective_cache_mode": metadata.get("captcha_cache_mode", request.cache_mode),
        "links": getattr(result, "links", {}) or {},
        "assessment": assessment,
        "next_action": "browser" if assessment["status"] != "read" else None,
        "markdown": markdown_str,
        "title": metadata.get("title"),
        "metadata": metadata,
        "captcha": captcha,
    }
    if assessment["status"] in {"blocked", "partial"} or (
        assessment["status"] == "failed" and not markdown_str.strip()
    ):
        await _browser_fallback(str(request.url), payload)
    return payload


def _attach_research() -> None:
    from service.app_runtime import attach_research

    attach_research(app)


_attach_research()
