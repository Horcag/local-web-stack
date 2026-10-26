"""Protected websocket handshake behavior with local deterministic endpoints."""

import asyncio

import pytest

from service import cdp_reader as module


def test_real_websocket_redirect_handshake_never_contacts_other_target():
    from websockets.exceptions import InvalidStatus

    async def scenario():
        calls = {"configured": 0, "other": 0}

        async def other(reader, writer):
            calls["other"] += 1
            writer.close()
            await writer.wait_closed()

        other_server = await asyncio.start_server(other, "127.0.0.1", 0)
        other_port = other_server.sockets[0].getsockname()[1]

        async def configured(reader, writer):
            calls["configured"] += 1
            await reader.readuntil(b"\r\n\r\n")
            response = (
                "HTTP/1.1 302 Found\r\n"
                f"Location: ws://127.0.0.1:{other_port}/different-target\r\n"
                "Content-Length: 0\r\nConnection: close\r\n\r\n"
            )
            writer.write(response.encode())
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        configured_server = await asyncio.start_server(configured, "127.0.0.1", 0)
        port = configured_server.sockets[0].getsockname()[1]
        try:
            with pytest.raises(InvalidStatus):
                async with module.connect(
                    f"ws://127.0.0.1:{port}/configured", proxy=None, open_timeout=1
                ):
                    pytest.fail("Redirect handshake accepted")
            assert calls == {"configured": 1, "other": 0}
        finally:
            configured_server.close()
            other_server.close()
            await configured_server.wait_closed()
            await other_server.wait_closed()

    asyncio.run(scenario())
