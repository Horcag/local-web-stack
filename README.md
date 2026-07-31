# Local Web Stack: Local Search & Crawl Stack for LLM Agents

A lightweight, local, token-free web toolset optimized for AI agents and coding assistants (like ChatGPT, Cursor, Windsurf, or Claude Desktop). 

It combines **SearXNG** for meta-search aggregation, **Crawl4AI** (wrapped in FastAPI) for high-quality, stealthy HTML-to-Markdown extraction, and a **Model Context Protocol (MCP)** server to expose them seamlessly to your LLM workspace.

---

## Key Features

- **Local & Token-Free:** Works entirely on your machine without requiring paid API keys or subscription credits.
- **Curated Meta-Search:** Pre-configured SearXNG pool running Yandex, Mojeek, and mwmbl — the engines measured to answer the actual query from this network (94% of returned results carry a query token past the first, over 10 technical queries). Bing, Google, Brave, and DuckDuckGo stay loaded but out of the default pool: Bing serves non-browser clients a placeholder SERP answering the first query term only, Google returns an empty JS shell, Brave is rate-limited, DuckDuckGo hard-CAPTCHAs under agent-rate load. All four remain reachable via `engines=<name>` for re-measurement. `web_search` additionally drops any engine whose whole batch shares no token with the query.
- **Stealth & Evasion Crawling:** Crawl4AI wrapped with `UndetectedAdapter` and `playwright-stealth` to bypass Cloudflare WAF, Turnstile, and client-side automation detection.
- **Pacing & Serialization:** Automatic request serialization and domain-specific delays (`CRAWL4AI_DOMAIN_DELAY_SECONDS`) to prevent target rate-limiting.
- **LLM-Friendly Output:** Extracts web pages and returns structured, clean Markdown optimized for LLM context windows.
- **Dual MCP Interface:** Supports both stdio (default) and standalone HTTP transport mode (running on port `8765`).

---

## Architecture

```mermaid
graph TD
    Agent[AI Agent / IDE] -->|MCP JSON-RPC| MCP[local-web MCP Server]
    MCP -->|HTTP /search| SearXNG[SearXNG Container]
    MCP -->|HTTP /md| Crawl4AI[Crawl4AI Container]
    SearXNG -->|Caching| Valkey[(Valkey Cache)]
    Crawl4AI -->|Playwright Chromium / Chrome| Web[(World Wide Web)]
    SearXNG -->|Search Requests| Web
```

- **Valkey (Redis fork):** Handles search caching.
- **SearXNG:** Serves search requests aggregator.
- **Crawl4AI (FastAPI):** Controls Chromium/Chrome via Playwright for reading pages.
- **MCP Server Wrapper:** Exposes tools to your AI agent.

---

## Prerequisites

- **Docker & Docker Compose** (for running Valkey, SearXNG, and Crawl4AI).
- **Python 3.10+** (with [uv](https://github.com/astral-sh/uv) recommended for fast execution).
- **Windows PowerShell** (for helper scripts).

---

## Quick Start

### 1. Start the Stack

Run the startup helper script. This builds the Crawl4AI container (baking in the official Google Chrome Stable and system dependencies), starts the services in the background, and initializes the HTTP MCP wrapper:

```powershell
.\scripts\up.ps1
```

### 2. Verify Health

Check if SearXNG and Crawl4AI are healthy and able to perform search/crawl requests:

```powershell
.\scripts\verify.ps1
```

### 3. Stop the Stack

Shut down all containers and stop the HTTP MCP background process:

```powershell
.\scripts\down.ps1
```

---

## Exposed MCP Tools

Once connected, the `local-web` MCP server provides the following tools to the agent:

1. **`web_search`**
   - **Arguments:** `query` (str), `max_results` (int, default 8), `category` (str, default "general"), `language` (str, default "auto"), `time_range` (str), `engines` (comma-separated str)
   - **Description:** Performs a local SearXNG aggregated search and returns a compact JSON with search result titles, snippets, and URLs. `engines` and `category` are mutually exclusive — SearXNG unions the two parameters rather than intersecting them, so `category` is only sent when no engines are selected. The response reports the engines that actually produced results in `engines`, what was asked for in `requested_engines`, and anything cut by the relevance guard in `dropped_engines`.
2. **`read_url`**
   - **Arguments:** `url` (str), `cache_mode` (str, default "enabled")
   - **Description:** Crawls a URL using Crawl4AI and returns structured, clean Markdown.
3. **`local_web_health`**
   - **Description:** Returns the API statuses of SearXNG and Crawl4AI.

---

## Configuration & Tuning

Configuration is managed via environment variables defined in [docker-compose.yml](file:///C:/Users/nikit/Documents/Codex/2026-06-11/firecrwal/work/local-web-stack/docker-compose.yml):

| Environment Variable | Default Value | Description |
| :--- | :--- | :--- |
| `CRAWL4AI_BROWSER_TYPE` | `undetected` | Set to `undetected` to run the official Google Chrome with stealth patches. |
| `CRAWL4AI_ENABLE_STEALTH` | `true` | Enables Playwright stealth hooks to mask webdriver properties. |
| `CRAWL4AI_MAGIC` | `true` | Activates Crawl4AI's automatic overlay removal and bot evasion patterns. |
| `CRAWL4AI_SIMULATE_USER` | `true` | Simulates human browser behavior (scrolls, mouse tracking, random delays). |
| `CRAWL4AI_WAIT_UNTIL` | `domcontentloaded` | Sets browser wait state. Keep at `domcontentloaded` to prevent hangs on Cloudflare Turnstile pages. |
| `CRAWL4AI_DOMAIN_DELAY_SECONDS`| `3.0` | Minimum delay in seconds between queries to the same domain. |
| `CRAWL4AI_PROXY_SERVER` | `""` | Optional proxy server URI (e.g. `http://user:pass@host:port`) for heavy anti-bot evasion. |

---

## Integrating with AI Agents / IDEs

### Cursor / Windsurf (via HTTP Transport)
Start the stack with `.\scripts\up.ps1`. The MCP server will automatically launch in the background at `http://127.0.0.1:8765/mcp`.
In your IDE settings, add a new MCP server:
- **Type:** `sse`
- **URL:** `http://127.0.0.1:8765/mcp`

### Claude Desktop (via Stdio Transport)
Add the server configuration to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "local-web": {
      "command": "uv",
      "args": [
        "run",
        "C:/absolute/path/to/local-web-stack/mcp/local_web_mcp.py"
      ],
      "env": {
        "LOCAL_WEB_MCP_TRANSPORT": "stdio",
        "SEARXNG_URL": "http://127.0.0.1:8088",
        "CRAWL4AI_URL": "http://127.0.0.1:11235"
      }
    }
  }
}
```
