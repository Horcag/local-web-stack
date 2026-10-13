"""Offline native protocol tests protect target ownership and fresh document evidence."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from service import cdp_reader as module


URL = "https://yandex.ru/search/?text=example"


class Socket:
    def __init__(self, change=None, failure=None):
        self.queue = asyncio.Queue()
        self.commands = []
        self.change, self.failure = change, failure
        self.loader = "fresh"
        self.url = URL
        self.status = 200
        self.emit_dcl = True
        self.frame_id = "main"
        self.readiness_checks = 0
        self.readiness_empty_checks = 0

    async def send(self, raw):
        command = json.loads(raw)
        self.commands.append(command)
        method = command["method"]
        session = command.get("sessionId")
        assert method not in {"Browser.close", "Target.getTargets", "Target.setAutoAttach"}
        if method not in {"Target.createTarget", "Target.attachToTarget", "Target.closeTarget"}:
            assert session == "own-session"
        if self.failure == method:
            await self.queue.put(
                json.dumps(
                    {"id": command["id"], "sessionId": session, "error": {"message": "private"}}
                )
            )
            return
        result = {}
        if method == "Target.createTarget":
            assert command["params"] == {"url": "about:blank", "hidden": True, "background": True}
            result = {"targetId": "own-target"}
        elif method == "Target.attachToTarget":
            assert command["params"] == {"targetId": "own-target", "flatten": True}
            result = {"sessionId": "own-session"}
        elif method == "Page.getFrameTree":
            result = {
                "frameTree": {
                    "frame": {"id": self.frame_id, "loaderId": self.loader, "url": self.url}
                }
            }
        elif method == "Page.navigate":
            assert command["params"]["url"] == URL
            await self.event(
                "Network.requestWillBeSent",
                {"type": "Document", "frameId": "main", "loaderId": "fresh"},
            )
            await self.event(
                "Network.responseReceived",
                {
                    "type": "Document",
                    "frameId": "sub",
                    "loaderId": "fresh",
                    "response": {"status": 503},
                },
            )
            await self.event(
                "Network.responseReceived",
                {
                    "type": "Document",
                    "frameId": "main",
                    "loaderId": "old",
                    "response": {"status": 403},
                },
            )
            await self.event(
                "Network.responseReceived",
                {
                    "type": "Document",
                    "frameId": "main",
                    "loaderId": "fresh",
                    "response": {"status": self.status},
                },
            )
            await self.event("Page.frameNavigated", {"frame": {"id": "main", "loaderId": "fresh"}})
            if self.emit_dcl:
                await self.event(
                    "Page.lifecycleEvent",
                    {"frameId": "main", "loaderId": "fresh", "name": "DOMContentLoaded"},
                )
            result = {"frameId": "main", "loaderId": "fresh"}
        elif method == "Runtime.evaluate":
            if command["params"]["expression"].startswith("({readiness:"):
                self.readiness_checks += 1
                cards = (
                    []
                    if self.readiness_checks <= self.readiness_empty_checks
                    else [{"title": "Ready"}]
                )
                result = {
                    "result": {
                        "value": {"cards": cards, "text": "Search loading shell", "title": "Яндекс"}
                    }
                }
                await self.queue.put(
                    json.dumps({"id": command["id"], "sessionId": session, "result": result})
                )
                return
            if command["params"]["expression"].startswith("Boolean("):
                await self.queue.put(
                    json.dumps(
                        {
                            "id": command["id"],
                            "sessionId": session,
                            "result": {"result": {"value": True}},
                        }
                    )
                )
                return
            assert "location.href" in command["params"]["expression"]
            value = {
                "url": URL,
                "title": "Results",
                "markdown": "Actual visible text",
                "truncated": False,
                "links": [],
                "search_results": [
                    {
                        "href": "https://example.org/",
                        "title": "Title",
                        "source": "yandex-organic-card",
                    }
                ],
            }
            result = {"result": {"value": value}}
            if self.change == "loader":
                self.loader = "changed"
            elif self.change == "url":
                self.url = "https://elsewhere.org/"
            elif self.change == "pending":
                await self.event(
                    "Network.requestWillBeSent",
                    {"type": "Document", "frameId": "main", "loaderId": "changed"},
                )
            elif self.change == "revision":
                await self.event(
                    "Page.frameNavigated", {"frame": {"id": "main", "loaderId": "fresh"}}
                )
        elif method == "Target.closeTarget":
            assert command["params"] == {"targetId": "own-target"}
            result = {"success": self.change != "cleanup"}
        reply = {"id": command["id"], "result": result}
        if session:
            reply["sessionId"] = session
        await self.queue.put(json.dumps(reply))

    async def event(self, method, params, session="own-session"):
        await self.queue.put(json.dumps({"method": method, "params": params, "sessionId": session}))

    async def recv(self):
        return await self.queue.get()


def runtime():
    return SimpleNamespace(
        configured=True,
        endpoint="http://protected:9226",
        _host_header="localhost",
        _lock=asyncio.Lock(),
        _validate_url=lambda url: None,
    )


def install(monkeypatch, socket):
    from service import browser_endpoint

    async def endpoint(*args, **kwargs):
        assert kwargs == {"websocket_required": True}
        return "ws://protected:9226/devtools/browser/own-test"

    @asynccontextmanager
    async def connect(address, **options):
        assert address == "ws://localhost/devtools/browser/own-test"
        assert options["host"] == "protected" and options["port"] == 9226
        assert options["proxy"] is None
        assert options["max_size"] == 1048576 and options["max_queue"] == 1
        yield socket

    monkeypatch.setattr(browser_endpoint, "_cdp_endpoint", endpoint)
    monkeypatch.setattr(module, "connect", connect)


def test_native_reader_owns_only_new_target_and_fresh_status(monkeypatch):
    socket = Socket()
    install(monkeypatch, socket)
    result = asyncio.run(module.read_cdp(runtime(), URL, search=True))
    assert result["status_code"] == 200
    assert result["search_document"]["verified"] is True
    assert result["search_results"][0]["title"] == "Title"
    assert result["captcha"] == {"status": "not_attempted", "attempts": 0}
    assert socket.commands[-1]["method"] == "Target.closeTarget"
    assert result["browser_cleanup"] == {
        "target_id": "own-target",
        "closed": True,
        "method": "Target.closeTarget",
        "hidden": True,
    }
    assert "Target.setAutoAttach" not in result["cdp_methods"]


@pytest.mark.parametrize("change", ["loader", "url", "pending", "revision"])
def test_changed_document_never_accepts_cards_or_status(monkeypatch, change):
    socket = Socket(change=change)
    install(monkeypatch, socket)
    result = asyncio.run(module.read_cdp(runtime(), URL, search=True))
    assert result["search_results"] == []
    assert result["status_code"] is None
    assert result["search_document"]["verified"] is False
    assert socket.commands[-1]["method"] == "Target.closeTarget"


def test_cleanup_failure_refuses_success(monkeypatch):
    socket = Socket(change="cleanup")
    install(monkeypatch, socket)
    with pytest.raises(RuntimeError, match="cleanup unverified"):
        asyncio.run(module.read_cdp(runtime(), URL, search=True))


@pytest.mark.parametrize("failure", ["Target.attachToTarget", "Page.enable", "Runtime.evaluate"])
def test_protocol_failure_closes_exact_owned_target(monkeypatch, failure):
    socket = Socket(failure=failure)
    install(monkeypatch, socket)
    with pytest.raises(RuntimeError, match="CDP command failed") as exc:
        asyncio.run(module.read_cdp(runtime(), URL))
    assert "private" not in str(exc.value)
    assert socket.commands[-1]["method"] == "Target.closeTarget"


def test_foreign_session_events_never_supply_document_status():
    reader = module.OwnedCDP(Socket())
    reader.session, reader.frame = "own-session", "main"
    reader.observe(
        {
            "sessionId": "foreign",
            "method": "Network.responseReceived",
            "params": {
                "type": "Document",
                "frameId": "main",
                "loaderId": "fresh",
                "response": {"status": 200},
            },
        }
    )
    assert reader.documents == {} and reader.revision == 0


def test_bounded_frame_and_event_counts(monkeypatch):
    class Oversized(Socket):
        async def recv(self):
            return "x" * (module.MAX_MESSAGE_BYTES + 1)

    with pytest.raises(ValueError, match="message exceeds"):
        asyncio.run(module.OwnedCDP(Oversized()).receive())
    monkeypatch.setattr(module, "MAX_MESSAGES_PER_COMMAND", 2)
    socket = Socket()

    async def ignored():
        return "{}"

    socket.recv = ignored

    async def send_noop(raw):
        pass

    socket.send = send_noop
    with pytest.raises(ValueError, match="count exceeds"):
        asyncio.run(module.OwnedCDP(socket).call("Browser.getVersion", browser=True))


def test_unknown_status_remains_unknown(monkeypatch):
    socket = Socket()
    socket.status = None
    install(monkeypatch, socket)
    result = asyncio.run(module.read_cdp(runtime(), URL))
    assert result["status_code"] is None


def test_secure_host_override_preserves_tls_verification():
    address, options = module._connection_options(
        "wss://protected.example.org:9443/devtools/browser/x", "localhost"
    )
    assert address == "wss://localhost/devtools/browser/x"
    assert options["server_hostname"] == "protected.example.org"
    assert options["host"] == "protected.example.org" and options["port"] == 9443
    assert "ssl" not in options


@pytest.mark.parametrize("stage", ["Target.createTarget", "Runtime.evaluate"])
def test_cancellation_resolves_and_closes_exact_created_target(monkeypatch, stage):
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()

        class Delayed(Socket):
            async def send(self, raw):
                if json.loads(raw)["method"] == stage and not json.loads(raw).get("params", {}).get(
                    "expression", ""
                ).startswith("Boolean("):
                    started.set()
                    if stage == "Target.createTarget":
                        await release.wait()
                    else:
                        await asyncio.Event().wait()
                await super().send(raw)

        socket = Delayed()
        install(monkeypatch, socket)
        task = asyncio.create_task(module.read_cdp(runtime(), URL, search=True))
        await started.wait()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        closes = [
            command for command in socket.commands if command["method"] == "Target.closeTarget"
        ]
        assert len(closes) == 1 and closes[0]["params"] == {"targetId": "own-target"}
        assert not [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task() and not task.done()
        ]

    asyncio.run(scenario())


def test_create_failure_does_not_close_unknown_or_foreign_target(monkeypatch):
    socket = Socket(failure="Target.createTarget")
    install(monkeypatch, socket)
    with pytest.raises(RuntimeError):
        asyncio.run(module.read_cdp(runtime(), URL))
    assert [command["method"] for command in socket.commands] == ["Target.createTarget"]


def test_deadline_includes_runtime_lock(monkeypatch):
    async def scenario():
        subject = runtime()
        await subject._lock.acquire()
        monkeypatch.setattr(module, "READ_SECONDS", 0.01)
        with pytest.raises(TimeoutError):
            await module.read_cdp(subject, URL)
        subject._lock.release()

    asyncio.run(scenario())


def test_configured_cdp_socket_does_not_follow_redirects():
    socket = module.connect("ws://localhost:9226/devtools/browser/test", proxy=None)
    error = RuntimeError("redirect")
    assert socket.process_redirect(error) is error


@pytest.mark.parametrize(
    "endpoint",
    [
        "ws://user:password@localhost:9226/devtools/browser/test",
        "ws://localhost:9226/devtools/browser/test#fragment",
        "ws://localhost:9226/devtools/browser/test\n",
        "http://localhost:9226/devtools/browser/test",
    ],
)
def test_native_socket_rejects_invalid_endpoint_identity(endpoint):
    with pytest.raises(ValueError):
        module._connection_options(endpoint, "localhost")


def test_readable_http_200_without_domcontentloaded_is_accepted(monkeypatch):
    socket = Socket()
    socket.emit_dcl = False
    install(monkeypatch, socket)
    result = asyncio.run(module.read_cdp(runtime(), URL, search=True))
    assert result["status_code"] == 200 and result["search_document"]["verified"]
    assert result["search_results"] and result["browser_cleanup"]["closed"]
