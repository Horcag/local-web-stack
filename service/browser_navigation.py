"""Fresh main-frame navigation evidence for one borrowed browser page."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any, Callable

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError


class NavigationWatch:
    def __init__(self, page: Any, status: Callable[[int | None], None]) -> None:
        self.page, self._status = page, status
        self.revision = 0
        self.inflight = False
        self._pending: tuple[str, int | None] | None = None
        self._session: Any = None
        self.document_id: str | None = None
        self._frame_id: str | None = None
        self._pending_loader: str | None = None
        self._request_pending = False
        self._documents: dict[str, int] = {}
        self._cdp_handlers: dict[str, Any] = {}
        self._handlers = {
            kind: (lambda event, kind=kind: self._observe(kind, event))
            for kind in ("request", "response", "framenavigated")
        }
        for kind, handler in self._handlers.items():
            page.on(kind, handler)

    def _observe(self, kind: str, event: Any) -> None:
        if self._session is not None:
            return
        if kind == "framenavigated":
            if event != self.page.main_frame:
                return
            pending = self._pending
            self._status(pending[1] if pending and pending[0] == event.url else None)
            self._pending, self.inflight = None, False
        else:
            request = event.request if kind == "response" else event
            # Service-worker resource requests can have no frame; they are not navigation.
            if not request.is_navigation_request():
                return
            try:
                if request.frame != self.page.main_frame:
                    return
            except PlaywrightError:
                self._status(None)
                self._pending, self.inflight = None, True
                self.revision += 1
                return
            status = event.status if kind == "response" else None
            status = status if type(status) is int and 100 <= status <= 599 else None
            self._status(status)
            self._pending = (event.url, status) if kind == "response" else None
            self.inflight = True
        self.revision += 1

    @property
    def native(self) -> bool:
        return self._session is not None

    async def start(self) -> None:
        context = getattr(self.page, "context", None)
        if context is None or not callable(getattr(context, "new_cdp_session", None)):
            return
        async with asyncio.timeout(5):
            self._session = await context.new_cdp_session(self.page)
            frame = (await self._session.send("Page.getFrameTree"))["frameTree"]["frame"]
            self._frame_id, self.document_id = frame["id"], frame["loaderId"]
            self._cdp_handlers = {
                "Network.requestWillBeSent": lambda event: self._native_request(event),
                "Network.responseReceived": lambda event: self._native_response(event),
                "Page.frameNavigated": lambda event: self._native_frame(event),
            }
            for kind, handler in self._cdp_handlers.items():
                self._session.on(kind, handler)
            await self._session.send("Page.enable")
            await self._session.send("Network.enable")

    def _native_request(self, event: dict) -> None:
        if event.get("type") != "Document" or event.get("frameId") != self._frame_id:
            return
        self._pending_loader = event.get("loaderId")
        self._request_pending = True
        self.inflight = True
        self._status(None)
        self.revision += 1

    def _native_response(self, event: dict) -> None:
        if event.get("type") != "Document" or event.get("frameId") != self._frame_id:
            return
        loader, status = event.get("loaderId"), event.get("response", {}).get("status")
        if isinstance(loader, str) and loader and type(status) is int and 100 <= status <= 599:
            self._documents[loader] = status
            if len(self._documents) > 8:
                self._documents.pop(next(iter(self._documents)))
        else:
            self._status(None)
        self.revision += 1

    def _native_frame(self, event: dict) -> None:
        frame = event.get("frame", {})
        if frame.get("parentId") or frame.get("id") != self._frame_id:
            return
        self.document_id = frame.get("loaderId")
        self._request_pending = False
        if self._pending_loader == self.document_id:
            self._pending_loader = None
        self._status(self._documents.get(self.document_id or ""))
        self.inflight = self.document_id not in self._documents
        self.revision += 1

    async def refresh_status(self) -> None:
        if self._session is None:
            return
        try:
            async with asyncio.timeout(5):
                frame = (await self._session.send("Page.getFrameTree"))["frameTree"]["frame"]
            loader = frame["loaderId"]
            if frame["id"] != self._frame_id or not isinstance(loader, str) or not loader:
                raise ValueError("Unknown browser document")
            self.document_id = loader
            self.inflight = self._request_pending and self._pending_loader != loader
            self._status(None if self.inflight else self._documents.get(loader))
        except (PlaywrightError, TimeoutError, KeyError, TypeError, ValueError):
            self._status(None)
            self.inflight = True

    def accept_goto(self) -> None:
        if self._session is None:
            self._pending, self.inflight = None, False

    async def close(self) -> None:
        for kind, handler in self._handlers.items():
            self.page.remove_listener(kind, handler)
        if self._session is not None:
            for kind, handler in self._cdp_handlers.items():
                self._session.remove_listener(kind, handler)
            with suppress(PlaywrightError, TimeoutError):
                async with asyncio.timeout(2):
                    await self._session.detach()


async def wait_for_document(page: Any) -> None:
    wait = getattr(page, "wait_for_load_state", None)
    if callable(wait):
        with suppress(PlaywrightTimeoutError, TimeoutError):
            async with asyncio.timeout(3):
                await wait("domcontentloaded", timeout=3000)
