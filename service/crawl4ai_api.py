from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


app = FastAPI(title="Local Crawl4AI API", version="0.2.0")
_CRAWL_LOCK = asyncio.Lock()
_DOMAIN_LOCKS: dict[str, asyncio.Lock] = {}
_DOMAIN_LAST_REQUEST: dict[str, float] = {}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _default_cache_mode() -> Literal["bypass", "enabled"]:
    value = os.environ.get("CRAWL4AI_CACHE_MODE", "enabled").strip().lower()
    return "bypass" if value == "bypass" else "enabled"


def _default_headers() -> dict[str, str]:
    return {
        "Accept-Language": os.environ.get(
            "CRAWL4AI_ACCEPT_LANGUAGE",
            "en-US,en;q=0.9,ru;q=0.8",
        )
    }


def _default_user_data_dir() -> str:
    return os.environ.get("CRAWL4AI_USER_DATA_DIR", "/app/work/browser-profile").strip()


class CrawlRequest(BaseModel):
    """JSON-safe Crawl4AI request schema for the local microservice.

    The model intentionally rejects fingerprint/WAF evasion switches. For
    owned integration tests, allowlist this crawler at the app or WAF layer.
    """

    model_config = ConfigDict(extra="forbid")

    url: HttpUrl

    # BrowserConfig: browser process, profile, viewport, headers, and proxy.
    browser_type: Literal["chromium", "firefox", "webkit", "undetected"] = "chromium"
    headless: bool = Field(default_factory=lambda: _env_bool("CRAWL4AI_HEADLESS", True))
    use_persistent_context: bool = Field(default_factory=lambda: _env_bool("CRAWL4AI_PERSISTENT_CONTEXT", True))
    user_data_dir: str | None = Field(default_factory=_default_user_data_dir)
    chrome_channel: str | None = None
    channel: str | None = None
    viewport_width: int = Field(default_factory=lambda: _env_int("CRAWL4AI_VIEWPORT_WIDTH", 1365), ge=320)
    viewport_height: int = Field(default_factory=lambda: _env_int("CRAWL4AI_VIEWPORT_HEIGHT", 768), ge=240)
    device_scale_factor: float = Field(default=1.0, gt=0)
    accept_downloads: bool = False
    downloads_path: str | None = None
    storage_state: str | dict[str, Any] | None = None
    ignore_https_errors: bool = True
    java_script_enabled: bool = True
    cookies: list[dict[str, Any]] = Field(default_factory=list)
    headers: dict[str, str] = Field(default_factory=_default_headers)
    user_agent: str | None = Field(default_factory=lambda: os.environ.get("CRAWL4AI_USER_AGENT", "").strip() or None)
    text_mode: bool = Field(default_factory=lambda: _env_bool("CRAWL4AI_TEXT_MODE", False))
    light_mode: bool = Field(default_factory=lambda: _env_bool("CRAWL4AI_LIGHT_MODE", False))
    memory_saving_mode: bool = False
    max_pages_before_recycle: int = Field(default=0, ge=0)
    verbose: bool = Field(default_factory=lambda: _env_bool("CRAWL4AI_VERBOSE", False))

    proxy_server: str | None = None
    proxy_username: str | None = None
    proxy_password: str | None = None

    # CrawlerRunConfig: content extraction, cache, waits, pacing, and timeout.
    cache_mode: Literal["bypass", "enabled"] = Field(default_factory=_default_cache_mode)
    word_count_threshold: int = Field(default=1, ge=0)
    only_text: bool = False
    css_selector: str | None = None
    target_elements: list[str] | None = None
    excluded_tags: list[str] | None = None
    excluded_selector: str | None = None
    remove_forms: bool = False
    prettiify: bool = False
    parser_type: str = "lxml"
    locale: str | None = None
    timezone_id: str | None = None
    session_id: str | None = None
    wait_until: str = Field(default_factory=lambda: os.environ.get("CRAWL4AI_WAIT_UNTIL", "domcontentloaded").strip())
    page_timeout: int = Field(default_factory=lambda: _env_int("CRAWL4AI_PAGE_TIMEOUT_MS", 45000), ge=1)
    wait_for: str | None = None
    wait_for_timeout: int | None = None
    wait_for_images: bool = False
    delay_before_return_html: float = Field(
        default_factory=lambda: _env_float("CRAWL4AI_DELAY_BEFORE_RETURN_HTML", 0.4),
        ge=0,
    )
    mean_delay: float = Field(default_factory=lambda: _env_float("CRAWL4AI_MEAN_DELAY", 0.2), ge=0)
    max_range: float = Field(default_factory=lambda: _env_float("CRAWL4AI_MAX_DELAY_RANGE", 0.5), ge=0)
    semaphore_count: int = Field(default=1, ge=1)
    scan_full_page: bool = False
    scroll_delay: float = Field(default=0.2, ge=0)
    max_scroll_steps: int | None = None
    process_iframes: bool = False
    flatten_shadow_dom: bool = False
    remove_overlay_elements: bool = Field(default_factory=lambda: _env_bool("CRAWL4AI_REMOVE_OVERLAYS", True))
    remove_consent_popups: bool = Field(default_factory=lambda: _env_bool("CRAWL4AI_REMOVE_CONSENT_POPUPS", True))
    screenshot: bool = False
    pdf: bool = False
    capture_mhtml: bool = False
    exclude_external_images: bool = False
    exclude_all_images: bool = False
    exclude_external_links: bool = False
    exclude_social_media_links: bool = False
    exclude_domains: list[str] | None = None
    exclude_internal_links: bool = False
    check_robots_txt: bool = False
    method: Literal["GET", "POST"] = "GET"
    max_retries: int = Field(default_factory=lambda: _env_int("CRAWL4AI_MAX_RETRIES", 1), ge=0)

    serialize_requests: bool = Field(default_factory=lambda: _env_bool("CRAWL4AI_SERIALIZE_REQUESTS", True))
    domain_delay_seconds: float = Field(default_factory=lambda: _env_float("CRAWL4AI_DOMAIN_DELAY_SECONDS", 2.0), ge=0)

    # Stealth and evasion options mapped as regular fields with env fallback
    enable_stealth: bool = Field(default_factory=lambda: _env_bool("CRAWL4AI_ENABLE_STEALTH", False))
    magic: bool = Field(default_factory=lambda: _env_bool("CRAWL4AI_MAGIC", False))
    simulate_user: bool = Field(default_factory=lambda: _env_bool("CRAWL4AI_SIMULATE_USER", False))
    override_navigator: bool = Field(default_factory=lambda: _env_bool("CRAWL4AI_OVERRIDE_NAVIGATOR", False))
    user_agent_mode: str = Field(default_factory=lambda: os.environ.get("CRAWL4AI_USER_AGENT_MODE", "").strip())


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
        "extra_args": [
            "--disable-blink-features=AutomationControlled",
            "--disable-web-security"
        ] if request.browser_type == "undetected" else []
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
            browser_config=browser_config,
            browser_adapter=adapter
        )
        async with AsyncWebCrawler(crawler_strategy=strategy, config=browser_config) as crawler:
            return await crawler.arun(
                url=url,
                config=build_run_config(request),
            )
    else:
        async with AsyncWebCrawler(config=browser_config) as crawler:
            return await crawler.arun(
                url=url,
                config=build_run_config(request),
            )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "healthy", "service": "local-crawl4ai"}


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

    markdown_obj = getattr(result, "markdown", None)
    if markdown_obj is None:
        markdown_str = ""
    elif hasattr(markdown_obj, "raw_markdown"):
        markdown_str = markdown_obj.raw_markdown or ""
    else:
        markdown_str = str(markdown_obj)

    metadata = getattr(result, "metadata", None) or {}
    return {
        "url": str(request.url),
        "cache_mode": request.cache_mode,
        "markdown": markdown_str,
        "title": metadata.get("title"),
        "metadata": metadata,
    }
