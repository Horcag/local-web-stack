"""Optional CDP browser adapter. Never launches or owns the connected browser."""

from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from typing import Any, Literal
from uuid import uuid4

import httpx as httpx

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from service.browser_navigation import NavigationWatch, wait_for_document
from service.browser_endpoint import _cdp_endpoint as _cdp_endpoint, _cdp_headers as _cdp_headers
from service.browser_search import read_search
from service.captcha_browser import recover_captcha
from service.captcha_solver import CaptchaSolver


class OpenTab(BaseModel):
    url: str = Field(min_length=1, max_length=4096)


class TabAction(BaseModel):
    action: Literal["click", "fill", "scroll", "navigate"]
    selector: str = Field(default="", max_length=500)
    value: str = Field(default="", max_length=4096)


class BrowserRuntime:
    def __init__(
        self,
        endpoint: str | None = None,
        *,
        host_header: str | None = None,
        targeted_reads: bool | None = None,
    ) -> None:
        self.endpoint = (
            endpoint if endpoint is not None else os.getenv("LOCAL_WEB_BROWSER_CDP_URL", "")
        )
        self._host_header = (
            host_header
            if host_header is not None
            else os.getenv("LOCAL_WEB_BROWSER_CDP_HOST_HEADER", "")
        )
        self._targeted_reads = (
            targeted_reads
            if targeted_reads is not None
            else os.getenv("LOCAL_WEB_BROWSER_TARGETED_READS", "true").lower()
            in {"1", "true", "yes"}
        )
        self._driver: Any = None
        self._browser: Any = None
        self._tabs: dict[str, Any] = {}
        self._targets: dict[str, str] = {}
        self._navigation_status: dict[str, int | None] = {}
        self._navigation_events: dict[str, NavigationWatch] = {}
        self._lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return bool(self.endpoint)

    async def _connect(self) -> None:
        if not self.configured:
            raise HTTPException(
                503, "No CDP route configured. Use existing browser tools and import the result."
            )
        if self._browser is not None and self._browser.is_connected():
            return
        await self.close()
        from playwright.async_api import async_playwright

        self._driver = await async_playwright().start()
        try:
            endpoint = await _cdp_endpoint(self.endpoint, self._host_header)
            options: dict[str, Any] = {"timeout": 15000, "no_defaults": True}
            headers = _cdp_headers(self._host_header)
            if headers:
                options["headers"] = headers
            self._browser = await self._driver.chromium.connect_over_cdp(endpoint, **options)
        except BaseException:
            await self.close()
            raise

    @staticmethod
    def _validate_url(url: str) -> None:
        from service.research.content import canonical_url

        try:
            canonical_url(url)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    async def _watch_navigation(self, page: Any, tab_id: str) -> None:
        if callable(getattr(page, "on", None)):
            self._navigation_events[tab_id] = NavigationWatch(
                page, lambda status: self._navigation_status.__setitem__(tab_id, status)
            )
            await self._navigation_events[tab_id].start()

    async def _forget(self, tab_id: str) -> Any:
        page = self._tabs.pop(tab_id, None)
        watch = self._navigation_events.pop(tab_id, None)
        self._targets.pop(tab_id, None)
        self._navigation_status.pop(tab_id, None)
        if watch:
            await watch.close()
        return page

    async def _navigate(self, page: Any, tab_id: str, url: str) -> None:
        self._navigation_status[tab_id] = None
        watch = self._navigation_events.get(tab_id)
        revision = watch.revision if watch else 0
        response = await page.goto(url, wait_until="commit", timeout=45000)
        if watch and watch.revision != revision:
            return
        if watch:
            watch.accept_goto()
        status = getattr(response, "status", None)
        if type(status) is int and 100 <= status <= 599:
            self._navigation_status[tab_id] = status

    async def _open(self, url: str) -> str:
        self._validate_url(url)
        await self._connect()
        if len(self._tabs) >= 8:
            raise HTTPException(409, "Close a local-web tab before opening more (limit: 8)")
        contexts = self._browser.contexts
        if not contexts:
            raise HTTPException(503, "CDP browser has no available context")
        page = await contexts[0].new_page()
        tab_id = uuid4().hex
        self._tabs[tab_id] = page
        self._targets[tab_id] = url
        try:
            await self._watch_navigation(page, tab_id)
            await self._navigate(page, tab_id, url)
        except BaseException:
            await self._forget(tab_id)
            with suppress(Exception):
                await page.close()
            raise
        return tab_id

    async def _page(self, tab_id: str) -> Any:
        page = self._tabs.get(tab_id)
        if page is None or page.is_closed():
            await self._forget(tab_id)
            raise HTTPException(404, "Unknown or closed local-web tab")
        return page

    async def _snapshot(self, tab_id: str, *, allow_captcha_recovery: bool = True) -> dict:
        page = await self._page(tab_id)
        watch = self._navigation_events.get(tab_id)
        recovery_revision = watch.revision if watch else 0
        captcha = {"attempts": 0, "status": "not_attempted"}
        if allow_captcha_recovery:
            await wait_for_document(page)
            captcha = await recover_captcha(page, CaptchaSolver.from_env(), self._targets[tab_id])
        if captcha.get("status") == "solved" and (not watch or watch.revision == recovery_revision):
            final_status = captcha.get("final_status_code")
            self._navigation_status[tab_id] = (
                final_status if type(final_status) is int and 100 <= final_status <= 599 else None
            )
        if watch:
            await watch.refresh_status()
        original_url, revision = page.url, watch.revision if watch else 0
        document = watch.document_id if watch else None
        text = await page.locator("body").inner_text(timeout=15000)
        links = await page.locator("a[href]").evaluate_all(
            "els => els.slice(0, 150).map(e => ({href:e.href,text:e.innerText.slice(0,150)}))"
        )
        title = await page.title()
        if watch:
            await watch.refresh_status()
        stable = (watch.revision if watch else 0) == revision
        stable = stable and (
            watch.document_id == document if watch and watch.native else page.url == original_url
        )
        return {
            "tab_id": tab_id,
            "url": page.url,
            "title": title,
            "markdown": text[:200000],
            "truncated": len(text) > 200000,
            "links": links,
            "method": "cdp-visible-text",
            "captcha": captcha,
            "status_code": self._navigation_status.get(tab_id)
            if stable and not (watch and watch.inflight)
            else None,
        }

    async def open(self, url: str) -> dict:
        async with self._lock:
            tab_id = await self._open(url)
            try:
                return await self._snapshot(tab_id)
            except BaseException:
                page = await self._forget(tab_id)
                if page is not None:
                    with suppress(Exception):
                        await page.close()
                raise

    async def snapshot(self, tab_id: str) -> dict:
        async with self._lock:
            return await self._snapshot(tab_id)

    async def action(self, tab_id: str, request: TabAction) -> dict:
        async with self._lock:
            page = await self._page(tab_id)
            if request.action in {"click", "fill"}:
                if not request.selector:
                    raise HTTPException(422, "selector is required")
                locator = page.locator(request.selector)
                if request.action == "click":
                    await locator.click(timeout=10000)
                else:
                    await locator.fill(request.value, timeout=10000)
            elif request.action == "navigate":
                self._validate_url(request.value)
                await self._navigate(page, tab_id, request.value)
                self._targets[tab_id] = request.value
            else:
                await page.mouse.wheel(0, 800)
            return await self._snapshot(tab_id)

    async def close_tab(self, tab_id: str) -> dict:
        async with self._lock:
            page = await self._page(tab_id)
            await page.close()
            await self._forget(tab_id)
            return {"closed": tab_id}

    async def read(self, url: str, *, allow_captcha_recovery: bool = True) -> dict | None:
        if not self.configured:
            return None
        if self._targeted_reads and not allow_captcha_recovery:
            from service.cdp_reader import read_cdp

            return await read_cdp(self, url)
        async with self._lock:
            tab_id = await self._open(url)
            try:
                return await self._snapshot(tab_id, allow_captcha_recovery=allow_captcha_recovery)
            finally:
                page = await self._forget(tab_id)
                if page is not None:
                    with suppress(Exception):
                        await page.close()

    async def read_search(self, url: str, *, allow_captcha_recovery: bool = False) -> dict | None:
        return await read_search(self, url, allow_captcha_recovery=allow_captcha_recovery)

    async def close(self) -> None:
        for tab_id in list(self._tabs):
            page = await self._forget(tab_id)
            with suppress(Exception):
                await page.close()
        self._tabs.clear()
        self._targets.clear()
        self._navigation_status.clear()
        # stop the connection/driver; never invoke browser.close() on a borrowed browser.
        if self._driver is not None:
            with suppress(Exception):
                await self._driver.stop()
        self._driver = self._browser = None


def create_browser_router(runtime: BrowserRuntime) -> APIRouter:
    router = APIRouter(prefix="/browser", tags=["browser"])

    @router.get("/status")
    async def status() -> dict:
        return {
            "configured": runtime.configured,
            "owned_tabs": len(runtime._tabs),
            "fallback": "cdp" if runtime.configured else "external-browser-import",
        }

    @router.post("/tabs")
    async def open_tab(request: OpenTab) -> dict:
        return await runtime.open(request.url)

    @router.get("/tabs/{tab_id}")
    async def snapshot(tab_id: str) -> dict:
        return await runtime.snapshot(tab_id)

    @router.post("/tabs/{tab_id}/action")
    async def action(tab_id: str, request: TabAction) -> dict:
        return await runtime.action(tab_id, request)

    @router.delete("/tabs/{tab_id}")
    async def close_tab(tab_id: str) -> dict:
        return await runtime.close_tab(tab_id)

    return router
