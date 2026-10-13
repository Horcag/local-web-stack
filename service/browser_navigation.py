"""Fresh main-frame navigation evidence for one borrowed browser page."""

from __future__ import annotations

from typing import Any, Callable

from playwright.async_api import Error as PlaywrightError


class NavigationWatch:
    def __init__(self, page: Any, status: Callable[[int | None], None]) -> None:
        self.page, self._status = page, status
        self.revision = 0
        self.inflight = False
        self._pending: tuple[str, int | None] | None = None
        self._handlers = {
            kind: (lambda event, kind=kind: self._observe(kind, event))
            for kind in ("request", "response", "framenavigated")
        }
        for kind, handler in self._handlers.items():
            page.on(kind, handler)

    def _observe(self, kind: str, event: Any) -> None:
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

    def accept_goto(self) -> None:
        self._pending, self.inflight = None, False

    def close(self) -> None:
        for kind, handler in self._handlers.items():
            self.page.remove_listener(kind, handler)
