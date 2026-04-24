from __future__ import annotations

from flexgate.config import GatewayConfig, ProviderConfig, RouteConfig


class NoRouteMatchError(Exception):
    def __init__(self, model: str) -> None:
        self.model = model
        super().__init__(f"No route matches model: {model}")


def resolve(config: GatewayConfig, model: str) -> tuple[ProviderConfig, str | None]:
    """Return (provider, model_override) for the first matching route."""
    for route in config.routes:
        if route.pattern.search(model):
            return config.providers[route.provider_name], route.model
    raise NoRouteMatchError(model)
