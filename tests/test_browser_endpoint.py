"""CDP discovery stays bound to the configured broker with no real network I/O."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

from service import browser


@pytest.fixture
def broker(monkeypatch):
    monkeypatch.delenv("LOCAL_WEB_BROWSER_CDP_HOST_HEADER", raising=False)
    requests, options = [], []
    state = {"body": {"webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/id?token=a"}}
    real_client = httpx.AsyncClient

    def handle(request):
        requests.append(request)
        if "error" in state:
            raise state["error"]
        body = state["body"]
        content = json.dumps(body).encode() if not isinstance(body, bytes) else body
        return httpx.Response(state.get("status", 200), content=content)

    def client(**kwargs):
        options.append(kwargs)
        return real_client(**kwargs, transport=httpx.MockTransport(handle))

    monkeypatch.setattr(browser.httpx, "AsyncClient", client)
    return state, requests, options


def resolve(endpoint):
    return asyncio.run(browser._cdp_endpoint(endpoint))


@pytest.mark.parametrize("scheme, websocket", [("http", "ws"), ("https", "wss")])
@pytest.mark.parametrize("advertised_host", ["127.0.0.1", "127.0.0.2", "localhost", "[::1]"])
def test_loopback_advertisement_uses_only_configured_authority(
    broker, scheme, websocket, advertised_host
):
    state, requests, options = broker
    state["body"] = {
        "webSocketDebuggerUrl": f"ws://{advertised_host}:9222/devtools/browser/id?token=a"
    }
    endpoint = f"{scheme}://configured-broker:9226"
    assert resolve(endpoint) == f"{websocket}://configured-broker:9226/devtools/browser/id?token=a"
    assert len(requests) == 1
    assert str(requests[0].url) == endpoint + "/json/version"
    assert requests[0].headers["Accept-Encoding"] == "identity"
    assert options == [{"timeout": 5, "trust_env": False}]


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://localhost:9222",
        "https://127.0.0.1:9222",
        "http://[::1]:9222",
        "ws://remote:9222/devtools/browser/id",
        "wss://remote/devtools/browser/id?token=a",
    ],
)
def test_native_loopback_http_and_explicit_websocket_are_unchanged(broker, endpoint):
    _, requests, _ = broker
    assert resolve(endpoint) == endpoint
    assert not requests


@pytest.mark.parametrize(
    "host",
    [
        "remote-browser",
        "localhost.evil.test",
        "127.0.0.1.evil.test",
        "192.168.1.20",
        "[2001:db8::1]",
    ],
)
def test_nonloopback_advertisement_does_not_change_configured_endpoint(broker, host):
    state, requests, _ = broker
    state["body"] = {"webSocketDebuggerUrl": f"wss://{host}/devtools/browser/id"}
    assert resolve("https://configured-broker:9226") == "https://configured-broker:9226"
    assert len(requests) == 1 and requests[0].url.host == "configured-broker"


def test_configured_path_prefix_and_discovery_query_preserved(broker):
    _, requests, _ = broker
    assert (
        resolve("http://configured-broker:9226/relay/?key=private")
        == "ws://configured-broker:9226/devtools/browser/id?token=a"
    )
    assert str(requests[0].url) == "http://configured-broker:9226/relay/json/version?key=private"


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {},
        {"webSocketDebuggerUrl": 1},
        b"not json",
        b"\xff",
        {"webSocketDebuggerUrl": "http://localhost/devtools/browser/id"},
        {"webSocketDebuggerUrl": "ws://user:secret@localhost/devtools/browser/id"},
        {"webSocketDebuggerUrl": "ws://localhost/devtools/browser/id#fragment"},
        {"webSocketDebuggerUrl": "ws://localhost/devtools/page/id"},
        {"webSocketDebuggerUrl": "ws://localhost/devtools/browser/"},
        {"webSocketDebuggerUrl": "ws://localhost:bad/devtools/browser/id"},
        {"webSocketDebuggerUrl": "ws://[broken/devtools/browser/id"},
        {"webSocketDebuggerUrl": "ws:///devtools/browser/id"},
    ],
)
def test_malformed_broker_responses_fail_without_endpoint_leak(broker, value):
    state, requests, _ = broker
    state["body"] = value
    with pytest.raises(HTTPException) as exc:
        resolve("http://configured-broker:9226")
    assert exc.value.status_code == 503
    assert "secret" not in exc.value.detail and "configured-broker" not in exc.value.detail
    assert len(requests) == 1


@pytest.mark.parametrize("status", [302, 401, 500])
def test_redirect_and_failed_discovery_are_not_followed(broker, status):
    state, requests, _ = broker
    state["status"] = status
    with pytest.raises(HTTPException):
        resolve("http://configured-broker:9226")
    assert len(requests) == 1


@pytest.mark.parametrize("error", [httpx.ReadTimeout("timeout"), httpx.ConnectError("unreachable")])
def test_discovery_failure_is_bounded_and_sanitized(broker, error):
    state, _, _ = broker
    state["error"] = error
    with pytest.raises(
        HTTPException, match="Configured CDP broker returned no valid browser endpoint"
    ):
        resolve("http://configured-broker:9226")


def test_exact_response_limit_allowed_and_one_more_byte_rejected(broker):
    state, _, _ = broker
    payload = json.dumps(state["body"]).encode()
    state["body"] = payload + b" " * (16 * 1024 - len(payload))
    assert resolve("http://configured-broker:9226").startswith("ws://configured-broker:9226/")
    state["body"] += b" "
    with pytest.raises(HTTPException):
        resolve("http://configured-broker:9226")


@pytest.mark.parametrize(
    "endpoint",
    [
        "ftp://configured-broker",
        "http://user:secret@configured-broker",
        "http://configured-broker#fragment",
        "http://configured-broker:invalid",
        "http:///missing-host",
    ],
)
def test_invalid_configured_http_endpoint_never_requests_network(broker, endpoint):
    _, requests, _ = broker
    with pytest.raises(HTTPException):
        resolve(endpoint)
    assert not requests


def test_connect_uses_resolved_endpoint_and_preserves_no_defaults(broker, monkeypatch):
    import playwright.async_api

    borrowed = AsyncMock()
    driver = SimpleNamespace(
        chromium=SimpleNamespace(connect_over_cdp=AsyncMock(return_value=borrowed)),
        stop=AsyncMock(),
    )
    monkeypatch.setattr(
        playwright.async_api,
        "async_playwright",
        lambda: SimpleNamespace(start=AsyncMock(return_value=driver)),
    )
    subject = browser.BrowserRuntime("http://configured-broker:9226")
    asyncio.run(subject._connect())
    driver.chromium.connect_over_cdp.assert_awaited_once_with(
        "ws://configured-broker:9226/devtools/browser/id?token=a", timeout=15000, no_defaults=True
    )
    asyncio.run(subject.close())
    borrowed.close.assert_not_awaited()


def test_failed_discovery_stops_driver_without_attempting_browser_connection(broker, monkeypatch):
    import playwright.async_api

    broker[0]["body"] = b"malformed"
    driver = SimpleNamespace(
        chromium=SimpleNamespace(connect_over_cdp=AsyncMock()), stop=AsyncMock()
    )
    monkeypatch.setattr(
        playwright.async_api,
        "async_playwright",
        lambda: SimpleNamespace(start=AsyncMock(return_value=driver)),
    )
    subject = browser.BrowserRuntime("http://configured-broker:9226")
    with pytest.raises(HTTPException):
        asyncio.run(subject._connect())
    driver.chromium.connect_over_cdp.assert_not_awaited()
    driver.stop.assert_awaited_once()


@pytest.mark.parametrize(
    "advertised",
    [
        "ws://localhost/devtools/browser/id#",
        "ws://localhost/devtools/browser/id/../page",
        "ws://localhost/devtools/browser/id/nested",
    ],
)
def test_fragment_or_non_browser_websocket_path_is_rejected(broker, advertised):
    broker[0]["body"] = {"webSocketDebuggerUrl": advertised}
    with pytest.raises(HTTPException):
        resolve("http://configured-broker:9226")


def test_default_discovery_host_is_the_configured_authority(broker):
    resolve("http://configured-broker:9226")
    assert broker[1][0].headers["Host"] == "configured-broker:9226"


def test_opt_in_host_override_is_shared_by_discovery_and_playwright(broker, monkeypatch):
    import playwright.async_api

    monkeypatch.setenv("LOCAL_WEB_BROWSER_CDP_HOST_HEADER", "localhost")
    driver = SimpleNamespace(
        chromium=SimpleNamespace(connect_over_cdp=AsyncMock()), stop=AsyncMock()
    )
    monkeypatch.setattr(
        playwright.async_api,
        "async_playwright",
        lambda: SimpleNamespace(start=AsyncMock(return_value=driver)),
    )
    subject = browser.BrowserRuntime("http://configured-broker:9226")
    asyncio.run(subject._connect())
    assert broker[1][0].headers["Host"] == "localhost"
    driver.chromium.connect_over_cdp.assert_awaited_once_with(
        "ws://configured-broker:9226/devtools/browser/id?token=a",
        timeout=15000,
        no_defaults=True,
        headers={"Host": "localhost"},
    )
    asyncio.run(subject.close())


@pytest.mark.parametrize(
    "header",
    [
        "localhost\r\nInjected: value",
        "user:secret@localhost",
        "localhost/path",
        "localhost?query=1",
        "localhost#",
        "localhost:invalid",
        "localhost with-space",
    ],
)
def test_invalid_host_override_is_rejected_without_network(broker, header):
    with pytest.raises(HTTPException, match="Invalid configured CDP Host header"):
        asyncio.run(browser._cdp_endpoint("http://configured-broker:9226", header))
    assert not broker[1]


@pytest.mark.parametrize(
    "advertised",
    [
        "\nws://localhost/devtools/browser/id",
        "ws://localhost/devtools/browser/id\t",
        "ws://localhost/devtools/browser/id?token=with space",
    ],
)
def test_websocket_url_control_or_whitespace_is_not_normalized(broker, advertised):
    broker[0]["body"] = {"webSocketDebuggerUrl": advertised}
    with pytest.raises(HTTPException):
        resolve("http://configured-broker:9226")


def test_cancelled_discovery_stops_own_driver_and_propagates(broker, monkeypatch):
    import playwright.async_api

    broker[0]["error"] = asyncio.CancelledError()
    driver = SimpleNamespace(
        chromium=SimpleNamespace(connect_over_cdp=AsyncMock()), stop=AsyncMock()
    )
    monkeypatch.setattr(
        playwright.async_api,
        "async_playwright",
        lambda: SimpleNamespace(start=AsyncMock(return_value=driver)),
    )
    subject = browser.BrowserRuntime("http://configured-broker:9226")
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(subject._connect())
    driver.chromium.connect_over_cdp.assert_not_awaited()
    driver.stop.assert_awaited_once()
    assert subject._driver is None
