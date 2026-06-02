from __future__ import annotations

import json
import logging

import httpx
from starlette.responses import JSONResponse, StreamingResponse

from flexgate.config import ProviderConfig

logger = logging.getLogger("flexgate.proxy")

MULTIMODAL_MODELS = {"MiniMax-M3"}
_IMAGE_NOT_SUPPORTED_NOTE = "[flexgate] image message is not handled because multimodal is not supported by the current model"


def _model_supports_multimodal(model: str) -> bool:
    return model in MULTIMODAL_MODELS


def _has_image_content(body_json: dict) -> bool:
    for msg in body_json.get("messages", []):
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") in ("image", "image_url"):
                    return True
    return False


def _strip_images_from_request(body_json: dict) -> None:
    for msg in body_json.get("messages", []):
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        remaining = [b for b in content if not (isinstance(b, dict) and b.get("type") in ("image", "image_url"))]
        if not remaining:
            remaining = [{"type": "text", "text": _IMAGE_NOT_SUPPORTED_NOTE}]
        else:
            remaining.append({"type": "text", "text": _IMAGE_NOT_SUPPORTED_NOTE})
        msg["content"] = remaining


def _add_note_to_response(data: dict) -> None:
    for block in data.get("content", []):
        if isinstance(block, dict) and block.get("type") == "text":
            block["text"] += "\n" + _IMAGE_NOT_SUPPORTED_NOTE
            return
    data.setdefault("content", []).append({"type": "text", "text": _IMAGE_NOT_SUPPORTED_NOTE})


def _error_json(error_type: str, message: str, status: int) -> JSONResponse:
    return JSONResponse(
        {"type": "error", "error": {"type": error_type, "message": message}},
        status_code=status,
    )


def _build_upstream_headers(
    incoming_headers: dict[str, str], provider: ProviderConfig
) -> dict[str, str]:
    return {
        "content-type": "application/json",
        "x-api-key": provider.api_key,
        "anthropic-version": incoming_headers.get("anthropic-version", "2023-06-01"),
        "anthropic-beta": incoming_headers.get("anthropic-beta", ""),
    }


async def handle_request(
    client: httpx.AsyncClient,
    body: bytes,
    body_json: dict,
    incoming_headers: dict[str, str],
    provider: ProviderConfig,
    model_override: str | None,
) -> JSONResponse | StreamingResponse:
    if model_override:
        body_json["model"] = model_override
        body = json.dumps(body_json, ensure_ascii=False).encode()

    url = f"{provider.base_url}/v1/messages"
    headers = _build_upstream_headers(incoming_headers, provider)

    is_stream = body_json.get("stream", False)
    model = body_json.get("model", "")
    strip_images = not _model_supports_multimodal(model) and _has_image_content(body_json)

    if strip_images:
        _strip_images_from_request(body_json)
        body = json.dumps(body_json, ensure_ascii=False).encode()
        logger.info("Stripped image content for non-multimodal model: %s", model)

    if is_stream:
        return await _stream_proxy(client, url, headers, body, provider.name, strip_images)
    return await _regular_proxy(client, url, headers, body, provider.name, strip_images)


async def _regular_proxy(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    body: bytes,
    provider_name: str,
    images_stripped: bool = False,
) -> JSONResponse:
    try:
        resp = await client.post(url, headers=headers, content=body)
    except httpx.ConnectError as exc:
        logger.error("Provider %s unreachable: %s", provider_name, exc)
        return _error_json("api_error", f"Provider unreachable: {provider_name}", 502)

    try:
        data = resp.json()
    except Exception:
        return _error_json("api_error", resp.text[:500], resp.status_code)

    if images_stripped and resp.status_code == 200:
        _add_note_to_response(data)

    return JSONResponse(data, status_code=resp.status_code)


async def _stream_proxy(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    body: bytes,
    provider_name: str,
    images_stripped: bool = False,
) -> StreamingResponse:
    try:
        req = client.build_request("POST", url, headers=headers, content=body)
        upstream = await client.send(req, stream=True)
    except httpx.ConnectError as exc:
        logger.error("Provider %s unreachable: %s", provider_name, exc)
        return _error_json("api_error", f"Provider unreachable: {provider_name}", 502)

    if upstream.status_code != 200:
        error_body = await upstream.aread()
        await upstream.aclose()
        try:
            data = json.loads(error_body)
        except Exception:
            data = {"type": "error", "error": {"type": "api_error", "message": error_body.decode(errors="replace")[:500]}}
        return JSONResponse(data, status_code=upstream.status_code)

    async def generate():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        except httpx.ReadError:
            logger.error("Upstream %s connection lost during streaming", provider_name)
        finally:
            await upstream.aclose()

    return StreamingResponse(
        generate(),
        status_code=200,
        headers={
            "content-type": "text/event-stream",
            "cache-control": "no-cache",
            "connection": "keep-alive",
        },
    )
