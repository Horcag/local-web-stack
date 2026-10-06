"""Optional CDP browser adapter. Never launches or owns the connected browser."""

from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field


class OpenTab(BaseModel):
    url: str = Field(min_length=1, max_length=4096)


class TabAction(BaseModel):
    action: Literal["click", "fill", "scroll", "navigate"]
    selector: str = Field(default="", max_length=500)
    value: str = Field(default="", max_length=4096)


class BrowserRuntime:
    def __init__(self, endpoint: str | None = None) -> None:
        self.endpoint = (
            endpoint if endpoint is not None else os.getenv("LOCAL_WEB_BROWSER_CDP_URL", "")
        )
        self._driver: Any = None
        self._browser: Any = None
        self._tabs: dict[str, Any] = {}
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
            self._browser = await self._driver.chromium.connect_over_cdp(
                self.endpoint, timeout=15000
            )
        except Exception:
            await self.close()
            raise

    @staticmethod
    def _validate_url(url: str) -> None:
        from service.research.content import canonical_url

        try:
            canonical_url(url)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

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
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except BaseException:
            self._tabs.pop(tab_id, None)
            with suppress(Exception):
                await page.close()
            raise
        return tab_id

    def _page(self, tab_id: str) -> Any:
        page = self._tabs.get(tab_id)
        if page is None or page.is_closed():
            self._tabs.pop(tab_id, None)
            raise HTTPException(404, "Unknown or closed local-web tab")
        return page

    async def _snapshot(self, tab_id: str) -> dict:
        page = self._page(tab_id)
        text = await page.locator("body").inner_text(timeout=15000)
        links = await page.locator("a[href]").evaluate_all(
            "els => els.slice(0, 150).map(e => ({href:e.href,text:e.innerText.slice(0,150)}))"
        )
        return {
            "tab_id": tab_id,
            "url": page.url,
            "title": await page.title(),
            "markdown": text[:200000],
            "truncated": len(text) > 200000,
            "links": links,
            "method": "cdp-visible-text",
        }

    async def open(self, url: str) -> dict:
        async with self._lock:
            tab_id = await self._open(url)
            try:
                return await self._snapshot(tab_id)
            except BaseException:
                page = self._tabs.pop(tab_id, None)
                if page is not None:
                    with suppress(Exception):
                        await page.close()
                raise

    async def snapshot(self, tab_id: str) -> dict:
        async with self._lock:
            return await self._snapshot(tab_id)

    async def action(self, tab_id: str, request: TabAction) -> dict:
        async with self._lock:
            page = self._page(tab_id)
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
                await page.goto(request.value, wait_until="domcontentloaded", timeout=45000)
            else:
                await page.mouse.wheel(0, 800)
            return await self._snapshot(tab_id)

    async def close_tab(self, tab_id: str) -> dict:
        async with self._lock:
            page = self._page(tab_id)
            await page.close()
            self._tabs.pop(tab_id, None)
            return {"closed": tab_id}

    async def read(self, url: str) -> dict | None:
        if not self.configured:
            return None
        async with self._lock:
            tab_id = await self._open(url)
            try:
                return await self._snapshot(tab_id)
            finally:
                page = self._tabs.pop(tab_id, None)
                if page is not None:
                    with suppress(Exception):
                        await page.close()

    async def close(self) -> None:
        for page in self._tabs.values():
            with suppress(Exception):
                await page.close()
        self._tabs.clear()
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
