"""Offline contracts for the bounded browser SERP adapter."""

import asyncio
from urllib.parse import parse_qs, quote, urlsplit

import pytest

from service import search_browser as search


QUERY = 'site:docs.python.org "asyncio timeout"'


class Runtime:
    configured = True

    def __init__(self, **updates):
        self.updates = updates
        self.calls = []

    async def read_search(self, url, *, allow_captcha_recovery):
        self.calls.append((url, allow_captcha_recovery))
        return {
            "url": url,
            "title": "Search results",
            "markdown": "Search results for asyncio timeout\nPython documentation",
            "links": [
                {"href": "https://docs.python.org/3/library/asyncio-task.html", "text": "Timeout"}
            ],
            "search_results": [
                {
                    "href": "https://docs.python.org/3/library/asyncio-task.html",
                    "title": "Timeout",
                    "source": "yandex-organic-card",
                    "selector": ".OrganicTitle-Link",
                }
            ],
            "search_document": {"verified": True, "document_id": "loader", "revision": 1},
            "status_code": 200,
            "method": "cdp-visible-text",
            "captcha": {"status": "not_attempted", "attempts": 0},
            "truncated": False,
        } | self.updates


def run(runtime=None, query=QUERY, language="all"):
    return asyncio.run(search.browser_search(runtime or Runtime(), query, language))


@pytest.mark.parametrize("language", ["auto", "all", "", "ru", "en", "fr"])
def test_query_language_and_original_link_provenance(language):
    runtime = Runtime()
    result = run(runtime, language=language)
    requested, recovery = runtime.calls[0]
    assert len(runtime.calls) == 1 and recovery is False
    assert parse_qs(urlsplit(requested).query) == {
        "text": [QUERY],
        **({"lang": [language]} if language in {"ru", "en", "fr"} else {}),
    }
    assert result["receipt"]["status"] == "accepted"
    assert result["receipt"]["actual_query"] == QUERY
    candidate = result["results"][0]
    assert candidate["content"] == ""
    assert candidate["provenance"]["href"] == candidate["url"]
    assert candidate["provenance"]["text"] == "Timeout"
    assert candidate["provenance"]["query"] == QUERY
    assert result["receipt"]["links"] == [{"href": candidate["url"], "text": "Timeout"}]


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.org/search/?text=x",
        "https://yandex.ru.evil.org/search/?text=x",
        "https://yandex.ru/showcaptcha?text=x",
        "http://yandex.ru/search/?text=x",
        "https://user@yandex.ru/search/?text=x",
        "https://yandex.ru:8443/search/?text=x",
        "https://yandex.ru/search/?text=other",
        "https://yandex.ru/search/?text=x&text=x",
        "https://yandex.ru/search/?text=x&p=1",
        "https://yandex.ru/search/?text=x#changed",
        "https://yandex.ru/search/?text=x&lang=en",
    ],
)
def test_changed_search_identity_is_rejected(url):
    result = run(Runtime(url=url), query="x")
    assert result["results"] == []
    assert result["receipt"]["reason"] == "search_target_unverified"


def test_normalized_query_and_server_region():
    result = run(Runtime(url="https://www.yandex.ru/search?text=a++b&lr=213"), query="a b")
    assert result["receipt"]["status"] == "accepted"
    assert result["receipt"]["search_identity"]["parameters"]["lr"] == ["213"]


@pytest.mark.parametrize(
    "updates,reason",
    [
        ({"status_code": None}, "http_status_unverified"),
        ({"status_code": True}, "http_status_unverified"),
        ({"status_code": 302}, "http_status_unverified"),
        ({"status_code": 403}, "http_status_unverified"),
        ({"truncated": True}, "browser_content_unverified"),
        ({"truncated": None}, "browser_content_unverified"),
        ({"method": "import"}, "browser_content_unverified"),
        ({"links": None}, "browser_content_unverified"),
        ({"captcha": None}, "browser_content_unverified"),
        ({"captcha": {"status": "solved", "attempts": 1}}, "content_gate_or_privacy_error"),
        (
            {"captcha": {"status": "not_attempted", "attempts": 0, "challenge_observed": True}},
            "content_gate_or_privacy_error",
        ),
        ({"title": "Verify you are human"}, "content_gate_or_privacy_error"),
        ({"markdown": "Подтвердите, что вы не робот"}, "content_gate_or_privacy_error"),
        ({"title": "Privacy error"}, "content_gate_or_privacy_error"),
        ({"markdown": "# Your connection is not private"}, "content_gate_or_privacy_error"),
        ({"markdown": ""}, "content_gate_or_privacy_error"),
    ],
)
def test_unverified_document_is_rejected(updates, reason):
    result = run(Runtime(**updates))
    assert result["results"] == []
    assert result["receipt"]["reason"] == reason


def test_only_known_provider_redirect_is_decoded_and_no_candidates_are_loaded(monkeypatch):
    monkeypatch.setenv("LOCAL_WEB_RESEARCH_ALLOW_PRIVATE", "true")
    target = "https://docs.python.org/3/?a=1&b=2"
    redirect = "https://yandex.ru/clck/jsredir?url=" + quote(target, safe="")
    rejected = [
        "https://maps.yandex.ru/",
        "https://yandex.ru/search/?text=other",
        "https://yandex.ru/clck/jsredir?url=" + quote("http://127.0.0.1/private", safe=""),
        "https://yandex.ru/clck/jsredir?url=" + quote("https://maps.yandex.ru/", safe=""),
        "https://yandex.ru/other?url=" + quote(target, safe=""),
        "http://localhost/",
        "http://10.0.0.1/",
        "http://2130706433/",
        "http://127.1/",
        "http://host.internal/",
        "https://name:password@example.org/",
        "javascript:alert(1)",
    ]
    links = [{"href": href, "text": "UI"} for href in rejected]
    links += [{"href": redirect, "text": "Python docs"}]
    links += [{"href": "https://other.org/path?url=" + quote(target, safe=""), "text": "Other"}]
    cards = [
        {"href": link["href"], "title": link["text"], "source": "yandex-organic-card"}
        for link in links
    ]
    runtime = Runtime(links=links, search_results=cards)
    result = run(runtime)
    assert [item["url"] for item in result["results"]] == [target, links[-1]["href"]]
    assert result["results"][0]["provenance"]["href"] == redirect
    assert result["results"][0]["provenance"]["target_method"] == "yandex_jsredir_url"
    assert len(runtime.calls) == 1


def test_invalid_links_and_empty_candidates():
    result = run(
        Runtime(
            search_results=[
                None,
                {},
                {"href": "https://docs.python.org", "title": " ", "source": "yandex-organic-card"},
            ]
        )
    )
    assert result["results"] == []
    assert result["receipt"]["reason"] == "no_candidates"


def test_configuration_and_language_fail_before_browser():
    runtime = Runtime()
    runtime.configured = False
    assert run(runtime)["receipt"]["reason"] == "browser_unconfigured"
    runtime.configured = True
    assert run(runtime, language="de")["receipt"]["reason"] == "unsupported_query_or_language"
    assert run(runtime, query="  ")["receipt"]["reason"] == "unsupported_query_or_language"
    assert runtime.calls == []


def test_timeout_covers_waiting_for_runtime_lock(monkeypatch):
    monkeypatch.setattr(search, "_DEADLINE_SECONDS", 0.01)

    class Waiting(Runtime):
        async def read_search(self, url, *, allow_captcha_recovery):
            async with asyncio.Lock():
                await asyncio.Event().wait()

    assert run(Waiting())["receipt"]["reason"] == "browser_timeout"


def test_browser_error_does_not_expose_exception_text():
    class Failed(Runtime):
        async def read_search(self, url, *, allow_captcha_recovery):
            raise ValueError("private browser content")

    result = run(Failed())
    assert result["receipt"]["reason"] == "browser_failed"
    assert result["receipt"]["error_type"] == "ValueError"
    assert "private browser content" not in str(result)


def test_requested_page_is_preserved_and_redirect_page_rejected():
    runtime = Runtime()
    result = asyncio.run(search.browser_search(runtime, QUERY, page=3))
    assert parse_qs(urlsplit(runtime.calls[0][0]).query)["p"] == ["2"]
    assert result["receipt"]["status"] == "accepted"
    result = asyncio.run(
        search.browser_search(Runtime(url="https://yandex.ru/search/?text=x"), "x", page=2)
    )
    assert result["receipt"]["reason"] == "search_target_unverified"


@pytest.mark.parametrize("page", [0, -1, True, 1.5])
def test_invalid_page_fails_before_browser(page):
    runtime = Runtime()
    result = asyncio.run(search.browser_search(runtime, QUERY, page=page))
    assert result["receipt"]["reason"] == "unsupported_query_or_language"
    assert runtime.calls == []


def test_generic_external_links_do_not_replace_organic_card_evidence():
    result = run(Runtime(search_results=None))
    assert result["results"] == []
    assert result["receipt"]["reason"] == "organic_results_unverified"
    result = run(Runtime(search_results=[{"href": "https://docs.python.org/", "title": "Python"}]))
    assert result["results"] == []
    assert result["receipt"]["reason"] == "no_candidates"


def test_unavailable_snapshot():
    class Unavailable(Runtime):
        async def read_search(self, url, *, allow_captcha_recovery):
            return None

    assert run(Unavailable())["receipt"]["reason"] == "browser_unavailable"


def test_cancellation_is_propagated():
    class Cancelled(Runtime):
        async def read_search(self, url, *, allow_captcha_recovery):
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        run(Cancelled())


def test_missing_document_binding_is_rejected():
    result = run(Runtime(search_document=None))
    assert result["receipt"]["reason"] == "organic_results_unverified"
    assert result["results"] == []
