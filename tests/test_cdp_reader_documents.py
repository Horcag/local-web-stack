"""Malformed-frame rejection and SERP rendering readiness contracts."""

import asyncio

import pytest

from service import cdp_reader as module
from test_cdp_reader import Socket, install, runtime

URL = "https://yandex.ru/search/?text=example"


@pytest.mark.parametrize("frame_id", [None, "", " ", 0, False])
def test_invalid_main_frame_identity_fails_closed_and_closes_owned_target(monkeypatch, frame_id):
    socket = Socket()
    socket.frame_id = frame_id
    install(monkeypatch, socket)
    with pytest.raises(ValueError, match="frame identity unverified"):
        asyncio.run(module.read_cdp(runtime(), URL, search=True))
    assert "Page.navigate" not in [command["method"] for command in socket.commands]
    assert socket.commands[-1]["method"] == "Target.closeTarget"


def test_search_readiness_waits_through_body_shell_until_organic_cards(monkeypatch):
    socket = Socket()
    socket.emit_dcl = False
    socket.readiness_empty_checks = 2
    install(monkeypatch, socket)
    result = asyncio.run(module.read_cdp(runtime(), URL, search=True))
    assert socket.readiness_checks == 3
    assert result["status_code"] == 200 and result["search_results"]


@pytest.mark.parametrize("frame_call", [2, 3])
def test_malformed_frame_identity_before_or_after_snapshot_rejects_evidence(
    monkeypatch, frame_call
):
    class MalformedSnapshot(Socket):
        async def send(self, raw):
            import json

            if json.loads(raw)["method"] == "Page.getFrameTree":
                previous = sum(
                    command["method"] == "Page.getFrameTree" for command in self.commands
                )
                if previous + 1 == frame_call:
                    self.frame_id = None
            await super().send(raw)

    socket = MalformedSnapshot()
    install(monkeypatch, socket)
    result = asyncio.run(module.read_cdp(runtime(), URL, search=True))
    assert result["status_code"] is None and result["search_results"] == []
    assert result["search_document"]["verified"] is False
    assert result["browser_cleanup"]["closed"] is True
