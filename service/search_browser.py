"""Bounded Yandex SERP extraction through the configured, borrowed browser."""

from __future__ import annotations

import asyncio
import ipaddress
import unicodedata
from typing import Protocol
from urllib.parse import parse_qs, urlencode, urlsplit

from service.research.content import is_captcha_gate


_DEADLINE_SECONDS = 30
_SEARCH_HOSTS = {"yandex.ru", "www.yandex.ru", "yandex.com", "www.yandex.com"}
_LANGUAGES = {"ru", "en", "fr"}
_PROVIDER_DOMAINS = ("yandex.ru", "yandex.com", "yandex.net", "ya.ru")


def supports_language(language: str) -> bool:
    return language in {"auto", "all", "", *_LANGUAGES}


class SearchBrowser(Protocol):
    @property
    def configured(self) -> bool: ...

    async def read_search(
        self, url: str, *, allow_captcha_recovery: bool = False
    ) -> dict | None: ...


def _query_identity(query: str) -> str:
    return " ".join(unicodedata.normalize("NFC", query).split())


def _public_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if (
            parsed.scheme not in {"http", "https"}
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 80, 443}
            or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url)
            or host.endswith((".localhost", ".local", ".internal", ".test", ".invalid"))
            or host == "localhost"
            or "." not in host
        ):
            return False
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            # Reject abbreviated and integer IPv4 spellings rather than resolving them.
            return not all(char.isdigit() or char == "." for char in host)
        return address.is_global
    except ValueError:
        return False


def _provider_url(url: str) -> bool:
    host = urlsplit(url).hostname or ""
    return any(host == domain or host.endswith("." + domain) for domain in _PROVIDER_DOMAINS)


def _result_target(href: str) -> tuple[str, str] | None:
    if not _public_url(href):
        return None
    parsed = urlsplit(href)
    target, method = href, "direct"
    # Decode only the known Yandex redirect endpoint, once. Never follow it.
    if parsed.hostname in _SEARCH_HOSTS and parsed.path == "/clck/jsredir":
        parameters = parse_qs(parsed.query, keep_blank_values=True)
        targets = parameters.get("url", [])
        if len(targets) != 1:
            return None
        target, method = targets[0], "yandex_jsredir_url"
    if not _public_url(target) or _provider_url(target):
        return None
    return target, method


def _search_identity(url: str, query: str, language: str, page: int) -> dict | None:
    if not _public_url(url):
        return None
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _SEARCH_HOSTS
        or parsed.port not in {None, 443}
        or parsed.path not in {"/search", "/search/"}
        or parsed.fragment
    ):
        return None
    parameters = parse_qs(parsed.query, keep_blank_values=True)
    texts = parameters.get("text", [])
    if (
        len(texts) != 1
        or _query_identity(texts[0]) != _query_identity(query)
        or set(parameters) - {"text", "lang", "lr", "p"}
        or any(len(values) != 1 for values in parameters.values())
        or parameters.get("lang", []) != ([language] if language else [])
        or parameters.get("p", []) != ([str(page - 1)] if page > 1 else [])
    ):
        return None
    return {
        "query": texts[0],
        "parameters": parameters,
        "host": parsed.hostname,
        "path": parsed.path,
    }


def _extract(snapshot: dict, receipt: dict, query: str, language: str, page: int) -> list[dict]:
    actual = snapshot.get("url")
    receipt.update(
        actual_url=actual,
        status_code=snapshot.get("status_code"),
        method=snapshot.get("method"),
        captcha=snapshot.get("captcha"),
        truncated=snapshot.get("truncated"),
        search_document=snapshot.get("search_document"),
    )
    identity = _search_identity(actual, query, language, page) if isinstance(actual, str) else None
    if identity is None:
        receipt["reason"] = "search_target_unverified"
        return []
    receipt.update(actual_query=identity["query"], search_identity=identity)
    status = snapshot.get("status_code")
    if type(status) is not int or not 200 <= status < 300:
        receipt["reason"] = "http_status_unverified"
        return []
    body, title, links, captcha = (
        snapshot.get("markdown"),
        snapshot.get("title"),
        snapshot.get("links"),
        snapshot.get("captcha"),
    )
    if (
        not isinstance(body, str)
        or not isinstance(title, str)
        or not isinstance(links, list)
        or not isinstance(captcha, dict)
        or snapshot.get("truncated") is not False
        or snapshot.get("method") != "cdp-visible-text"
    ):
        receipt["reason"] = "browser_content_unverified"
        return []
    receipt["links"] = links
    cards = snapshot.get("search_results")
    document = snapshot.get("search_document")
    if (
        not isinstance(cards, list)
        or not isinstance(document, dict)
        or document.get("verified") is not True
    ):
        receipt["reason"] = "organic_results_unverified"
        return []
    heading = next((line.strip(" #").lower() for line in body.splitlines() if line.strip()), "")
    if (
        captcha.get("status") != "not_attempted"
        or captcha.get("attempts") != 0
        or captcha.get("challenge_observed")
        or is_captcha_gate(body, title)
        or title.strip().lower()
        in {"privacy error", "your connection is not private", "access denied"}
        or heading in {"privacy error", "your connection is not private", "access denied"}
        or not body.strip()
    ):
        receipt["reason"] = "content_gate_or_privacy_error"
        return []
    results = []
    for index, link in enumerate(cards[:150]):
        if not isinstance(link, dict):
            continue
        href, text = link.get("href"), link.get("title")
        if link.get("source") != "yandex-organic-card":
            continue
        if not isinstance(href, str) or not isinstance(text, str) or not text.strip():
            continue
        target = _result_target(href)
        if target is None:
            continue
        results.append(
            {
                "title": text.strip(),
                "url": target[0],
                "content": "",
                "engine": "yandex",
                "provenance": {
                    "source": "yandex-organic-card",
                    "selector": link.get("selector"),
                    "card_text": link.get("card_text"),
                    "search_url": actual,
                    "query": query,
                    "link_index": index,
                    "href": href,
                    "text": text,
                    "target_method": target[1],
                },
            }
        )
    receipt.update(
        status="accepted" if results else "rejected", reason="" if results else "no_candidates"
    )
    return results


async def browser_search(
    runtime: SearchBrowser, query: str, language: str = "all", page: int = 1
) -> dict:
    """Read one public SERP, including lock wait in the overall deadline."""
    language = "" if language in {"auto", "all", ""} else language
    receipt: dict = {
        "provider": "yandex",
        "status": "rejected",
        "reason": "",
        "requested_query": query,
        "language": language or "all",
        "page": page,
        "deadline_seconds": _DEADLINE_SECONDS,
    }
    response = {"results": [], "receipt": receipt}
    if (
        not _query_identity(query)
        or language
        and language not in _LANGUAGES
        or type(page) is not int
        or page < 1
    ):
        receipt["reason"] = "unsupported_query_or_language"
        return response
    parameters = {"text": query}
    if language:
        parameters["lang"] = language
    if page > 1:
        parameters["p"] = str(page - 1)
    requested = "https://yandex.ru/search/?" + urlencode(parameters)
    receipt["requested_url"] = requested
    if not runtime.configured:
        receipt["reason"] = "browser_unconfigured"
        return response
    try:
        async with asyncio.timeout(_DEADLINE_SECONDS):
            snapshot = await runtime.read_search(requested, allow_captcha_recovery=False)
            if isinstance(snapshot, dict):
                response["results"] = _extract(snapshot, receipt, query, language, page)
            else:
                receipt["reason"] = "browser_unavailable"
    except TimeoutError:
        receipt["reason"] = "browser_timeout"
    except Exception as exc:
        receipt.update(reason="browser_failed", error_type=type(exc).__name__)
    return response
