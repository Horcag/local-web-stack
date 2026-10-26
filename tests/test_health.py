from __future__ import annotations

import asyncio
import struct

import pytest

from service import health


@pytest.mark.parametrize("failure", ["exception", "timeout"])
def test_dns_probe_sanitizes_failures_and_deadlines(monkeypatch, failure):
    async def run():
        async def fail(*_args, **_kwargs):
            if failure == "timeout":
                await asyncio.sleep(1)
            raise OSError("private resolver details")

        monkeypatch.setattr(health, "_DNS_TIMEOUT_SECONDS", 0.01)
        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", fail)
        result = await health.probe_dns()
        assert result == {"status": "unhealthy", "error": "dns_unavailable"}
        assert "private" not in str(result)

    asyncio.run(run())


def test_socks_probe_sends_remote_dns_connect_and_consumes_domain_reply(monkeypatch):
    async def run():
        captured = {}
        closed = asyncio.Event()

        async def proxy(reader, writer):
            try:
                captured["greeting"] = await reader.readexactly(3)
                writer.write(b"\x05\x00")
                await writer.drain()
                captured["connect"] = await reader.readexactly(5)
                size = captured["connect"][-1]
                captured["host"] = await reader.readexactly(size)
                captured["port"] = await reader.readexactly(2)
                writer.write(b"\x05\x00\x00\x03\x03ip1\x23\x45")
                await writer.drain()
                await reader.read()
            finally:
                writer.close()
                await writer.wait_closed()
                closed.set()

        server = await asyncio.start_server(proxy, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setenv("CRAWL4AI_DEFAULT_PROXY", f"socks5://127.0.0.1:{port}")
        monkeypatch.setenv("LOCAL_WEB_HEALTH_DNS_HOST", "canary.example")
        try:
            result = await health.probe_proxy()
            assert result == {"status": "healthy"}
            await asyncio.wait_for(closed.wait(), 1)
            assert captured["greeting"] == b"\x05\x01\x00"
            assert captured["connect"] == b"\x05\x01\x00\x03\x0e"
            assert captured["host"] == b"canary.example"
            assert captured["port"] == struct.pack(">H", 443)
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_listener_without_socks_reply_is_unhealthy_and_connection_closes(monkeypatch):
    monkeypatch.setattr(health, "_NETWORK_TIMEOUT_SECONDS", 0.05)

    async def run():
        accepted = asyncio.Event()

        async def listener(reader, writer):
            accepted.set()
            await reader.read()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(listener, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setenv("CRAWL4AI_DEFAULT_PROXY", f"socks5://127.0.0.1:{port}")
        try:
            assert await health.probe_proxy() == {
                "status": "unhealthy",
                "error": "proxy_unavailable",
            }
            await asyncio.wait_for(accepted.wait(), 1)
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_dns_success_uses_real_socket_kind(monkeypatch):
    async def run():
        captured = []

        async def resolver(*args, **kwargs):
            captured.append(kwargs["type"])
            return [(2, 1, 6, "", ("93.184.216.34", 443))]

        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolver)
        assert await health.probe_dns() == {"status": "healthy"}
        assert captured == [health.socket.SOCK_STREAM]

    asyncio.run(run())


def test_socks_failure_reply_is_sanitized(monkeypatch):
    async def run():
        async def proxy(reader, writer):
            await reader.readexactly(3)
            writer.write(b"\x05\x00")
            await writer.drain()
            await reader.readexactly(5)
            writer.write(b"\x05\x05\x00\x01\x7f\x00\x00\x01\x00\x50")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(proxy, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setenv("CRAWL4AI_DEFAULT_PROXY", f"socks5://127.0.0.1:{port}")
        try:
            assert await health.probe_proxy() == {
                "status": "unhealthy",
                "error": "proxy_unavailable",
            }
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_readiness_probes_independently_and_reports_degraded(monkeypatch):
    async def run():
        async def dns():
            return {"status": "healthy"}

        async def proxy():
            return {"status": "unhealthy", "error": "proxy_unavailable"}

        monkeypatch.setattr(health, "probe_dns", dns)
        monkeypatch.setattr(health, "probe_proxy", proxy)
        assert await health.readiness() == {
            "status": "degraded",
            "service": "local-crawl4ai",
            "checks": {
                "dns": {"status": "healthy"},
                "proxy": {"status": "unhealthy", "error": "proxy_unavailable"},
            },
        }

    asyncio.run(run())


def test_blank_proxy_is_not_required_and_readiness_works_without_it(monkeypatch):
    async def run():
        monkeypatch.delenv("CRAWL4AI_DEFAULT_PROXY", raising=False)
        monkeypatch.setattr(
            health, "probe_dns", lambda: asyncio.sleep(0, result={"status": "healthy"})
        )
        assert await health.probe_proxy() == {"status": "healthy", "mode": "not_required"}
        result = await health.readiness()
        assert result["status"] == "healthy"

    asyncio.run(run())
