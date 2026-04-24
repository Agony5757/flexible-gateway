from __future__ import annotations

import os
import re
from dataclasses import dataclass, field


@dataclass
class ProviderConfig:
    name: str
    base_url: str
    api_key: str


@dataclass
class RouteConfig:
    pattern: re.Pattern[str]
    provider_name: str
    model: str | None = None


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8765


@dataclass
class GatewayConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    routes: list[RouteConfig] = field(default_factory=list)


def load_config(path: str | None = None) -> GatewayConfig:
    import yaml

    if path is None:
        path = os.environ.get("GATEWAY_CONFIG", "config.yaml")

    with open(path) as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise ValueError(f"Config file {path} must be a YAML mapping")

    cfg = GatewayConfig()

    # server
    srv = raw.get("server", {})
    cfg.server = ServerConfig(
        host=srv.get("host", "127.0.0.1"),
        port=srv.get("port", 8765),
    )

    # providers
    for name, prov in raw.get("providers", {}).items():
        cfg.providers[name] = ProviderConfig(
            name=name,
            base_url=prov["base_url"].rstrip("/"),
            api_key=prov["api_key"],
        )

    # routes
    for r in raw.get("routes", []):
        pat = r["pattern"]
        prov_name = r["provider"]
        if prov_name not in cfg.providers:
            raise ValueError(f"Route references unknown provider '{prov_name}'")
        cfg.routes.append(RouteConfig(
            pattern=re.compile(pat),
            provider_name=prov_name,
            model=r.get("model"),
        ))

    return cfg
