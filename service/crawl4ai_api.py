from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig
from fastapi import FastAPI, HTTPException


from service.crawl_defaults import (
    _env_bool as _env_bool,
    _env_int as _env_int,
    _env_float as _env_float,
)
from service.crawl_request import CrawlRequest as CrawlRequest
from service.captcha_solver import CaptchaSolver
from service.captcha_browser import recover_captcha
from service.crawl_fallback import browser_fallback
from service.crawl_execution import execute_crawl
from service.health import readiness
from fastapi.responses import JSONResponse, HTMLResponse
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
            await asyncio.wait_for(crawler.close(), timeout=10)


@app.get("/live")
async def live() -> dict[str, str]:
    return {"status": "alive", "service": "local-crawl4ai"}


@app.get("/health/canary", response_class=HTMLResponse)
async def browser_canary() -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><html><title>Local Web Browser Canary</title><main>"
        "<h1>Local Web Browser Canary</h1><p>This owned fixture verifies browser navigation "
        "and content extraction without relying on a third-party page.</p></main></html>",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/health")
async def health() -> JSONResponse:
    status = await readiness()
    monitor = getattr(app.state, "crawl_monitor", None)
    if monitor is not None:
        status["checks"]["crawler"] = monitor.snapshot()
        if status["checks"]["crawler"]["status"] != "healthy":
            status["status"] = "degraded"
    return JSONResponse(status, status_code=200 if status["status"] == "healthy" else 503)


async def _browser_fallback(url: str, payload: dict[str, Any]) -> None:
    await browser_fallback(
        getattr(app.state, "browser_runtime", None),
        url,
        payload,
        _BROWSER_FALLBACK_TIMEOUT_SECONDS,
    )


@app.post("/md")
async def markdown(request: CrawlRequest) -> dict[str, Any]:
    result, recovery = await execute_crawl(request, _crawl, _CRAWL_LOCK)
    if result is None:
        payload = {
            "url": str(request.url),
            "cache_mode": request.cache_mode,
            "status_code": None,
            "initial_status_code": None,
            "markdown": "",
            "title": None,
            "links": {},
            "metadata": {},
            "captcha": None,
            "assessment": {"status": "failed", "reasons": [recovery["reason"]]},
            "next_action": "browser",
            "recovery": recovery,
        }
        if request.method == "GET" and recovery["reason"] not in {
            "robots_denied",
            "crawler_configuration_error",
        }:
            await _browser_fallback(str(request.url), payload)
        if payload.get("source") == "browser-fallback":
            return payload
        raise HTTPException(
            {"crawl_timeout": 504, "robots_denied": 403, "crawler_configuration_error": 503}.get(
                recovery["reason"], 502
            ),
            detail={
                "reason": recovery["reason"],
                "recovery": recovery,
                "browser_fallback": payload.get("browser_fallback"),
            },
        )

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
    monitor = getattr(app.state, "crawl_monitor", None)
    if (
        monitor is not None
        and getattr(result, "cache_status", None) == "miss"
        and assessment["status"] == "read"
        and status_code is not None
        and 200 <= status_code < 300
    ):
        monitor.observe_fresh_read()
    payload = {
        "url": str(request.url),
        "cache_mode": request.cache_mode,
        "recovery": recovery,
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
    if request.method == "GET" and (
        assessment["status"] in {"blocked", "partial"}
        or assessment["status"] == "failed"
        and not markdown_str.strip()
    ):
        await _browser_fallback(str(request.url), payload)
    return payload


def _attach_research() -> None:
    from service.app_runtime import attach_research
    from service.crawl_monitor import CrawlMonitor

    if _env_bool("LOCAL_WEB_CRAWL_CANARY_ENABLED", False):
        app.state.crawl_monitor = CrawlMonitor(_crawl, _CRAWL_LOCK)
    attach_research(app)


_attach_research()
