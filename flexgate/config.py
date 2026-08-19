from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from flexgate.migrate import CURRENT_CONFIG_VERSION, detect_config_version

FLEXGATE_HOME = os.path.expanduser("~/.flexgate")


def get_default_config_path() -> str:
    """Config path priority: FLEXGATE_CONFIG env > ~/.flexgate/config.yaml."""
    env = os.environ.get("FLEXGATE_CONFIG")
    if env:
        return env
    return os.path.join(FLEXGATE_HOME, "config.yaml")


def ensure_home_dir() -> None:
    os.makedirs(FLEXGATE_HOME, exist_ok=True)


@dataclass
class ProviderConfig:
    name: str
    base_url: str
    api_key: str
    available_models: list[str] = field(default_factory=list)

    @property
    def default_model(self) -> str | None:
        return self.available_models[0] if self.available_models else None


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
        model = r.get("model")
        if model is None and not providers[prov_name].available_models:
            raise ValueError(
                f"Route '{r['pattern']}' omits 'model' but provider '{prov_name}' "
                f"has no 'available_models' to fall back to. "
                f"Add 'available_models' to provider '{prov_name}' or set an explicit 'model' on the route."
            )
        routes.append(RouteConfig(
            pattern=re.compile(r["pattern"]),
            provider_name=prov_name,
            model=model,
        ))
    return routes


def load_config(path: str | None = None) -> GatewayConfig:
    import yaml

    if path is None:
        path = get_default_config_path()

    with open(path) as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise ValueError(f"Config file {path} must be a YAML mapping")

    config_version = detect_config_version(raw)
    if config_version > CURRENT_CONFIG_VERSION:
        raise ValueError(
            f"{path} was written by a newer flexgate (config_version {config_version} > "
            f"{CURRENT_CONFIG_VERSION}). Run 'flexgate update' to upgrade."
        )

    cfg = GatewayConfig()

    srv = raw.get("server", {})
    host = srv.get("host", "127.0.0.1")
    port = srv.get("port", 8765)
    if not isinstance(host, str) or not host:
        raise ValueError("server.host must be a non-empty string")
    if isinstance(port, bool) or not isinstance(port, int) or not (1 <= port <= 65535):
        raise ValueError("server.port must be an integer between 1 and 65535")
    cfg.server = ServerConfig(
        host=host,
        port=port,
    )

    for name, prov in raw.get("providers", {}).items():
        available = prov.get("available_models", []) or []
        if not isinstance(available, list):
            raise ValueError(
                f"Provider '{name}': 'available_models' must be a list of model names"
            )
        cfg.providers[name] = ProviderConfig(
            name=name,
            base_url=prov["base_url"].rstrip("/"),
            api_key=prov["api_key"],
            available_models=[str(m) for m in available],
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
        path = get_default_config_path()

    data: dict = {
        "config_version": CURRENT_CONFIG_VERSION,
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
        entry: dict = {
            "base_url": prov.base_url,
            "api_key": prov.api_key,
        }
        if prov.available_models:
            entry["available_models"] = list(prov.available_models)
        data["providers"][name] = entry

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


TIER_PATTERNS: dict[str, str] = {
    "opus": "^claude-opus",
    "sonnet": "^claude-sonnet",
    "haiku": "^claude-haiku",
}

DEFAULT_CONFIG_TEMPLATE = """\
server:
  host: "127.0.0.1"
  port: 8765

# Each provider must declare `available_models`. The first entry is used as the
# fallback model whenever a route below omits its own `model:` field.
# Multiple accounts on the same upstream can be configured as separate
# providers (e.g. minimax-tmy / minimax-ywj below).
providers:
  minimax:
    base_url: "https://api.minimaxi.com/anthropic"
    api_key: "your-minimax-api-key"
    available_models:
      - "MiniMax-M3"
  minimax-tmy:
    base_url: "https://api.minimaxi.com/anthropic"
    api_key: "your-minimax-tmy-api-key"
    available_models:
      - "MiniMax-M3"
  minimax-ywj:
    base_url: "https://api.minimaxi.com/anthropic"
    api_key: "your-minimax-ywj-api-key"
    available_models:
      - "MiniMax-M3"
  zai:
    base_url: "https://api.z.ai/api/anthropic"
    api_key: "your-zai-api-key"
    available_models:
      - "glm-5.3"      # used as fallback when a route omits `model`
      - "glm-4.6v"
  xiaomi:
    base_url: "https://token-plan-cn.xiaomimimo.com/anthropic"
    api_key: "your-xiaomi-api-key"
    available_models:
      - "mimo-v2.5-pro"
  ustc:
    base_url: "https://api.llm.ustc.edu.cn"
    api_key: "your-ustc-api-key"
    available_models:
      - "deepseek-v4-pro"

claude_settings:
  default_opus_model: "claude-opus-4-7"
  default_sonnet_model: "claude-sonnet-4-6"
  default_haiku_model: "claude-haiku-4-5"
  api_timeout_ms: 3000000

# Scheduled routes (optional): switch model config by time of day; the first
# matching time window wins.
# schedule:
#   - name: "xiaomi-discount"
#     start: "16:00"
#     end: "24:00"
#     routes:
#       - pattern: "^claude-opus"
#         provider: zai
#         model: "glm-5.3"
#       - pattern: "^claude-sonnet"
#         provider: xiaomi
#         model: "mimo-v2.5-pro"
#       - pattern: "^claude-haiku"
#         provider: minimax
#         model: "MiniMax-M3"

# Default routes (used when no schedule window is active).
# Routes are matched top-to-bottom; first match wins.
# `model:` is optional — when omitted, the provider's first available_models
# entry is used. A provider referenced without `model` MUST have available_models.
routes:
  - pattern: "^claude-opus"
    provider: ustc
    model: "deepseek-v4-pro"
  - pattern: "^claude-haiku"
    provider: ustc
    model: "deepseek-v4-pro"
  - pattern: "^claude-sonnet"
    provider: ustc
    model: "deepseek-v4-pro"
  - pattern: ".*"           # catch-all
    provider: ustc
    model: "deepseek-v4-pro"
"""
