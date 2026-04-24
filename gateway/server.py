from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncGenerator

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from gateway.config import GatewayConfig
from gateway.proxy import handle_request
from gateway.router import NoRouteMatchError, resolve

logger = logging.getLogger("gateway")


def _error_json(error_type: str, message: str, status: int) -> JSONResponse:
    return JSONResponse(
        {"type": "error", "error": {"type": error_type, "message": message}},
        status_code=status,
    )


async def messages(request: Request) -> JSONResponse:
    t0 = time.monotonic()
    config: GatewayConfig = request.app.state.config
    client: httpx.AsyncClient = request.app.state.client

    body_bytes = await request.body()
    try:
        body_json = json.loads(body_bytes)
    except json.JSONDecodeError:
        return _error_json("invalid_request_error", "Invalid JSON in request body", 400)

    model_name = body_json.get("model", "")

    try:
        provider, model_override = resolve(config, model_name)
    except NoRouteMatchError as exc:
        return _error_json("not_found_error", str(exc), 503)

    display_model = model_override or model_name
    logger.info("%s -> %s (%s)", model_name, provider.name, display_model)

    incoming_headers = dict(request.headers)
    resp = await handle_request(
        client, body_bytes, body_json, incoming_headers, provider, model_override,
    )

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    logger.info(
        "%s -> %s (%s) | %s | %dms",
        model_name, provider.name, display_model, resp.status_code, elapsed_ms,
    )
    return resp


def create_app(config: GatewayConfig) -> Starlette:
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10, read=3600, write=10, pool=10),
    )

    routes = [Route("/v1/messages", messages, methods=["POST"])]

    async def lifespan(app: Starlette) -> AsyncGenerator[None, None]:
        app.state.config = config
        app.state.client = client
        yield
        await client.aclose()

    return Starlette(routes=routes, lifespan=lifespan)
