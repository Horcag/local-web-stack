"""Container health commands with semantic checks and bounded local HTTP I/O."""

import json
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen


class ReadinessError(Exception):
    def __init__(self, checks: list[str]):
        self.checks = checks


def check(target: str) -> None:
    if target == "crawl4ai":
        request = Request("http://127.0.0.1:11235/health")
    elif target == "mcp":
        request = Request(
            "http://127.0.0.1:8765/mcp",
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode(),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
    else:
        raise ValueError("Unknown healthcheck target")
    try:
        response = urlopen(request, timeout=8)
    except HTTPError as error:
        if target != "crawl4ai":
            raise
        response = error
    with response:
        payload = json.loads(response.read(256 * 1024))
    if target == "crawl4ai":
        if payload.get("status") != "healthy":
            checks = payload.get("checks", {})
            raise ReadinessError(
                [
                    name
                    for name in ("dns", "proxy", "crawler")
                    if checks.get(name, {}).get("status") != "healthy"
                ]
            )
    else:
        names = {tool["name"] for tool in payload["result"]["tools"]}
        assert {"read_url", "web_search", "local_web_health"} <= names, "MCP tools missing"


if __name__ == "__main__":
    try:
        check(sys.argv[1])
    except ReadinessError as error:
        print(json.dumps({"failed_checks": error.checks}))
        raise SystemExit(1) from None
    except Exception:
        # Healthcheck logs never include proxy credentials, response bodies or raw errors.
        print(json.dumps({"failed_checks": ["service"]}))
        raise SystemExit(1) from None
