"""Structured errors for every route and capability flexgate does not serve.

A bare Starlette 404 tells an API client nothing: it cannot tell "wrong port"
apart from "right gateway, unsupported endpoint". Every unmatched request
instead gets a JSON error explaining what was asked for and what flexgate
does support, so client-side fallback logic (and humans reading logs) can
react without guessing.

Error payload shape is a merge of the Anthropic and OpenAI conventions so
both client families parse it:

    {"type": "error", "error": {"type": ..., "message": ..., "code": ...}}

Status code policy:
- 404 route_not_found        unknown path entirely; message lists valid routes
- 405 method_not_allowed     known path, wrong HTTP method
- 501 endpoint_not_supported recognized API family flexgate does not serve
- 501 capability_not_supported / model_not_supported for valid routes with
  unsupported operations or model names (raised by the owning module)
"""

from __future__ import annotations

import re

from starlette.requests import Request
from starlette.responses import JSONResponse

SUPPORTED_ROUTES = ("POST /v1/messages", "POST /v1/images/generations")


def error_json(message: str, status: int, error_type: str, code: str) -> JSONResponse:
    return JSONResponse(
        {"type": "error", "error": {"type": error_type, "message": message, "code": code}},
        status_code=status,
    )


# Recognized-but-unsupported API families: (path regex, human reason).
# Order matters; first match wins. Keep reasons actionable: say what the
# caller probably wanted and where that capability lives, if anywhere.
_UNSUPPORTED_FAMILIES: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"^/v1/messages/count_tokens$"),
        "Token counting is not proxied; flexgate only forwards POST /v1/messages. "
        "Count tokens client-side (e.g. with a tokenizer) instead.",
    ),
    (
        re.compile(r"^/v1/(chat/completions|responses)(/.*)?$"),
        "OpenAI chat is not served: flexgate proxies Anthropic-style POST /v1/messages only. "
        "Point an Anthropic-compatible client at this gateway, or call the upstream provider directly.",
    ),
    (
        re.compile(r"^/v1/images/edits$"),
        # Owning module (images.py) normally answers this first; kept as a safety net.
        "Image editing/inpainting is not available: the MiniMax image-01 backend is text-to-image only. "
        "Use POST /v1/images/generations, or edit the image with another tool and upload the result.",
    ),
    (
        re.compile(r"^/v1/embeddings"),
        "Embeddings are not proxied: no configured provider exposes an embedding endpoint. "
        "Call an embedding-capable provider directly.",
    ),
    (
        re.compile(r"^/v1/(audio|videos?)(/.*)?$"),
        "Audio/video endpoints are not proxied by flexgate. "
        "Call the upstream provider's native API directly.",
    ),
    (
        re.compile(r"^/v1/(models|files|fine_tuning|batches|moderations|vector_stores|assistants|threads|uploads)(/.*)?$"),
        "This OpenAI platform endpoint is not proxied: flexgate is a routing gateway, not an API platform. "
        "It only serves POST /v1/messages and POST /v1/images/generations.",
    ),
]


async def fallback(request: Request) -> JSONResponse:
    """Catch-all handler for every path/method flexgate does not serve."""
    path = request.url.path.rstrip("/") or "/"

    if path in ("/v1/messages", "/v1/images/generations"):
        return error_json(
            f"{request.method} is not allowed on {path}; use POST.",
            405,
            "invalid_request_error",
            "method_not_allowed",
        )

    for pattern, reason in _UNSUPPORTED_FAMILIES:
        if pattern.match(path):
            return error_json(reason, 501, "not_implemented", "endpoint_not_supported")

    return error_json(
        f"Unknown route: {request.method} {path}. "
        f"flexgate serves exactly: {', '.join(SUPPORTED_ROUTES)}.",
        404,
        "not_found_error",
        "route_not_found",
    )
