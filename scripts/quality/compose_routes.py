"""Validate container-to-container endpoints in normalized Compose configuration."""

from __future__ import annotations

import json
import subprocess
from typing import Any
from urllib.parse import urlsplit

ROUTES = (
    ("crawl4ai", "SEARXNG_URL", "searxng"),
    ("mcp", "SEARXNG_URL", "searxng"),
    ("mcp", "CRAWL4AI_URL", "crawl4ai"),
)


def validate_routes(config: dict[str, Any]) -> None:
    services = config.get("services", {})
    errors = []
    for caller, variable, target in ROUTES:
        if caller not in services or target not in services:
            errors.append(f"{caller}.{variable}: required caller/target service is missing")
            continue
        endpoint = services[caller].get("environment", {}).get(variable)
        if not isinstance(endpoint, str) or not endpoint.strip():
            errors.append(f"{caller}.{variable}: explicit cross-service endpoint is required")
            continue
        destination = services[target]
        hosts = {target, destination.get("container_name", target)}
        internal_ports = {
            int(port["target"])
            for port in destination.get("ports", [])
            if isinstance(port, dict) and port.get("target") is not None
        }
        internal_ports.update(int(port) for port in destination.get("expose", []))
        try:
            url = urlsplit(endpoint)
            port = url.port or {"http": 80, "https": 443}.get(url.scheme)
            valid = (
                url.scheme in {"http", "https"}
                and url.hostname in hosts
                and not url.username
                and not url.password
                and port in internal_ports
            )
        except ValueError:
            valid = False
        if not valid:
            errors.append(
                f"{caller}.{variable}: must address declared {target} service/container "
                "using its internal target port, not loopback or a published host port"
            )
    if errors:
        raise ValueError("\n".join(errors))


def main() -> None:
    result = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    validate_routes(json.loads(result.stdout))
    print("Compose cross-service routes passed (normalized config; no daemon required)")


if __name__ == "__main__":
    main()
