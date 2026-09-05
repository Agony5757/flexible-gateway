"""OpenAI-compatible Images API endpoint, backed by MiniMax image generation.

Exposes `POST /v1/images/generations` so OpenAI-style clients (e.g. editppt's
openai-compatible-api image backend) can generate images through the gateway
without any per-provider configuration: the upstream is derived from whichever
configured provider points at MiniMax (`api.minimaxi.com` / `api.minimax.io`).

Translation notes:
- MiniMax only has text-to-image (`POST /v1/image_generation`, model
  `image-01`). `size` ("WxH") is mapped to width/height clamped to 512-2048
  and rounded to multiples of 8; unrecognized sizes are omitted.
- MiniMax replies with `data.image_base64[]` (JPEG bytes); it is forwarded
  verbatim as OpenAI `data[].b64_json` — downstream tools sniff the real
  content type, so no transcoding dependency is introduced here.
- `/v1/images/edits` is answered 501: MiniMax has no general image-editing or
  inpainting capability (its subject_reference is character-only).
"""

from __future__ import annotations

import json
import logging
import re
import time
from urllib.parse import urlsplit

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse

from flexgate.config import GatewayConfig, ProviderConfig
from flexgate.fallback import error_json

logger = logging.getLogger("flexgate.images")

MINIMAX_MODEL = "image-01"
MINIMAX_MIN_EDGE = 512
MINIMAX_MAX_EDGE = 2048
MINIMAX_MAX_PROMPT_CHARS = 1500

# Requested model names containing any of these hints are accepted and mapped
# to MiniMax image-01 (the only image backend behind this gateway).
_IMAGE_MODEL_HINTS = (
    "gpt-image",
    "dall-e",
    "image-01",
    "seedream",
    "flux",
    "sdxl",
    "stable-diffusion",
    "minimax",
)


def _error_json(message: str, status: int, code: str = "minimax_error") -> JSONResponse:
    error_type = {
        400: "invalid_request_error",
        501: "not_implemented",
        503: "not_implemented",
    }.get(status, "api_error")
    return error_json(message, status, error_type, code)


def _find_minimax_provider(config: GatewayConfig) -> ProviderConfig | None:
    candidates = [p for p in config.providers.values() if "minimax" in p.base_url]
    for provider in candidates:
        if provider.name == "minimax":
            return provider
    return candidates[0] if candidates else None


def _image_api_root(base_url: str) -> str:
    parts = urlsplit(base_url)
    return f"{parts.scheme}://{parts.netloc}"


def _clamp_edge(value: int) -> int:
    return max(MINIMAX_MIN_EDGE, min(MINIMAX_MAX_EDGE, value))


def _round8(value: float) -> int:
    return max(8, int(round(value / 8)) * 8)


def _map_size(size: object) -> dict[str, int]:
    if not isinstance(size, str):
        return {}
    match = re.fullmatch(r"(\d+)x(\d+)", size)
    if not match:
        return {}
    w, h = int(match.group(1)), int(match.group(2))
    if w <= 0 or h <= 0:
        return {}
    scale = min(1.0, MINIMAX_MAX_EDGE / max(w, h))
    w, h = w * scale, h * scale
    if min(w, h) < MINIMAX_MIN_EDGE:
        boost = MINIMAX_MIN_EDGE / min(w, h)
        w, h = w * boost, h * boost
        if max(w, h) > MINIMAX_MAX_EDGE:
            shrink = MINIMAX_MAX_EDGE / max(w, h)
            w, h = w * shrink, h * shrink
    return {"width": _clamp_edge(_round8(w)), "height": _clamp_edge(_round8(h))}


async def images_generate(request: Request) -> JSONResponse:
    t0 = time.monotonic()
    config: GatewayConfig = request.app.state.config
    client: httpx.AsyncClient = request.app.state.client

    try:
        body = json.loads(await request.body())
    except json.JSONDecodeError:
        return _error_json("Invalid JSON in request body", 400, "bad_request")

    provider = _find_minimax_provider(config)
    if provider is None:
        return _error_json(
            "No MiniMax provider configured (need a provider whose base_url points at MiniMax)",
            503,
            "provider_not_found",
        )

    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return _error_json("Missing required field: prompt", 400, "bad_request")

    requested_model = str(body.get("model") or "")
    if requested_model and not any(h in requested_model.lower() for h in _IMAGE_MODEL_HINTS):
        return _error_json(
            f"Model '{requested_model}' is not a supported image model. flexgate maps image "
            f"generation to MiniMax {MINIMAX_MODEL} (text-to-image only); request a gpt-image-* "
            f"alias or '{MINIMAX_MODEL}'.",
            501,
            "model_not_supported",
        )
    logger.info(
        "images/generations model=%s -> %s via %s",
        requested_model or "(default)", MINIMAX_MODEL, provider.name,
    )

    n = body.get("n", 1)
    payload: dict = {
        "model": MINIMAX_MODEL,
        "prompt": prompt[:MINIMAX_MAX_PROMPT_CHARS],
        "response_format": "base64",
        "n": max(1, min(9, int(n))) if isinstance(n, (int, float)) else 1,
    }
    payload.update(_map_size(body.get("size")))

    url = f"{_image_api_root(provider.base_url)}/v1/image_generation"
    headers = {
        "Authorization": f"Bearer {provider.api_key}",
        "Content-Type": "application/json",
    }

    try:
        resp = await client.post(url, headers=headers, json=payload)
    except httpx.ConnectError as exc:
        logger.error("Provider %s unreachable: %s", provider.name, exc)
        return _error_json(f"Provider unreachable: {provider.name}", 502)

    try:
        data = resp.json()
    except Exception:
        return _error_json(resp.text[:500], 502)

    base_resp = data.get("base_resp") or {}
    status_code = base_resp.get("status_code", 0)
    if resp.status_code != 200 or status_code != 0:
        message = base_resp.get("status_msg") or f"HTTP {resp.status_code}"
        logger.error("MiniMax image generation failed: %s", message)
        return _error_json(f"MiniMax upstream: {message}", 502)

    images = (data.get("data") or {}).get("image_base64") or []
    if not images:
        return _error_json("MiniMax returned no image_base64 payload", 502)

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    logger.info("images/generations <- %s | %d image(s) | %dms", provider.name, len(images), elapsed_ms)
    return JSONResponse({
        "created": int(time.time()),
        "data": [{"b64_json": img, "revised_prompt": None} for img in images],
    })


async def images_edits(request: Request) -> JSONResponse:
    return _error_json(
        "MiniMax image-01 does not support image editing/inpainting; "
        "only /v1/images/generations (text-to-image) is available through this gateway.",
        501,
        "edits_not_supported",
    )
