"""Validated discovery of an explicitly configured CDP browser endpoint."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import HTTPException


_MAX_CDP_VERSION_BYTES = 16 * 1024


def _loopback(host: str | None) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


def _cdp_headers(host_header: str | None) -> dict[str, str]:
    if not host_header:
        return {}
    try:
        authority = urlsplit("//" + host_header)
        authority.port
        if (
            not host_header.isascii()
            or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in host_header)
            or not authority.hostname
            or authority.username is not None
            or authority.password is not None
            or authority.path
            or authority.query
            or "#" in host_header
        ):
            raise ValueError
    except ValueError as exc:
        raise HTTPException(503, "Invalid configured CDP Host header") from exc
    return {"Host": host_header}


async def _cdp_endpoint(
    endpoint: str, host_header: str | None = None, *, websocket_required: bool = False
) -> str:
    """Keep loopback websocket advertisements on the explicitly configured broker."""
    try:
        headers = _cdp_headers(host_header)
        configured = urlsplit(endpoint)
        if configured.scheme in {"ws", "wss"}:
            return endpoint
        if (
            configured.scheme not in {"http", "https"}
            or not configured.hostname
            or configured.username is not None
            or configured.password is not None
            or "#" in endpoint
            or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in endpoint)
        ):
            raise ValueError
        configured.port
        # Local native Chrome already resolves its own advertised loopback URL.
        if _loopback(configured.hostname) and not websocket_required:
            return endpoint
        version_url = urlunsplit(
            (
                configured.scheme,
                configured.netloc,
                configured.path.rstrip("/") + "/json/version",
                configured.query,
                "",
            )
        )
        async with asyncio.timeout(5):
            async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
                async with client.stream(
                    "GET", version_url, headers={"Accept-Encoding": "identity", **headers}
                ) as response:
                    response.raise_for_status()
                    body = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=_MAX_CDP_VERSION_BYTES + 1):
                        body.extend(chunk)
                        if len(body) > _MAX_CDP_VERSION_BYTES:
                            raise ValueError
        version = json.loads(body)
        advertised_url = version.get("webSocketDebuggerUrl") if isinstance(version, dict) else None
        if not isinstance(advertised_url, str):
            raise ValueError
        advertised = urlsplit(advertised_url)
        advertised.port
        if (
            advertised.scheme not in {"ws", "wss"}
            or not advertised.hostname
            or advertised.username is not None
            or advertised.password is not None
            or "#" in advertised_url
            or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in advertised_url)
            or not re.fullmatch(r"/devtools/browser/[A-Za-z0-9_-]+", advertised.path)
        ):
            raise ValueError
        if not _loopback(advertised.hostname):
            return advertised_url if websocket_required else endpoint
        return urlunsplit(
            (
                "wss" if configured.scheme == "https" else "ws",
                configured.netloc,
                advertised.path,
                advertised.query,
                "",
            )
        )
    except (ValueError, RecursionError, httpx.HTTPError, TimeoutError) as exc:
        raise HTTPException(
            503, "Configured CDP broker returned no valid browser endpoint"
        ) from exc
