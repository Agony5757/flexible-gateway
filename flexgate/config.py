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
class ScheduleEntry:
    name: str
    start_minutes: int
    end_minutes: int
    routes: list[RouteConfig] = field(default_factory=list)


@dataclass
class GatewayConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    routes: list[RouteConfig] = field(default_factory=list)
    schedule: list[ScheduleEntry] = field(default_factory=list)
    claude_settings: ClaudeSettings = field(default_factory=ClaudeSettings)


def _parse_hhmm(value: str) -> int:
    """Parse 'HH:MM' string to minutes since midnight."""
    parts = value.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid time format '{value}', expected 'HH:MM'")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h <= 24 and 0 <= m <= 59):
        raise ValueError(f"Time out of range: '{value}'")
    return h * 60 + m


def _parse_routes(raw_routes: list[dict], providers: dict[str, ProviderConfig]) -> list[RouteConfig]:
    routes: list[RouteConfig] = []
    for r in raw_routes:
        prov_name = r["provider"]
        if prov_name not in providers:
            raise ValueError(f"Route references unknown provider '{prov_name}'")
        routes.append(RouteConfig(
            pattern=re.compile(r["pattern"]),
            provider_name=prov_name,
            model=r.get("model"),
        ))
    return routes


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

    cfg.routes = _parse_routes(raw.get("routes", []), cfg.providers)

    for entry in raw.get("schedule", []):
        cfg.schedule.append(ScheduleEntry(
            name=entry.get("name", ""),
            start_minutes=_parse_hhmm(entry["start"]),
            end_minutes=_parse_hhmm(entry["end"]),
            routes=_parse_routes(entry.get("routes", []), cfg.providers),
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


def _serialize_routes(routes: list[RouteConfig]) -> list[dict]:
    result: list[dict] = []
    for route in routes:
        r: dict = {"pattern": route.pattern.pattern, "provider": route.provider_name}
        if route.model:
            r["model"] = route.model
        result.append(r)
    return result


def _format_hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


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
        "routes": _serialize_routes(cfg.routes),
    }

    for name, prov in cfg.providers.items():
        data["providers"][name] = {
            "base_url": prov.base_url,
            "api_key": prov.api_key,
        }

    if cfg.schedule:
        schedule_data: list[dict] = []
        for entry in cfg.schedule:
            schedule_data.append({
                "name": entry.name,
                "start": _format_hhmm(entry.start_minutes),
                "end": _format_hhmm(entry.end_minutes),
                "routes": _serialize_routes(entry.routes),
            })
        data["schedule"] = schedule_data

    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
