from __future__ import annotations

import logging
import re
from datetime import datetime

from flexgate.config import GatewayConfig, ProviderConfig, RouteConfig

logger = logging.getLogger("flexgate")


class NoRouteMatchError(Exception):
    def __init__(self, model: str) -> None:
        self.model = model
        super().__init__(f"No route matches model: {model}")


def _current_minutes() -> int:
    now = datetime.now()
    return now.hour * 60 + now.minute


def _in_window(minutes: int, start: int, end: int) -> bool:
    if start == end:
        return True
    if start < end:
        return start <= minutes < end
    # Overnight wrap: e.g. 22:00-06:00
    return minutes >= start or minutes < end


def _match_route(routes: list[RouteConfig], model: str) -> RouteConfig | None:
    for route in routes:
        if route.pattern.search(model):
            return route
    return None


# Claude Code sends bare aliases ("sonnet", "default", "sonnet[plan]") and
# legacy numbered names ("claude-3-7-sonnet-latest") when the client-side
# default-model env vars are absent; without normalization these miss the
# "^claude-<tier>" route patterns and fall through to the catch-all.
_ALIASES = {
    "default": "claude-sonnet",
    "sonnet": "claude-sonnet",
    "opus": "claude-opus",
    "haiku": "claude-haiku",
}

_LEGACY_NUMBERED = re.compile(r"^claude-\d+(?:\.\d+)*(?:-\d+)*-(sonnet|opus|haiku)(?:-.*)?$")


def _normalize_model(model: str) -> str:
    m = model.strip().lower().removesuffix("[plan]")
    if m in _ALIASES:
        return _ALIASES[m]
    match = _LEGACY_NUMBERED.match(m)
    if match:
        return f"claude-{match.group(1)}"
    return model


def resolve(config: GatewayConfig, model: str) -> tuple[ProviderConfig, str | None, str, RouteConfig]:
    """Return (provider, model_override, schedule_name, route) for the match.

    model_override is always a concrete model name: if the matched route omits
    'model', it falls back to the provider's first available_models entry.
    The route object itself is returned so the proxy can read/advance the
    route's active-key pointer.
    """
    now = _current_minutes()
    normalized = _normalize_model(model)

    for entry in config.schedule:
        if _in_window(now, entry.start_minutes, entry.end_minutes):
            route = _match_route(entry.routes, normalized)
            if route:
                provider = config.providers[route.provider_name]
                model_override = _resolve_model(provider, route.model)
                label = entry.name or f"{entry.start_minutes//60:02d}:{entry.start_minutes%60:02d}-{entry.end_minutes//60:02d}:{entry.end_minutes%60:02d}"
                logger.debug("schedule [%s] matched for model %s", label, model)
                return provider, model_override, label, route

    route = _match_route(config.routes, normalized)
    if route:
        provider = config.providers[route.provider_name]
        model_override = _resolve_model(provider, route.model)
        return provider, model_override, "default", route

    raise NoRouteMatchError(model)


def _resolve_model(provider: ProviderConfig, model_override: str | None) -> str:
    """Ensure a concrete model: fall back to the provider's first available model."""
    if model_override:
        return model_override
    if provider.available_models:
        return provider.available_models[0]
    raise ValueError(
        f"Provider '{provider.name}' has no model override and no available_models to fall back to"
    )
