"""
Amazon Ads AI-MCP Token-Refresh Proxy
─────────────────────────────────────
Sits between Claude Desktop (via mcp-remote) and Amazon's
`https://advertising-ai-eu.amazon.com/mcp` endpoint.

Claude Desktop → http://localhost:PORT/mcp
                      │
                      │ adds Authorization: Bearer <current access_token>
                      │ refreshes via LWA when token is near expiry
                      ▼
                https://advertising-ai-eu.amazon.com/mcp
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from dotenv import load_dotenv
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

load_dotenv()

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)
logger = logging.getLogger("aimcp-proxy")

# ── Configuration ──────────────────────────────────────────────────────────

UPSTREAM_URL = os.environ.get(
    "UPSTREAM_URL", "https://advertising-ai-eu.amazon.com/mcp"
)
LWA_TOKEN_URL = os.environ.get(
    "LWA_TOKEN_URL", "https://api.amazon.com/auth/o2/token"
)

CLIENT_ID = os.environ["AMAZON_CLIENT_ID"]
CLIENT_SECRET = os.environ["AMAZON_CLIENT_SECRET"]
REFRESH_TOKEN = os.environ["AMAZON_REFRESH_TOKEN"]
PROFILE_ID = os.environ["AMAZON_PROFILE_ID"]
ACCOUNT_MODE = os.environ.get("AMAZON_ACCOUNT_MODE", "FIXED")

# Optional: bootstrap with an existing access_token so the proxy does not
# have to call LWA on its very first request. Useful during debugging.
INITIAL_ACCESS_TOKEN = os.environ.get("AMAZON_ACCESS_TOKEN") or None
INITIAL_EXPIRES_IN = int(os.environ.get("AMAZON_EXPIRES_IN", "0") or 0)

# Refresh this many seconds before nominal expiry to avoid serving a token
# that upstream will reject due to clock skew / network latency.
REFRESH_LEAD_SECONDS = int(os.environ.get("REFRESH_LEAD_SECONDS", "120"))

# Off by default. When enabled, exposes GET /access-token returning the
# current Bearer token as JSON. Intended for local scripts that need to
# call Amazon Ads APIs not covered by the MCP surface.
EXPOSE_ACCESS_TOKEN = os.environ.get("EXPOSE_ACCESS_TOKEN", "false").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)

# Hop-by-hop headers that must not be forwarded verbatim.
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}

# ── Token cache ────────────────────────────────────────────────────────────


class TokenCache:
    def __init__(self) -> None:
        self.access_token: str | None = INITIAL_ACCESS_TOKEN
        self.expires_at: float = (
            time.time() + INITIAL_EXPIRES_IN if INITIAL_EXPIRES_IN else 0.0
        )
        self.lock = asyncio.Lock()

    def _fresh_enough(self) -> bool:
        return bool(
            self.access_token
            and time.time() < self.expires_at - REFRESH_LEAD_SECONDS
        )

    async def get(
        self, client: httpx.AsyncClient, force: bool = False
    ) -> str:
        if not force and self._fresh_enough():
            return self.access_token  # type: ignore[return-value]

        async with self.lock:
            if not force and self._fresh_enough():
                return self.access_token  # type: ignore[return-value]
            return await self._refresh(client)

    async def _refresh(self, client: httpx.AsyncClient) -> str:
        logger.info("Refreshing LWA access token via %s", LWA_TOKEN_URL)
        resp = await client.post(
            LWA_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": REFRESH_TOKEN,
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            timeout=30.0,
        )
        if resp.status_code != 200:
            logger.error(
                "LWA refresh failed: %s %s", resp.status_code, resp.text[:500]
            )
            resp.raise_for_status()
        data = resp.json()
        self.access_token = data["access_token"]
        self.expires_at = time.time() + int(data.get("expires_in", 3600))
        logger.info(
            "Got new access token, expires in %ss",
            int(self.expires_at - time.time()),
        )
        return self.access_token  # type: ignore[return-value]


tokens = TokenCache()

# Upstream HTTP client — keep alive across requests for connection reuse.
upstream = httpx.AsyncClient(
    http2=False,
    timeout=httpx.Timeout(connect=15.0, read=300.0, write=60.0, pool=30.0),
    limits=httpx.Limits(max_connections=32, max_keepalive_connections=8),
)

# ── Request handler ────────────────────────────────────────────────────────


def _filter_request_headers(incoming: dict[str, str]) -> dict[str, str]:
    """Copy client headers but drop hop-by-hop + auth (we set our own)."""
    out: dict[str, str] = {}
    for name, value in incoming.items():
        low = name.lower()
        if low in _HOP_BY_HOP:
            continue
        if low == "authorization":  # replaced below
            continue
        if low.startswith("amazon-ads-") or low.startswith(
            "amazon-advertising-api-"
        ):
            # replaced below with proxy-managed values
            continue
        out[name] = value
    return out


def _filter_response_headers(incoming: httpx.Headers) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, value in incoming.items():
        if name.lower() in _HOP_BY_HOP:
            continue
        out[name] = value
    return out


async def _send_upstream(
    request: Request, body: bytes, access_token: str
) -> httpx.Response:
    headers = _filter_request_headers(dict(request.headers))
    headers["authorization"] = f"Bearer {access_token}"
    headers["amazon-ads-clientid"] = CLIENT_ID
    headers["amazon-ads-ai-account-selection-mode"] = ACCOUNT_MODE
    headers["amazon-advertising-api-scope"] = PROFILE_ID

    req = upstream.build_request(
        request.method,
        UPSTREAM_URL,
        headers=headers,
        content=body,
        params=dict(request.query_params),
    )
    return await upstream.send(req, stream=True)


async def proxy(request: Request) -> StreamingResponse | JSONResponse:
    body = await request.body()

    try:
        token = await tokens.get(upstream)
    except httpx.HTTPError as exc:
        logger.exception("Could not obtain access token")
        return JSONResponse(
            {
                "error": "token_refresh_failed",
                "detail": str(exc),
            },
            status_code=502,
        )

    resp = await _send_upstream(request, body, token)

    # On 401, assume the cached token went stale unexpectedly and retry once
    # with a forced refresh. This also covers the first request when the
    # upstream decides the bootstrap token it knows differs from ours.
    if resp.status_code == 401:
        logger.warning(
            "Upstream returned 401; forcing token refresh and retrying once"
        )
        await resp.aclose()
        try:
            token = await tokens.get(upstream, force=True)
        except httpx.HTTPError as exc:
            logger.exception("Forced token refresh failed after 401")
            return JSONResponse(
                {
                    "error": "token_refresh_failed_after_401",
                    "detail": str(exc),
                },
                status_code=502,
            )
        resp = await _send_upstream(request, body, token)

    response_headers = _filter_response_headers(resp.headers)

    async def stream_body():
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
        finally:
            await resp.aclose()

    return StreamingResponse(
        stream_body(),
        status_code=resp.status_code,
        headers=response_headers,
        media_type=resp.headers.get("content-type"),
    )


async def health(_request: Request) -> JSONResponse:
    remaining = max(0, int(tokens.expires_at - time.time()))
    return JSONResponse(
        {
            "status": "ok",
            "upstream": UPSTREAM_URL,
            "has_access_token": bool(tokens.access_token),
            "access_token_seconds_remaining": remaining,
        }
    )


async def access_token(request: Request) -> JSONResponse:
    # GET  → return cached token (refreshes only if stale per REFRESH_LEAD_SECONDS)
    # POST → force a fresh LWA refresh and return the new token
    force = request.method == "POST"
    try:
        token = await tokens.get(upstream, force=force)
    except httpx.HTTPError as exc:
        logger.exception("Could not obtain access token for /access-token")
        return JSONResponse(
            {"error": "token_refresh_failed", "detail": str(exc)},
            status_code=502,
        )
    return JSONResponse(
        {
            "access_token": token,
            "expires_at": tokens.expires_at,
            "expires_in": max(0, int(tokens.expires_at - time.time())),
            "refreshed": force,
        }
    )


@asynccontextmanager
async def lifespan(_app: Starlette):
    yield
    await upstream.aclose()


# ── App ────────────────────────────────────────────────────────────────────

_routes = [
    Route(
        "/mcp",
        proxy,
        methods=["GET", "POST", "DELETE", "OPTIONS", "PUT", "PATCH"],
    ),
    Route(
        "/mcp/{path:path}",
        proxy,
        methods=["GET", "POST", "DELETE", "OPTIONS", "PUT", "PATCH"],
    ),
    Route("/health", health, methods=["GET"]),
]

if EXPOSE_ACCESS_TOKEN:
    _routes.append(Route("/access-token", access_token, methods=["GET", "POST"]))
    logger.warning(
        "EXPOSE_ACCESS_TOKEN=true — GET/POST /access-token will return the "
        "Bearer token without any auth (POST forces a refresh). Only safe "
        "when the proxy is reachable on localhost / a trusted network."
    )

app = Starlette(routes=_routes, lifespan=lifespan)


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "9090"))
    host = os.environ.get("HOST", "0.0.0.0")
    logger.info("Starting AI-MCP proxy on %s:%s → %s", host, port, UPSTREAM_URL)
    uvicorn.run(app, host=host, port=port, log_level=LOG_LEVEL.lower())
