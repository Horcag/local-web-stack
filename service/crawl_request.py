"""Validated request contract for the local Crawl4AI adapter."""

import os
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator
from service.crawl_defaults import (
    _env_bool,
    _env_int,
    _env_float,
    _default_cache_mode,
    _default_headers,
    _default_user_data_dir,
    _default_browser_type,
    _default_chrome_channel,
)


class CrawlRequest(BaseModel):
    """JSON-safe Crawl4AI request schema for the local microservice.

    Private initial targets require explicit local configuration; network egress
    controls remain necessary for redirect, DNS and subresource isolation.
    """

    model_config = ConfigDict(extra="forbid")

    url: HttpUrl

    # BrowserConfig: browser process, profile, viewport, headers, and proxy.
    browser_type: Literal["chromium", "firefox", "webkit", "undetected"] = Field(
        default_factory=_default_browser_type
    )
    headless: bool = Field(default_factory=lambda: _env_bool("CRAWL4AI_HEADLESS", True))
    use_persistent_context: bool = Field(
        default_factory=lambda: _env_bool("CRAWL4AI_PERSISTENT_CONTEXT", True)
    )
    user_data_dir: str | None = Field(default_factory=_default_user_data_dir)
    chrome_channel: str | None = Field(default_factory=_default_chrome_channel)
    channel: str | None = Field(default_factory=_default_chrome_channel)
    viewport_width: int = Field(
        default_factory=lambda: _env_int("CRAWL4AI_VIEWPORT_WIDTH", 1365), ge=320
    )
    viewport_height: int = Field(
        default_factory=lambda: _env_int("CRAWL4AI_VIEWPORT_HEIGHT", 768), ge=240
    )
    device_scale_factor: float = Field(default=1.0, gt=0)
    accept_downloads: bool = False
    downloads_path: str | None = None
    storage_state: str | dict[str, Any] | None = None
    # Off by default: --ignore-certificate-errors is one of the few launch
    # flags with a JS-observable effect. A page can serve a subresource with a
    # deliberately invalid certificate and watch whether it loads; a real
    # browser refuses. ByeDPI works at the TCP layer and does not terminate
    # TLS, so certificates stay valid end to end.
    ignore_https_errors: bool = Field(
        default_factory=lambda: _env_bool("CRAWL4AI_IGNORE_HTTPS_ERRORS", False)
    )
    java_script_enabled: bool = True
    cookies: list[dict[str, Any]] = Field(default_factory=list)
    headers: dict[str, str] = Field(default_factory=_default_headers)
    user_agent: str | None = Field(
        default_factory=lambda: os.environ.get("CRAWL4AI_USER_AGENT", "").strip() or None
    )
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
    wait_until: str = Field(
        default_factory=lambda: os.environ.get("CRAWL4AI_WAIT_UNTIL", "domcontentloaded").strip()
    )
    page_timeout: int = Field(
        default_factory=lambda: _env_int("CRAWL4AI_PAGE_TIMEOUT_MS", 45000), ge=1
    )
    wait_for: str | None = None
    wait_for_timeout: int | None = None
    wait_for_images: bool = False
    delay_before_return_html: float = Field(
        default_factory=lambda: _env_float("CRAWL4AI_DELAY_BEFORE_RETURN_HTML", 0.4),
        ge=0,
    )
    mean_delay: float = Field(default_factory=lambda: _env_float("CRAWL4AI_MEAN_DELAY", 0.2), ge=0)
    max_range: float = Field(
        default_factory=lambda: _env_float("CRAWL4AI_MAX_DELAY_RANGE", 0.5), ge=0
    )
    semaphore_count: int = Field(default=1, ge=1)
    scan_full_page: bool = False
    scroll_delay: float = Field(default=0.2, ge=0)
    max_scroll_steps: int | None = None
    process_iframes: bool = False
    flatten_shadow_dom: bool = False
    remove_overlay_elements: bool = Field(
        default_factory=lambda: _env_bool("CRAWL4AI_REMOVE_OVERLAYS", True)
    )
    remove_consent_popups: bool = Field(
        default_factory=lambda: _env_bool("CRAWL4AI_REMOVE_CONSENT_POPUPS", True)
    )
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

    serialize_requests: bool = Field(
        default_factory=lambda: _env_bool("CRAWL4AI_SERIALIZE_REQUESTS", True)
    )
    domain_delay_seconds: float = Field(
        default_factory=lambda: _env_float("CRAWL4AI_DOMAIN_DELAY_SECONDS", 2.0), ge=0
    )

    # Stealth and evasion options mapped as regular fields with env fallback
    enable_stealth: bool = Field(
        default_factory=lambda: _env_bool("CRAWL4AI_ENABLE_STEALTH", False)
    )
    magic: bool = Field(default_factory=lambda: _env_bool("CRAWL4AI_MAGIC", False))
    simulate_user: bool = Field(default_factory=lambda: _env_bool("CRAWL4AI_SIMULATE_USER", False))
    override_navigator: bool = Field(
        default_factory=lambda: _env_bool("CRAWL4AI_OVERRIDE_NAVIGATOR", False)
    )
    user_agent_mode: str = Field(
        default_factory=lambda: os.environ.get("CRAWL4AI_USER_AGENT_MODE", "").strip()
    )

    @model_validator(mode="after")
    def validate_target(self) -> Self:
        from service.research.content import canonical_url

        canonical_url(str(self.url))
        return self
