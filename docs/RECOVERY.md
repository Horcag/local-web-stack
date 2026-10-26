# Health and automatic recovery

On Linux/WSL with a systemd user manager and Docker CLI access, start the stack with:

```sh
bash scripts/up.sh
```

This waits for real container readiness and installs/enables the host watchdog. For an
already running stack, `bash scripts/install-watchdog.sh` installs the watchdog alone.
The user manager must start with the host; deployments without an interactive login
need systemd user lingering enabled by the host administrator. The watchdog runs outside
containers, with the operator's existing Docker access; no container receives the Docker socket.

Plain `docker compose up` installs the health checks and process restart policies, but
does not install the host watchdog. Windows PowerShell helpers wait for health checks;
the host watchdog in this repository targets Linux/WSL.

## What is checked

- External DNS is explicit for the proxy, search and crawler containers. Docker's embedded
  resolver still resolves internal service names. `LOCAL_WEB_DNS_PRIMARY` and
  `LOCAL_WEB_DNS_SECONDARY` override the default `1.1.1.1` and `8.8.8.8` when the deployment
  requires different reachable resolvers. This avoids inheriting a WSL-only resolver with
  no external upstreams, the cause of the 2026-10-26 outage.
- Valkey must answer `PING`. ByeDPI must resolve an external name and accept connections.
  SearXNG must resolve an external name and answer its own `/healthz` endpoint.
- Crawler `/live` is process liveness. `/health` returns 503 while dependencies are degraded:
  DNS resolution, a complete SOCKS5/HTTP CONNECT exchange through the configured proxy,
  and the browser canary. A listening SOCKS port alone cannot pass the crawler's proxy probe.
- With `LOCAL_WEB_CRAWL_CANARY_ENABLED=true` (Compose default), a browser reads
  `https://example.com/` every minute with cache bypass and checks HTTP 200 and actual
  Example Domain content. It uses the same crawler and profile lock as user requests;
  busy traffic takes precedence. Two failed canaries or a success older than three
  minutes without a canary or verified fresh primary navigation makes the crawler unhealthy.
  Fresh reads cannot clear failed-canary counts; cached reads refresh neither state.
- MCP health calls its actual `tools/list` endpoint and verifies the core tools. The
  `local_web_health` tool also reports aggregate `healthy`/`degraded` status.

Network probes have a five-second budget; the browser canary has a 25-second budget
plus bounded browser cleanup. Docker checks every 30 seconds, requires three failures
and allows startup grace. Dependency startup uses `condition: service_healthy`.

## How recovery works

A GET read retries one transient crawler failure in a fresh browser, bypassing cache.
The total primary crawl budget is 75 seconds (`LOCAL_WEB_CRAWL_TIMEOUT_SECONDS`), and the
profile queue wait is bounded at ten seconds. An exhausted primary read also attempts
the configured CDP browser, with a separate 30-second budget and the existing exact-target,
HTTP-status and content checks. POST reads are never automatically replayed, and an explicit
robots.txt denial is terminal across both acquisition routes.
Invalid crawler configuration also remains a terminal, sanitized service error.
Responses retain sanitized attempt/fallback receipts; MCP preserves the recovery receipt.
Unrecoverable failures remain errors and never become invented successful content.

Docker restarts exited processes. The host watchdog additionally restarts running,
unhealthy containers. It checks the exact container name, immutable ID and both Compose
project/service labels against its fixed allowlist. A crawler health failure identifying
the proxy restarts ByeDPI rather than repeatedly restarting its caller. Paused, stopped,
restarting, foreign and unverified containers are left alone.

Each target has a persistent cooldown (60 seconds, doubling up to 15 minutes), with at most
three restart attempts per 15-minute window. Failed restart commands also consume this
budget. State is atomically stored in `~/.local/state/local-web-watchdog/state.json`;
an unavailable or malformed state blocks recovery actions. A process lock prevents duplicate
watchdogs. Logs contain status transitions and sanitized categories, never proxy credentials.
External outages can still exhaust available routes; rate limits prevent restart storms.

Inspect the deployed state with:

```sh
docker compose ps
systemctl --user status local-web-watchdog
journalctl --user -u local-web-watchdog --since '15 minutes ago'
```

The deployment acceptance test freezes the verified ByeDPI process while leaving its TCP
listener alive, waits for automatic dependency recovery, then reads the original failing
URLs through MCP. Offline tests separately cover deadlines, cancellation, cache bypass,
fallback provenance, identity refusal, persistent cooldown and restart rate limits.

Docker contracts: [DNS configuration](https://docs.docker.com/reference/compose-file/services/#dns),
[readiness ordering](https://docs.docker.com/compose/how-tos/startup-order/),
[process restart policies](https://docs.docker.com/engine/containers/start-containers-automatically/).
