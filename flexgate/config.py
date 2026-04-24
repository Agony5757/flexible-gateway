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
class ClaudeSettings:
    default_opus_model: str = "claude-opus-4-7"
    default_sonnet_model: str = "claude-sonnet-4-6"
    default_haiku_model: str = "claude-haiku-4-5"
    api_timeout_ms: int = 3000000


@dataclass
class GatewayConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    routes: list[RouteConfig] = field(default_factory=list)
    claude_settings: ClaudeSettings = field(default_factory=ClaudeSettings)


def load_config(path: str | None = None) -> GatewayConfig:
    import yaml

    if path is None:
        path = os.environ.get("FLEXGATE_CONFIG", "config.yaml")

    with open(path) as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise ValueError(f"Config file {path} must be a YAML mapping")

    cfg = GatewayConfig()

    srv = raw.get("server", {})
    cfg.server = ServerConfig(
        host=srv.get("host", "127.0.0.1"),
        port=srv.get("port", 8765),
    )

    for name, prov in raw.get("providers", {}).items():
        cfg.providers[name] = ProviderConfig(
            name=name,
            base_url=prov["base_url"].rstrip("/"),
            api_key=prov["api_key"],
        )

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

    cs = raw.get("claude_settings", {})
    if cs:
        cfg.claude_settings = ClaudeSettings(
            default_opus_model=cs.get("default_opus_model", cfg.claude_settings.default_opus_model),
            default_sonnet_model=cs.get("default_sonnet_model", cfg.claude_settings.default_sonnet_model),
            default_haiku_model=cs.get("default_haiku_model", cfg.claude_settings.default_haiku_model),
            api_timeout_ms=cs.get("api_timeout_ms", cfg.claude_settings.api_timeout_ms),
        )

    return cfg


def save_config(cfg: GatewayConfig, path: str | None = None) -> None:
    import yaml

    if path is None:
        path = os.environ.get("FLEXGATE_CONFIG", "config.yaml")

    data: dict = {
        "server": {
            "host": cfg.server.host,
            "port": cfg.server.port,
        },
        "providers": {},
        "claude_settings": {
            "default_opus_model": cfg.claude_settings.default_opus_model,
            "default_sonnet_model": cfg.claude_settings.default_sonnet_model,
            "default_haiku_model": cfg.claude_settings.default_haiku_model,
            "api_timeout_ms": cfg.claude_settings.api_timeout_ms,
        },
        "routes": [],
    }

    for name, prov in cfg.providers.items():
        data["providers"][name] = {
            "base_url": prov.base_url,
            "api_key": prov.api_key,
        }

    for route in cfg.routes:
        r: dict = {"pattern": route.pattern.pattern, "provider": route.provider_name}
        if route.model:
            r["model"] = route.model
        data["routes"].append(r)

    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
