"""Bounded, dependency-free readiness probes for crawl networking."""

from __future__ import annotations

import asyncio
import os
import socket
import struct
from typing import Any
from urllib.parse import urlsplit

_DNS_TIMEOUT_SECONDS = 3.0
_NETWORK_TIMEOUT_SECONDS = 5.0
_DEFAULT_CANARY = "example.com"


def _canary_host() -> str:
    return os.environ.get("LOCAL_WEB_HEALTH_DNS_HOST", _DEFAULT_CANARY).strip() or _DEFAULT_CANARY


async def probe_dns() -> dict[str, str]:
    """Check that the configured canary name resolves, without exposing errors."""
    loop = asyncio.get_running_loop()
    try:
        addresses = await asyncio.wait_for(
            loop.getaddrinfo(_canary_host(), 443, type=socket.SOCK_STREAM),
            timeout=_DNS_TIMEOUT_SECONDS,
        )
        if not addresses:
            raise OSError("dns_unavailable")
    except TimeoutError:
        return {"status": "unhealthy", "error": "dns_unavailable"}
    except Exception:
        return {"status": "unhealthy", "error": "dns_unavailable"}
    return {"status": "healthy"}


async def _read_exactly(reader: asyncio.StreamReader, size: int) -> bytes:
    return await reader.readexactly(size)


async def _probe_socks(host: str, port: int, canary: str) -> None:
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port), timeout=_NETWORK_TIMEOUT_SECONDS
    )
    try:
        # Offer SOCKS5 with no authentication; a live listener alone is not enough.
        writer.write(b"\x05\x01\x00")
        await writer.drain()
        if (
            await asyncio.wait_for(_read_exactly(reader, 2), _NETWORK_TIMEOUT_SECONDS)
            != b"\x05\x00"
        ):
            raise ValueError("proxy_protocol_error")

        encoded_host = canary.encode("idna")
        if len(encoded_host) > 255:
            raise ValueError("proxy_protocol_error")
        writer.write(
            b"\x05\x01\x00\x03"
            + bytes((len(encoded_host),))
            + encoded_host
            + struct.pack(">H", 443)
        )
        await writer.drain()
        header = await asyncio.wait_for(_read_exactly(reader, 4), _NETWORK_TIMEOUT_SECONDS)
        version, reply, reserved, address_type = header
        if version != 5 or reserved != 0 or reply != 0:
            raise ConnectionError("proxy_unavailable")
        if address_type == 1:
            await asyncio.wait_for(_read_exactly(reader, 4), _NETWORK_TIMEOUT_SECONDS)
        elif address_type == 4:
            await asyncio.wait_for(_read_exactly(reader, 16), _NETWORK_TIMEOUT_SECONDS)
        elif address_type == 3:
            length = (await asyncio.wait_for(_read_exactly(reader, 1), _NETWORK_TIMEOUT_SECONDS))[0]
            if length == 0:
                raise ValueError("proxy_protocol_error")
            await asyncio.wait_for(_read_exactly(reader, length), _NETWORK_TIMEOUT_SECONDS)
        else:
            raise ValueError("proxy_protocol_error")
        await asyncio.wait_for(_read_exactly(reader, 2), _NETWORK_TIMEOUT_SECONDS)
    finally:
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
        except Exception:
            pass


async def _probe_http_connect(host: str, port: int, canary: str) -> None:
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port), timeout=_NETWORK_TIMEOUT_SECONDS
    )
    try:
        writer.write(f"CONNECT {canary}:443 HTTP/1.1\r\nHost: {canary}:443\r\n\r\n".encode("ascii"))
        await writer.drain()
        status_line = await asyncio.wait_for(reader.readline(), _NETWORK_TIMEOUT_SECONDS)
        if not status_line.startswith(b"HTTP/1.") or b" 200 " not in status_line:
            raise ConnectionError("proxy_unavailable")
        total = len(status_line)
        while True:
            line = await asyncio.wait_for(reader.readline(), _NETWORK_TIMEOUT_SECONDS)
            total += len(line)
            if not line or total > 16384:
                raise ValueError("proxy_protocol_error")
            if line in {b"\r\n", b"\n"}:
                break
    finally:
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
        except Exception:
            pass


async def probe_proxy() -> dict[str, str]:
    """Verify the configured proxy can tunnel to the fixed canary host."""
    raw = os.environ.get("CRAWL4AI_DEFAULT_PROXY", "").strip()
    if not raw:
        return {"status": "healthy", "mode": "not_required"}
    try:
        parsed = urlsplit(raw)
        if parsed.scheme not in {"socks5", "socks5h", "http"} or not parsed.hostname:
            return {"status": "unhealthy", "error": "proxy_protocol_error"}
        port = parsed.port or (1080 if parsed.scheme.startswith("socks5") else 80)
        if parsed.username or parsed.password:
            return {"status": "unhealthy", "error": "proxy_protocol_error"}
    except Exception:
        return {"status": "unhealthy", "error": "proxy_protocol_error"}

    try:
        async with asyncio.timeout(_NETWORK_TIMEOUT_SECONDS):
            if parsed.scheme.startswith("socks5"):
                await _probe_socks(parsed.hostname, port, _canary_host())
            else:
                await _probe_http_connect(parsed.hostname, port, _canary_host())
    except ValueError:
        return {"status": "unhealthy", "error": "proxy_protocol_error"}
    except Exception:
        return {"status": "unhealthy", "error": "proxy_unavailable"}
    return {"status": "healthy"}


async def readiness() -> dict[str, Any]:
    """Run independent dependency probes concurrently and summarize readiness."""
    dns, proxy = await asyncio.gather(probe_dns(), probe_proxy())
    checks = {"dns": dns, "proxy": proxy}
    return {
        "status": "healthy"
        if all(check["status"] == "healthy" for check in checks.values())
        else "degraded",
        "service": "local-crawl4ai",
        "checks": checks,
    }
