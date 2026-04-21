# Amazon Ads AI-MCP Token-Refresh Proxy

A tiny transparent proxy that sits between your MCP client
(Claude Desktop via `mcp-remote`, Claude Code CLI, or any other
MCP-speaking client) and Amazon's AI MCP endpoint
(`https://advertising-ai-eu.amazon.com/mcp` /
`https://advertising-ai-na.amazon.com/mcp`).

Its only job: **keep the connection authenticated**.

Amazon's access tokens (`Atza|…`) expire after ~1 hour. Without this
proxy your MCP client would die mid-session and you would have to
restart it. This proxy refreshes the token via LWA before expiry and
also recovers on an unexpected upstream `401` — fully transparent to
the client.

```
┌───────────────────┐      ┌────────────────────────┐      ┌────────────────────────────────────┐
│  MCP client       │      │  proxy (this repo)     │      │  Amazon Ads AI MCP                 │
│  Claude Desktop   │─────▶│  localhost:9090/mcp    │─────▶│  advertising-ai-eu.amazon.com/mcp  │
│  Claude Code CLI  │      │                        │      │                                    │
│  …                │      │  • injects Bearer      │      │                                    │
│                   │      │  • refreshes via LWA   │      │                                    │
│                   │      │  • retries on 401      │      │                                    │
└───────────────────┘      └────────────────────────┘      └────────────────────────────────────┘
```

## How it works

- `TokenCache` holds the current `access_token` + `expires_at` in memory.
- **Proactive refresh**: if `now >= expires_at - REFRESH_LEAD_SECONDS`
  the next request triggers a fresh LWA call before going upstream.
- **Reactive refresh**: if the upstream unexpectedly returns `401`
  (token revoked, clock skew, etc.), the proxy force-refreshes and
  retries the original request exactly once.
- The proxy strips the client's `Authorization` header and sets
  its own `Authorization: Bearer <current>`, `Amazon-Ads-ClientId`,
  `Amazon-Ads-AI-Account-Selection-Mode`, and
  `Amazon-Advertising-API-Scope` on every upstream request.
- Responses are streamed back to the client unchanged.

## Requirements

- Docker + Docker Compose, **or** Python 3.10+
- An Amazon Ads API application with a valid LWA refresh token.
  See
  [Amazon Ads API onboarding](https://advertising.amazon.com/API/docs/en-us/guides/onboarding/overview)
  and
  [Login with Amazon](https://developer.amazon.com/docs/login-with-amazon/register-web.html).

You will need four values:

| Name | Where to get it |
|---|---|
| LWA Client ID | `developer.amazon.com` → your Security Profile |
| LWA Client Secret | same place |
| LWA Refresh Token | issued by the Login-with-Amazon authorization code flow for your Amazon Ads account |
| Advertiser Profile ID | Amazon Ads API — the profile you want requests scoped to |

## Setup

### 1. Clone and configure

```bash
git clone <this-repo-url> amazon-ads-ai-proxy
cd amazon-ads-ai-proxy
cp .env.example .env
```

Open `.env` and fill in at least the four required values:

```dotenv
AMAZON_CLIENT_ID=amzn1.application-oa2-client.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
AMAZON_CLIENT_SECRET=your-lwa-client-secret
AMAZON_REFRESH_TOKEN=Atzr|...
AMAZON_PROFILE_ID=1234567890123456
```

### 2. Run with Docker (recommended)

```bash
docker compose up -d
docker compose logs -f
```

### 2-alt. Run without Docker

```bash
pip install "starlette>=0.37" "uvicorn[standard]>=0.29" "httpx>=0.27" "python-dotenv>=1.0"
python main.py
```

### 3. Verify it is alive

```bash
curl http://localhost:9090/health
```

```json
{
  "status": "ok",
  "upstream": "https://advertising-ai-eu.amazon.com/mcp",
  "has_access_token": true,
  "access_token_seconds_remaining": 3480
}
```

## Client configuration

Pick the block that matches your client. In all examples the MCP
server is simply named `amazon-ads` — rename as you like.

---

### Claude Code CLI

Claude Code supports HTTP MCP servers natively. Either add the
server via the CLI:

```bash
claude mcp add amazon-ads --transport http http://localhost:9090/mcp
```

…or drop a `.mcp.json` into your project root (shared with the
team) or into `~/.claude/` (personal, all projects):

```json
{
  "mcpServers": {
    "amazon-ads": {
      "type": "http",
      "url": "http://localhost:9090/mcp"
    }
  }
}
```

Then inside Claude Code:

```
/mcp
```

should list `amazon-ads` as connected.

---

### Claude Desktop — macOS

Claude Desktop only speaks stdio, so you need
[`mcp-remote`](https://www.npmjs.com/package/mcp-remote) as a
bridge to the proxy's HTTP endpoint.

Install it once globally:

```bash
npm install -g mcp-remote
```

Open the config file:

```bash
open -e "$HOME/Library/Application Support/Claude/claude_desktop_config.json"
```

Add the server entry:

```json
{
  "mcpServers": {
    "amazon-ads": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote",
        "http://localhost:9090/mcp",
        "--allow-http"
      ]
    }
  }
}
```

Quit Claude Desktop completely (`⌘Q`) and start it again. The
server should show up as connected in the MCP panel.

---

### Claude Desktop — Windows

Install `mcp-remote` once:

```powershell
npm install -g mcp-remote
```

Open the config file:

```powershell
notepad "$env:APPDATA\Claude\claude_desktop_config.json"
```

Add the server entry:

```json
{
  "mcpServers": {
    "amazon-ads": {
      "command": "npx.cmd",
      "args": [
        "-y",
        "mcp-remote",
        "http://localhost:9090/mcp",
        "--allow-http"
      ]
    }
  }
}
```

If Claude Desktop cannot find `npx.cmd` on `PATH`, point at the
shim explicitly:

```json
{
  "mcpServers": {
    "amazon-ads": {
      "command": "%APPDATA%\\npm\\mcp-remote.cmd",
      "args": [
        "http://localhost:9090/mcp",
        "--allow-http"
      ]
    }
  }
}
```

Fully quit Claude Desktop from the system tray and start it again.

> `--allow-http` is required because the proxy listens on plain
> HTTP on `localhost`. If you expose it on a domain with TLS, drop
> the flag.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `AMAZON_CLIENT_ID` | — | LWA Client ID (**required**) |
| `AMAZON_CLIENT_SECRET` | — | LWA Client Secret (**required**) |
| `AMAZON_REFRESH_TOKEN` | — | `Atzr|…` (**required**) |
| `AMAZON_PROFILE_ID` | — | Advertiser Profile ID sent as `Amazon-Advertising-API-Scope` (**required**) |
| `AMAZON_ACCOUNT_MODE` | `FIXED` | Value for `Amazon-Ads-AI-Account-Selection-Mode` |
| `UPSTREAM_URL` | `https://advertising-ai-eu.amazon.com/mcp` | Upstream endpoint. Use `advertising-ai-na.amazon.com` for North America or `advertising-ai-fe.amazon.com` for Far East. |
| `LWA_TOKEN_URL` | `https://api.amazon.com/auth/o2/token` | LWA token endpoint |
| `REFRESH_LEAD_SECONDS` | `120` | Refresh this many seconds before nominal expiry |
| `HOST` | `0.0.0.0` | Listen host |
| `PORT` | `9090` | Listen port |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |

## Endpoints

| Path | Method | Purpose |
|---|---|---|
| `/mcp` and `/mcp/*` | `GET`, `POST`, `PUT`, `PATCH`, `DELETE`, `OPTIONS` | Transparent MCP proxy |
| `/health` | `GET` | JSON with upstream URL, whether an access token is cached, and its remaining seconds |

## Operations

- **Token lifecycle**: LWA access tokens are valid for ~1 hour.
  The proxy refreshes `REFRESH_LEAD_SECONDS` seconds before
  expiry (default 2 minutes).
- **401 recovery**: on an upstream `401` the proxy force-refreshes
  once and retries the original request. The client only sees the
  final response.
- **Refresh-token rotation**: Amazon **may** return a new
  `refresh_token` in the LWA response. This proxy does **not**
  persist it automatically. If it ever happens, put the new value
  into `.env` and restart. Every refresh is logged.
- **Concurrency**: an `asyncio.Lock` around the refresh path
  prevents a thundering-herd of LWA calls when many parallel
  requests find the cache stale at the same moment.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `token_refresh_failed` on very first request | Client ID / Secret / Refresh Token do not match | Re-check `.env`, make sure refresh token was issued for this client |
| `token_refresh_failed_after_401` | Amazon revoked both the access token and the refresh token | Re-run the Login-with-Amazon authorization flow and update `AMAZON_REFRESH_TOKEN` |
| `502` from the proxy | Upstream unreachable | `docker compose logs proxy`, check outbound connectivity, verify `UPSTREAM_URL` |
| Health is green but the client sees no tools | `mcp-remote` not installed, `--allow-http` missing, or wrong URL | Re-check the client config block above |
| Many `Refreshing LWA access token` lines per minute | `REFRESH_LEAD_SECONDS` larger than the token's `expires_in` | Lower `REFRESH_LEAD_SECONDS` (keep it well below 3600) |

## Security notes

- **Access token lives in RAM only.** The current `access_token`
  is held as an attribute on a `TokenCache` instance inside the
  running `uvicorn` process — no file, no database, no Docker
  volume, no logging. When the container stops, the cached token
  is gone; the next request after restart automatically triggers
  a fresh LWA refresh (adds ~400 ms to the first call and nothing
  else). This also means horizontally scaling the proxy would
  give each instance its own cache — by design there is one
  process.
- **Only long-lived credentials touch disk**, and only via `.env`:
  `AMAZON_CLIENT_ID`, `AMAZON_CLIENT_SECRET`, `AMAZON_REFRESH_TOKEN`.
  Treat that file like a password store — it is already in
  `.gitignore`.
- The proxy listens on plain HTTP. Do not expose it on an
  untrusted network. Keep it on `localhost`, a private Docker
  network, or terminate TLS in front of it (nginx, Caddy, Traefik).
- Logs include the URL of every upstream request and every LWA
  refresh, but never the access or refresh tokens themselves.

## License

MIT — see `LICENSE` if present, otherwise feel free to adapt.
