"""CDP loader evidence distinguishes history updates from real document reloads."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from service import browser as module
from test_browser_runtime import runtime
from test_browser_status import event_page


class Session:
    def __init__(self):
        self.frame = {"id": "main", "loaderId": "initial", "url": "about:blank"}
        self.handlers = {}
        self.commands = []
        self.detach = AsyncMock()

    def on(self, kind, handler):
        self.handlers[kind] = handler

    def remove_listener(self, kind, handler):
        assert self.handlers.pop(kind) == handler

    async def send(self, command):
        self.commands.append(command)
        return {"frameTree": {"frame": dict(self.frame)}} if command == "Page.getFrameTree" else {}

    def emit(self, kind, event):
        if kind in self.handlers:
            self.handlers[kind](event)

    def document(self, loader, url, status):
        request = {"type": "Document", "frameId": "main", "loaderId": loader}
        self.emit("Network.requestWillBeSent", request)
        if status is not None:
            self.emit("Network.responseReceived", {**request, "response": {"status": status}})
        self.frame = {"id": "main", "loaderId": loader, "url": url}
        self.emit("Page.frameNavigated", {"frame": dict(self.frame)})


@pytest.fixture
def native(monkeypatch):
    subject, page, borrowed = runtime()
    callbacks = event_page(page)
    session = Session()
    page.context = SimpleNamespace(new_cdp_session=AsyncMock(return_value=session))
    monkeypatch.setattr(module, "recover_captcha", AsyncMock(return_value={"status": "not_needed"}))

    async def goto(url, **kwargs):
        assert kwargs == {"wait_until": "commit", "timeout": 45000}
        page.url = url
        page.main_frame.url = url
        session.document("first", url, 200)
        callbacks["framenavigated"](page.main_frame)
        return SimpleNamespace(status=200)

    page.goto.side_effect = goto
    return subject, page, session, callbacks, borrowed


def history(page, session, callbacks):
    page.url = "https://example.com/?text=hello+world&lr=51"
    page.main_frame.url = page.url
    session.frame["url"] = page.url
    session.emit("Page.navigatedWithinDocument", {"frameId": "main", "url": page.url})
    callbacks["framenavigated"](page.main_frame)


def test_history_normalization_keeps_verified_same_document_status(native):
    subject, page, session, callbacks, borrowed = native

    async def run():
        tab = (await subject.open("https://example.com/?text=hello%20world"))["tab_id"]
        history(page, session, callbacks)
        result = await subject.snapshot(tab)
        assert result["status_code"] == 200 and result["url"] == page.url
        await subject.close_tab(tab)
        assert not callbacks and not session.handlers and not subject._navigation_events

    asyncio.run(run())
    session.detach.assert_awaited_once()
    borrowed.close.assert_not_awaited()


@pytest.mark.parametrize("status", [403, None])
def test_actual_same_url_reload_never_retains_old_200(native, status):
    subject, page, session, callbacks, _ = native

    async def run():
        tab = (await subject.open("https://example.com/"))["tab_id"]
        session.document("reloaded", page.url, status)
        callbacks["framenavigated"](page.main_frame)
        assert (await subject.snapshot(tab))["status_code"] == status
        await subject.close()

    asyncio.run(run())
    session.detach.assert_awaited_once()


def test_verified_response_from_other_loader_cannot_authorize_current_document(native):
    subject, page, session, _, _ = native

    async def run():
        tab = (await subject.open("https://example.com/"))["tab_id"]
        session.emit(
            "Network.responseReceived",
            {
                "type": "Document",
                "frameId": "main",
                "loaderId": "unrelated",
                "response": {"status": 200},
            },
        )
        session.frame["loaderId"] = "unverified"
        assert (await subject.snapshot(tab))["status_code"] is None
        await subject.close()

    asyncio.run(run())


@pytest.mark.parametrize("reload", [False, True])
def test_document_identity_binds_content_snapshot(native, reload):
    subject, page, session, callbacks, _ = native

    async def run():
        tab = (await subject.open("https://example.com/?text=hello%20world"))["tab_id"]

        async def text(**kwargs):
            if reload:
                session.document("second", page.url, 200)
            else:
                history(page, session, callbacks)
            return "Visible document content"

        page.locator_result.inner_text.side_effect = text
        result = await subject.snapshot(tab)
        assert result["status_code"] == (None if reload else 200)
        await subject.close()

    asyncio.run(run())


def test_pending_cross_document_navigation_invalidates_old_document(native):
    subject, page, session, _, _ = native

    async def run():
        tab = (await subject.open("https://example.com/"))["tab_id"]
        session.emit(
            "Network.requestWillBeSent",
            {"type": "Document", "frameId": "main", "loaderId": "pending"},
        )
        assert (await subject.snapshot(tab))["status_code"] is None
        await subject.close()

    asyncio.run(run())


@pytest.mark.parametrize("failure", [RuntimeError("setup failed"), asyncio.CancelledError()])
def test_session_setup_error_or_cancel_detaches_owned_session(native, failure):
    subject, page, session, callbacks, _ = native
    original = session.send

    async def send(command):
        if command == "Page.enable":
            raise failure
        return await original(command)

    session.send = send
    with pytest.raises(type(failure)):
        asyncio.run(subject.open("https://example.com/"))
    session.detach.assert_awaited_once()
    page.close.assert_awaited_once()
    assert not callbacks and not session.handlers and not subject._navigation_events
    assert not subject._tabs and not subject._navigation_status


@pytest.mark.parametrize("loader", [None, "", 7])
def test_unknown_loader_request_cannot_rehydrate_old_status_before_commit(native, loader):
    subject, page, session, _, _ = native

    async def run():
        tab = (await subject.open("https://example.com/"))["tab_id"]
        session.emit(
            "Network.requestWillBeSent", {"type": "Document", "frameId": "main", "loaderId": loader}
        )
        assert (await subject.snapshot(tab))["status_code"] is None
        await subject.close()

    asyncio.run(run())
