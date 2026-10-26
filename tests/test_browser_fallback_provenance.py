"""Readers report actual acquisition and keep the original CAPTCHA as history."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from service.research.runner import Runner
from test_mcp_research_tools import module


def payload():
    return {
        "markdown": "Verified source evidence. " * 50,
        "title": "Primary source",
        "source": "browser-fallback",
        "method": "cdp-visible-text",
        "status_code": 200,
        "initial_status_code": 403,
        "assessment": {"status": "read", "reasons": []},
        "captcha": {"status": "not_attempted", "attempts": 0},
        "initial_captcha": {"status": "deadline", "attempts": 1, "challenge_observed": True},
        "browser_fallback": {
            "status": "accepted",
            "url": "https://source.test/article",
            "captcha_recovery_allowed": False,
        },
        "links": [{"href": "https://source.test/document", "text": "Document"}],
    }


def test_mcp_preserves_active_and_historical_reader_provenance():
    expected = payload()
    real_client = httpx.AsyncClient
    with patch.object(
        module.httpx,
        "AsyncClient",
        side_effect=lambda **kwargs: real_client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=expected)),
            **kwargs,
        ),
    ):
        actual = asyncio.run(module.read_url("https://source.test/article"))
    for field in (
        "source",
        "method",
        "status_code",
        "initial_status_code",
        "captcha",
        "initial_captcha",
        "browser_fallback",
        "links",
    ):
        assert actual[field] == expected[field]


def test_research_records_actual_browser_method_without_reblocking_historical_gate():
    original = {"kind": "search", "query": "source evidence"}
    source = {
        "title": "Source",
        "url": "https://source.test/article",
        "run_id": "run",
        "provenance": [original],
    }
    actual = asyncio.run(Runner(SimpleNamespace()).assess(source, payload(), "crawl4ai"))
    assert actual["status"] == "read" and actual["method"] == "cdp-visible-text"
    assert actual["provenance"][0] == original
    assert actual["provenance"][1]["url"] == source["url"]
    assert actual["provenance"][1]["initial_captcha"]["status"] == "deadline"


@pytest.mark.parametrize("receipt", [{"status": "rejected"}, {}, None, "invalid"])
def test_rejected_or_invalid_fallback_receipt_never_repeats_read_or_budget(receipt):
    source = {"title": "Source", "url": "https://source.test/article", "run_id": "run"}
    store = SimpleNamespace(reserve=Mock(return_value=True))
    reader = AsyncMock(return_value=payload())
    blocked = {
        "markdown": "Please confirm that you are not a robot",
        "title": "Are you not a robot?",
        "status_code": 403,
        "captcha": {"status": "deadline", "attempts": 2, "challenge_observed": True},
        "browser_fallback": receipt,
    }
    actual = asyncio.run(Runner(store, reader).assess(source, blocked, "crawl4ai"))
    assert actual["status"] == "browser_pending"
    assert actual["assessment_status"] == "blocked"
    reader.assert_not_awaited()
    store.reserve.assert_not_called()
