"""Bounded CDP reads in one owned target, without attaching other browser pages."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from websockets.asyncio.client import connect as WebsocketConnect


MAX_MESSAGE_BYTES = 1024 * 1024
MAX_MESSAGES_PER_COMMAND = 2048
COMMAND_SECONDS = 5
READ_SECONDS = 20


class connect(WebsocketConnect):
    """The configured CDP route must not silently follow websocket redirects."""

    def process_redirect(self, exc: Exception) -> Exception:
        return exc


class OwnedCDP:
    """Sequential commands keep one request in flight and retain no event payloads."""

    def __init__(self, websocket: Any) -> None:
        self.websocket = websocket
        self.sequence = 0
        self.methods: list[str] = []
        self.session: str | None = None
        self.target: str | None = None
        self.frame: str | None = None
        self.revision = 0
        self.pending_loader: str | None = None
        self.pending = False
        self.documents: dict[str, int] = {}
        self.loaded: set[str] = set()
        self.navigation_loader: str | None = None

    def observe(self, message: dict) -> None:
        if not self.session or message.get("sessionId") != self.session:
            return
        method, data = message.get("method"), message.get("params", {})
        if not isinstance(data, dict):
            return
        if method == "Page.frameNavigated":
            frame = data.get("frame", {})
            if frame.get("id") == self.frame and not frame.get("parentId"):
                self.revision += 1
                self.pending = False
        elif method in {"Network.requestWillBeSent", "Network.responseReceived"}:
            if data.get("type") != "Document" or data.get("frameId") != self.frame:
                return
            self.revision += 1
            loader = data.get("loaderId")
            if method == "Network.requestWillBeSent":
                self.pending_loader = loader
                self.pending = True
            else:
                status = data.get("response", {}).get("status")
                if isinstance(loader, str) and type(status) is int and 100 <= status <= 599:
                    self.documents[loader] = status
                    if len(self.documents) > 8:
                        self.documents.pop(next(iter(self.documents)))
        elif method == "Page.lifecycleEvent":
            if data.get("frameId") == self.frame and data.get("name") == "DOMContentLoaded":
                loader = data.get("loaderId")
                if isinstance(loader, str):
                    self.loaded.add(loader)
                    if len(self.loaded) > 8:
                        self.loaded.pop()

    async def receive(self) -> dict:
        raw = await self.websocket.recv()
        if len(raw) > MAX_MESSAGE_BYTES:
            raise ValueError("CDP message exceeds bound")
        message = json.loads(raw)
        if not isinstance(message, dict):
            raise ValueError("Invalid CDP message")
        self.observe(message)
        return message

    async def call(self, method: str, params: dict | None = None, *, browser: bool = False) -> dict:
        self.sequence += 1
        self.methods.append(method)
        request = {"id": self.sequence, "method": method, "params": params or {}}
        if not browser:
            if not self.session:
                raise ValueError("No owned CDP session")
            request["sessionId"] = self.session
        async with asyncio.timeout(15 if method == "Page.navigate" else COMMAND_SECONDS):
            await self.websocket.send(json.dumps(request))
            for _ in range(MAX_MESSAGES_PER_COMMAND):
                message = await self.receive()
                if message.get("id") != request["id"]:
                    continue
                if message.get("sessionId") != request.get("sessionId"):
                    raise ValueError("CDP response session mismatch")
                if "error" in message:
                    raise RuntimeError("CDP command failed: " + method)
                result = message.get("result")
                if not isinstance(result, dict):
                    raise ValueError("Invalid CDP command result")
                return result
        raise ValueError("CDP message count exceeds bound")

    async def create_target(self) -> None:
        async def create() -> None:
            result = await self.call(
                "Target.createTarget",
                {"url": "about:blank", "hidden": True, "background": True},
                browser=True,
            )
            target = result.get("targetId")
            if not isinstance(target, str) or not target:
                raise ValueError("Created CDP target identity unverified")
            self.target = target

        task = asyncio.create_task(create())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # Resolve a possibly created target before closing the connection.
            with suppress(Exception):
                await asyncio.shield(task)
            raise

    async def setup(self) -> None:
        await self.create_target()
        result = await self.call(
            "Target.attachToTarget", {"targetId": self.target, "flatten": True}, browser=True
        )
        session = result.get("sessionId")
        if not isinstance(session, str) or not session:
            raise ValueError("Owned CDP session identity unverified")
        self.session = session
        await self.call("Page.enable")
        await self.call("Network.enable", {"maxTotalBufferSize": 0, "maxResourceBufferSize": 0})
        await self.call("Page.setLifecycleEventsEnabled", {"enabled": True})
        frame = (await self.call("Page.getFrameTree"))["frameTree"]["frame"]
        frame_id = frame.get("id") if isinstance(frame, dict) else None
        if not isinstance(frame_id, str) or not frame_id.strip():
            raise ValueError("Owned CDP main frame identity unverified")
        self.frame = frame_id

    async def navigate(self, url: str, *, search: bool = False) -> None:
        navigation = await self.call("Page.navigate", {"url": url})
        loader = navigation.get("loaderId")
        if (
            navigation.get("errorText")
            or navigation.get("frameId") != self.frame
            or not isinstance(loader, str)
            or not loader
        ):
            raise ValueError("Fresh CDP navigation unverified")
        self.navigation_loader = loader
        # Commit plus fresh HTTP evidence is sufficient; some SERPs never emit DCL.
        from service.research.content import is_captcha_gate

        with suppress(TimeoutError):
            async with asyncio.timeout(8 if search else 3):
                for _ in range(80 if search else 30):
                    readiness = await self.call(
                        "Runtime.evaluate",
                        {"expression": _readiness_script(search), "returnByValue": True},
                    )
                    value = readiness.get("result", {}).get("value")
                    if search and isinstance(value, dict):
                        text, title = value.get("text", ""), value.get("title", "")
                        if (
                            value.get("cards")
                            or is_captcha_gate(text, title)
                            or text.strip()
                            in {
                                "По вашему запросу ничего не найдено",
                                "No results found",
                            }
                        ):
                            break
                    elif not search and value is True:
                        break
                    await asyncio.sleep(0.1)

    async def snapshot(self, search: bool) -> dict:
        before = (await self.call("Page.getFrameTree"))["frameTree"]["frame"]
        revision = self.revision
        loader = before.get("loaderId")
        evaluation = await self.call(
            "Runtime.evaluate",
            {"expression": _snapshot_script(search), "returnByValue": True},
        )
        if evaluation.get("exceptionDetails"):
            raise ValueError("CDP DOM extraction failed")
        value = evaluation.get("result", {}).get("value")
        if not isinstance(value, dict):
            raise ValueError("CDP DOM snapshot unverified")
        after = (await self.call("Page.getFrameTree"))["frameTree"]["frame"]
        stable = bool(
            isinstance(before.get("id"), str)
            and bool(before.get("id").strip())
            and isinstance(after.get("id"), str)
            and before.get("id") == after.get("id") == self.frame
            and isinstance(loader, str)
            and loader
            and after.get("loaderId") == loader
            and before.get("url") == after.get("url") == value.get("url")
            and self.revision == revision
            and not self.pending
            and loader == self.navigation_loader
        )
        status = self.documents.get(loader) if stable else None
        value.update(
            method="cdp-visible-text",
            status_code=status,
            captcha={"status": "not_attempted", "attempts": 0},
            search_document={"verified": stable, "document_id": loader, "revision": revision},
        )
        if search and not stable:
            value["search_results"] = []
        return value

    async def close_target(self) -> None:
        if self.target:
            result = await self.call("Target.closeTarget", {"targetId": self.target}, browser=True)
            if result.get("success") is not True:
                raise RuntimeError("Owned CDP target cleanup unverified")
            self.target = None


def _organic_expression() -> str:
    from service.browser_search import ORGANIC_SCRIPT, ORGANIC_SELECTOR

    return (
        f"({ORGANIC_SCRIPT})(Array.from(document.querySelectorAll({json.dumps(ORGANIC_SELECTOR)})))"
    )


def _readiness_script(search: bool) -> str:
    if not search:
        return "Boolean(document.body && document.body.innerText.trim())"
    return (
        "({readiness: true, cards: "
        + _organic_expression()
        + ", title: document.title, text: document.body ? document.body.innerText.slice(0,2000) : ''})"
    )


def _snapshot_script(search: bool) -> str:
    cards = _organic_expression() if search else "[]"
    return (
        """(() => {
        const body = document.body ? document.body.innerText : '';
        return {url: location.href, title: document.title, markdown: body.slice(0, 200000),
                truncated: body.length > 200000,
                links: Array.from(document.querySelectorAll('a[href]')).slice(0,150).map(
                    a => ({href:a.href, text:a.innerText.slice(0,150)})),
                search_results: """
        + cards
        + """};
    })()"""
    )


def _connection_options(endpoint: str, host_header: str | None) -> tuple[str, dict]:
    from service.browser_endpoint import _cdp_headers

    _cdp_headers(host_header)
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme not in {"ws", "wss"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in endpoint)
    ):
        raise ValueError("Resolved CDP websocket endpoint unverified")
    options: dict = {
        "proxy": None,
        "compression": None,
        "open_timeout": 5,
        "close_timeout": 2,
        "max_size": MAX_MESSAGE_BYTES,
        "max_queue": 1,
        "write_limit": 65536,
    }
    if host_header:
        options.update(
            host=parsed.hostname, port=parsed.port or (443 if parsed.scheme == "wss" else 80)
        )
        if parsed.scheme == "wss":
            options["server_hostname"] = parsed.hostname
        endpoint = urlunsplit((parsed.scheme, host_header, parsed.path, parsed.query, ""))
    return endpoint, options


async def read_cdp(runtime: Any, url: str, *, search: bool = False) -> dict | None:
    """Use the existing protected endpoint/profile; always close the exact new target."""
    from service.browser_endpoint import _cdp_endpoint

    runtime._validate_url(url)
    if not runtime.configured:
        return None
    async with asyncio.timeout(READ_SECONDS):
        async with runtime._lock:
            endpoint = await _cdp_endpoint(
                runtime.endpoint, runtime._host_header, websocket_required=True
            )
            address, options = _connection_options(endpoint, runtime._host_header)
            async with connect(address, **options) as websocket:
                reader = OwnedCDP(websocket)
                snapshot = None
                try:
                    await reader.setup()
                    await reader.navigate(url, search=search)
                    snapshot = await reader.snapshot(search)
                    return snapshot
                finally:
                    # Cleanup has its own bound and survives the read deadline cancellation.
                    target = reader.target
                    cleanup = asyncio.create_task(reader.close_target())
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        await asyncio.shield(cleanup)
                        raise
                    if snapshot is not None:
                        snapshot["browser_cleanup"] = {
                            "target_id": target,
                            "closed": reader.target is None,
                            "method": "Target.closeTarget",
                            "hidden": True,
                        }
                        snapshot["cdp_methods"] = reader.methods
