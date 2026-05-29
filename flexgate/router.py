from __future__ import annotations

import logging
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


def _match_route(routes: list[RouteConfig], model: str) -> tuple[ProviderConfig, str | None] | None:
    for route in routes:
        if route.pattern.search(model):
            return route.provider_name, route.model
    return None


def resolve(config: GatewayConfig, model: str) -> tuple[ProviderConfig, str | None, str]:
    """Return (provider, model_override, schedule_name) for the matching route.

    model_override is always a concrete model name: if the matched route omits
    'model', it falls back to the provider's first available_models entry.
    """
    now = _current_minutes()

    for entry in config.schedule:
        if _in_window(now, entry.start_minutes, entry.end_minutes):
            match = _match_route(entry.routes, model)
            if match:
                prov_name, model_override = match
                provider = config.providers[prov_name]
                model_override = _resolve_model(provider, model_override)
                label = entry.name or f"{entry.start_minutes//60:02d}:{entry.start_minutes%60:02d}-{entry.end_minutes//60:02d}:{entry.end_minutes%60:02d}"
                logger.debug("schedule [%s] matched for model %s", label, model)
                return provider, model_override, label

    match = _match_route(config.routes, model)
    if match:
        prov_name, model_override = match
        provider = config.providers[prov_name]
        model_override = _resolve_model(provider, model_override)
        return provider, model_override, "default"

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
