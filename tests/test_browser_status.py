"""Navigation response evidence and internal CAPTCHA-free reads use only owned tabs."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from service import browser as module
from service.browser import TabAction
from test_browser_runtime import runtime


@pytest.fixture
def no_recovery(monkeypatch):
    mocked = AsyncMock(return_value={"attempts": 0, "status": "not_needed"})
    monkeypatch.setattr(module, "recover_captcha", mocked)
    return mocked


@pytest.mark.parametrize("status", [200, 403])
def test_read_preserves_actual_goto_status_and_cleans_metadata(no_recovery, status):
    subject, page, borrowed = runtime()
    page.goto.return_value = SimpleNamespace(status=status)
    result = asyncio.run(subject.read("https://example.com"))
    assert result["status_code"] == status
    assert not subject._navigation_status and not subject._targets and not subject._tabs
    page.close.assert_awaited_once()
    borrowed.close.assert_not_awaited()
    no_recovery.assert_awaited_once()


@pytest.mark.parametrize(
    "response",
    [None, SimpleNamespace(status=None), SimpleNamespace(status=True), SimpleNamespace(status=0)],
)
def test_unknown_goto_response_never_fabricates_http_status(no_recovery, response):
    subject, page, _ = runtime()
    page.goto.return_value = response
    assert asyncio.run(subject.read("https://example.com"))["status_code"] is None


def test_internal_read_skips_solver_configuration_and_model(monkeypatch):
    subject, page, _ = runtime()
    page.goto.return_value = SimpleNamespace(status=200)
    recover = AsyncMock(side_effect=AssertionError("CAPTCHA recovery must not run"))
    monkeypatch.setattr(module, "recover_captcha", recover)
    monkeypatch.setattr(
        module.CaptchaSolver, "from_env", lambda: pytest.fail("Solver configuration must not load")
    )
    result = asyncio.run(subject.read("https://example.com", allow_captcha_recovery=False))
    assert result["captcha"] == {"attempts": 0, "status": "not_attempted"}
    assert result["status_code"] == 200
    recover.assert_not_awaited()
    assert not subject._navigation_status


@pytest.mark.parametrize(
    "receipt,expected",
    [
        ({"status": "solved", "final_status_code": 200}, 200),
        ({"status": "solved"}, None),
        ({"status": "unexpected_page", "final_status_code": 200}, 403),
        ({"status": "rejected", "final_status_code": 200}, 403),
    ],
)
def test_only_solved_recovery_replaces_original_navigation_status(no_recovery, receipt, expected):
    subject, page, _ = runtime()
    page.goto.return_value = SimpleNamespace(status=403)
    no_recovery.return_value = {"attempts": 1, **receipt}
    assert asyncio.run(subject.read("https://example.com"))["status_code"] == expected


def test_new_navigation_resets_previous_status_before_goto(no_recovery):
    subject, page, _ = runtime()
    page.goto.return_value = SimpleNamespace(status=403)

    async def run():
        opened = await subject.open("https://example.com")
        tab = opened["tab_id"]
        assert subject._navigation_status[tab] == 403

        async def goto(*args, **kwargs):
            assert subject._navigation_status[tab] is None
            return None

        page.goto.side_effect = goto
        result = await subject.action(
            tab, TabAction(action="navigate", value="https://example.com/next")
        )
        assert result["status_code"] is None
        await subject.close_tab(tab)
        assert tab not in subject._navigation_status

    asyncio.run(run())


def test_failed_navigation_does_not_keep_stale_status(no_recovery):
    subject, page, _ = runtime()
    page.goto.return_value = SimpleNamespace(status=200)

    async def run():
        tab = (await subject.open("https://example.com"))["tab_id"]
        page.goto.side_effect = RuntimeError("navigation failed")
        with pytest.raises(RuntimeError):
            await subject.action(
                tab, TabAction(action="navigate", value="https://example.com/next")
            )
        assert subject._navigation_status[tab] is None
        assert (await subject.snapshot(tab))["status_code"] is None
        await subject.close()
        assert not subject._navigation_status

    asyncio.run(run())


def test_closed_owned_page_forgets_navigation_metadata(no_recovery):
    subject, page, _ = runtime()
    page.goto.return_value = SimpleNamespace(status=200)

    async def run():
        tab = (await subject.open("https://example.com"))["tab_id"]
        page.is_closed = lambda: True
        with pytest.raises(HTTPException):
            await subject.snapshot(tab)
        assert tab not in subject._navigation_status and tab not in subject._targets

    asyncio.run(run())


@pytest.mark.parametrize("phase", ["goto", "snapshot"])
def test_failed_open_cleans_navigation_metadata(no_recovery, phase):
    subject, page, _ = runtime()
    if phase == "goto":
        page.goto.side_effect = asyncio.CancelledError()
        expected = asyncio.CancelledError
    else:
        page.goto.return_value = SimpleNamespace(status=200)
        page.locator_result.inner_text.side_effect = RuntimeError("snapshot failed")
        expected = RuntimeError
    with pytest.raises(expected):
        asyncio.run(subject.open("https://example.com"))
    assert not subject._navigation_status and not subject._targets and not subject._tabs
    page.close.assert_awaited_once()


def test_read_snapshot_failure_cleans_navigation_metadata(no_recovery):
    subject, page, _ = runtime()
    page.goto.return_value = SimpleNamespace(status=200)
    page.locator_result.inner_text.side_effect = RuntimeError("snapshot failed")
    with pytest.raises(RuntimeError):
        asyncio.run(subject.read("https://example.com"))
    assert not subject._navigation_status and not subject._tabs
    page.close.assert_awaited_once()


@pytest.mark.parametrize("final_status", [200, None])
def test_recovered_navigation_status_is_retained_for_later_snapshot(no_recovery, final_status):
    subject, page, _ = runtime()
    page.goto.return_value = SimpleNamespace(status=403)
    no_recovery.return_value = {"status": "solved", "final_status_code": final_status}

    async def run():
        opened = await subject.open("https://example.com")
        tab = opened["tab_id"]
        assert opened["status_code"] == final_status
        no_recovery.return_value = {"status": "not_needed"}
        assert (await subject.snapshot(tab))["status_code"] == final_status
        await subject.close_tab(tab)

    asyncio.run(run())


def event_page(page):
    callbacks = {}
    page.main_frame = SimpleNamespace(url=page.url)
    page.on = lambda kind, handler: callbacks.__setitem__(kind, handler)
    page.remove_listener = lambda kind, handler: callbacks.pop(kind)
    return callbacks


def emit_navigation(page, callbacks, status):
    request = SimpleNamespace(frame=page.main_frame, is_navigation_request=lambda: True)
    callbacks["request"](request)
    if status is not None:
        callbacks["response"](SimpleNamespace(request=request, url=page.url, status=status))
    page.main_frame.url = page.url
    callbacks["framenavigated"](page.main_frame)


def test_same_url_reload_replaces_cached_200_with_403(no_recovery):
    subject, page, _ = runtime()
    callbacks = event_page(page)
    page.goto.return_value = SimpleNamespace(status=200)

    async def run():
        tab = (await subject.open("https://example.com"))["tab_id"]
        emit_navigation(page, callbacks, 403)
        assert (await subject.snapshot(tab))["status_code"] == 403
        await subject.close_tab(tab)
        assert not callbacks and not subject._navigation_events

    asyncio.run(run())


@pytest.mark.parametrize("phase", ["request", "commit"])
def test_unknown_reload_invalidates_previous_200(no_recovery, phase):
    subject, page, _ = runtime()
    callbacks = event_page(page)
    page.goto.return_value = SimpleNamespace(status=200)

    async def run():
        tab = (await subject.open("https://example.com"))["tab_id"]
        if phase == "request":
            callbacks["request"](
                SimpleNamespace(frame=page.main_frame, is_navigation_request=lambda: True)
            )
        else:
            callbacks["framenavigated"](page.main_frame)
        assert (await subject.snapshot(tab))["status_code"] is None
        await subject.close()
        assert not callbacks

    asyncio.run(run())


def test_navigation_during_content_read_cannot_return_fresh_2xx(no_recovery):
    subject, page, _ = runtime()
    callbacks = event_page(page)
    page.goto.return_value = SimpleNamespace(status=200)

    async def run():
        tab = (await subject.open("https://example.com"))["tab_id"]

        async def text(**kwargs):
            emit_navigation(page, callbacks, 200)
            return "Content spanning a reload"

        page.locator_result.inner_text.side_effect = text
        assert (await subject.snapshot(tab))["status_code"] is None
        await subject.close()

    asyncio.run(run())


def test_later_reload_response_is_not_overwritten_by_older_goto_response(no_recovery):
    subject, page, _ = runtime()
    callbacks = event_page(page)

    async def goto(*args, **kwargs):
        emit_navigation(page, callbacks, 200)
        emit_navigation(page, callbacks, 403)
        return SimpleNamespace(status=200)

    page.goto.side_effect = goto
    assert asyncio.run(subject.read("https://example.com"))["status_code"] == 403
    assert not callbacks


def test_subframe_and_frameless_resources_do_not_change_main_status(no_recovery):
    from playwright.async_api import Error as PlaywrightError

    subject, page, _ = runtime()
    callbacks = event_page(page)
    page.goto.return_value = SimpleNamespace(status=200)

    class Resource:
        def is_navigation_request(self):
            return False

        @property
        def frame(self):
            raise PlaywrightError("Resource request has no frame")

    async def run():
        tab = (await subject.open("https://example.com"))["tab_id"]
        callbacks["request"](Resource())
        callbacks["response"](SimpleNamespace(request=Resource(), status=403))
        child = SimpleNamespace(url=page.url, child=True)
        request = SimpleNamespace(frame=child, is_navigation_request=lambda: True)
        callbacks["request"](request)
        callbacks["response"](SimpleNamespace(request=request, status=403, url=page.url))
        callbacks["framenavigated"](child)
        assert (await subject.snapshot(tab))["status_code"] == 200
        await subject.close()

    asyncio.run(run())


def test_unavailable_navigation_frame_fails_closed_without_callback_error(no_recovery):
    from playwright.async_api import Error as PlaywrightError

    subject, page, _ = runtime()
    callbacks = event_page(page)
    page.goto.return_value = SimpleNamespace(status=200)

    class Request:
        def is_navigation_request(self):
            return True

        @property
        def frame(self):
            raise PlaywrightError("Frame is not created yet")

    async def run():
        tab = (await subject.open("https://example.com"))["tab_id"]
        callbacks["request"](Request())
        assert (await subject.snapshot(tab))["status_code"] is None
        await subject.close()

    asyncio.run(run())


def test_stale_solved_receipt_cannot_overwrite_newer_observed_navigation(no_recovery):
    subject, page, _ = runtime()
    callbacks = event_page(page)
    page.goto.return_value = SimpleNamespace(status=200)

    async def recover(*args):
        emit_navigation(page, callbacks, 403)
        return {"status": "solved", "final_status_code": 200}

    no_recovery.side_effect = recover
    assert asyncio.run(subject.read("https://example.com"))["status_code"] == 403
    assert not callbacks
