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
    """Return (provider, model_override, schedule_name) for the matching route."""
    now = _current_minutes()

    for entry in config.schedule:
        if _in_window(now, entry.start_minutes, entry.end_minutes):
            match = _match_route(entry.routes, model)
            if match:
                prov_name, model_override = match
                label = entry.name or f"{entry.start_minutes//60:02d}:{entry.start_minutes%60:02d}-{entry.end_minutes//60:02d}:{entry.end_minutes%60:02d}"
                logger.debug("schedule [%s] matched for model %s", label, model)
                return config.providers[prov_name], model_override, label

    match = _match_route(config.routes, model)
    if match:
        prov_name, model_override = match
        return config.providers[prov_name], model_override, "default"

    raise NoRouteMatchError(model)
