from __future__ import annotations

import json
import logging

import httpx
from starlette.responses import JSONResponse, StreamingResponse

from flexgate.config import ProviderConfig

logger = logging.getLogger("flexgate.proxy")

MULTIMODAL_MODELS = {"MiniMax-M3", "glm-4.6v"}
_IMAGE_NOT_SUPPORTED_NOTE = "[flexgate] image message is not handled because multimodal is not supported by the current model"

# Upstream statuses that mean "this key can't serve the request right now"
# (dead key, quota/balance exhausted, rate limited, overloaded) — retry the
# same request with the provider's next fallback key, if any.
FALLBACK_STATUSES = {401, 402, 403, 429, 500, 502, 503, 529}


def _mask_key(key: str) -> str:
    if len(key) <= 8:
        return "***"
    return key[:4] + "***" + key[-4:]


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
    incoming_headers: dict[str, str], api_key: str
) -> dict[str, str]:
    return {
        "content-type": "application/json",
        "x-api-key": api_key,
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

    model = body_json.get("model", "")
    strip_images = not _model_supports_multimodal(model) and _has_image_content(body_json)
    if strip_images:
        _strip_images_from_request(body_json)
        logger.info("Stripped image content for non-multimodal model: %s", model)

    body = json.dumps(body_json, ensure_ascii=False).encode()
    url = f"{provider.base_url}/v1/messages"
    is_stream = body_json.get("stream", False)

    # Key fallback: try each api_keys entry in order until one succeeds or
    # fails with a non-retryable error. Streaming responses can only fall
    # back before the upstream returns 200 (once bytes flow to the client
    # the response is committed).
    keys = provider.api_keys
    last_error: JSONResponse | None = None
    for index, entry in enumerate(keys):
        if index > 0:
            logger.warning(
                "Provider %s: falling back to key #%d (%s%s)",
                provider.name, index + 1, _mask_key(entry.key),
                f" [{entry.note}]" if entry.note else "",
            )
        headers = _build_upstream_headers(incoming_headers, entry.key)
        if is_stream:
            resp, retryable = await _stream_proxy(client, url, headers, body, provider.name)
        else:
            resp, retryable = await _regular_proxy(client, url, headers, body, provider.name, strip_images)
        if not retryable:
            return resp
        last_error = resp if isinstance(resp, JSONResponse) else last_error
        if index < len(keys) - 1:
            logger.warning(
                "Provider %s key #%d (%s%s) failed; trying next key",
                provider.name, index + 1, _mask_key(entry.key),
                f" [{entry.note}]" if entry.note else "",
            )

    assert last_error is not None
    return last_error


async def _regular_proxy(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    body: bytes,
    provider_name: str,
    images_stripped: bool = False,
) -> tuple[JSONResponse, bool]:
    """POST one attempt. Returns (response, retryable_with_next_key)."""
    try:
        resp = await client.post(url, headers=headers, content=body)
    except httpx.ConnectError as exc:
        logger.error("Provider %s unreachable: %s", provider_name, exc)
        return _error_json("api_error", f"Provider unreachable: {provider_name}", 502), True
    except httpx.TimeoutException as exc:
        logger.error("Provider %s timed out: %s", provider_name, exc)
        return _error_json("api_error", f"Provider timed out: {provider_name}", 504), True

    if resp.status_code in FALLBACK_STATUSES:
        return _upstream_error(resp), True

    try:
        data = resp.json()
    except Exception:
        return _error_json("api_error", resp.text[:500], resp.status_code), False

    if images_stripped and resp.status_code == 200:
        _add_note_to_response(data)

    return JSONResponse(data, status_code=resp.status_code), False


def _upstream_error(resp: httpx.Response) -> JSONResponse:
    try:
        data = resp.json()
    except Exception:
        data = {"type": "error", "error": {"type": "api_error", "message": resp.text[:500]}}
    return JSONResponse(data, status_code=resp.status_code)


async def _stream_proxy(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    body: bytes,
    provider_name: str,
) -> tuple[JSONResponse | StreamingResponse, bool]:
    """Open one streaming attempt. Returns (response, retryable_with_next_key)."""
    try:
        req = client.build_request("POST", url, headers=headers, content=body)
        upstream = await client.send(req, stream=True)
    except httpx.ConnectError as exc:
        logger.error("Provider %s unreachable: %s", provider_name, exc)
        return _error_json("api_error", f"Provider unreachable: {provider_name}", 502), True
    except httpx.TimeoutException as exc:
        logger.error("Provider %s timed out: %s", provider_name, exc)
        return _error_json("api_error", f"Provider timed out: {provider_name}", 504), True

    if upstream.status_code != 200:
        error_body = await upstream.aread()
        await upstream.aclose()
        try:
            data = json.loads(error_body)
        except Exception:
            data = {"type": "error", "error": {"type": "api_error", "message": error_body.decode(errors="replace")[:500]}}
        return JSONResponse(data, status_code=upstream.status_code), upstream.status_code in FALLBACK_STATUSES

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
    ), False
