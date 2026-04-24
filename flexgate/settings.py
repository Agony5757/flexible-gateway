from __future__ import annotations

import glob
import json
import os
import re
import shutil
from datetime import datetime

from flexgate.config import (
    ClaudeSettings,
    GatewayConfig,
    ProviderConfig,
    RouteConfig,
    load_config,
    save_config,
)

CLAUDE_DIR = os.path.expanduser("~/.claude")
SETTINGS_GLOB = os.path.join(CLAUDE_DIR, "settings.json*")


def _scan_settings_files() -> list[tuple[str, str]]:
    """Return [(filepath, suffix), ...] for all ~/.claude/settings.json* files."""
    results = []
    for path in sorted(glob.glob(SETTINGS_GLOB)):
        basename = os.path.basename(path)
        # settings.json → suffix "default", settings.json.zai → "zai"
        if basename == "settings.json":
            suffix = "default"
        else:
            suffix = basename.replace("settings.json.", "", 1)
            # Skip backup files from our own apply command
            if suffix.startswith("bak"):
                continue
        results.append((path, suffix))
    return results


def _extract_provider(filepath: str) -> dict | None:
    """Extract base_url and api_key from a settings.json file."""
    try:
        with open(filepath) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    env = data.get("env", {})
    base_url = env.get("ANTHROPIC_BASE_URL", "")
    api_key = env.get("ANTHROPIC_AUTH_TOKEN", "")

    if not base_url or not api_key:
        return None

    return {"base_url": base_url.rstrip("/"), "api_key": api_key}


def settings_import(config_path: str) -> None:
    """Read ~/.claude/settings.json* files and populate config.yaml providers."""
    files = _scan_settings_files()
    if not files:
        print("No settings files found in ~/.claude/")
        return

    # Try to load existing config, or start fresh
    try:
        cfg = load_config(config_path)
    except (FileNotFoundError, ValueError):
        cfg = GatewayConfig()

    print("Found settings files:")
    for filepath, suffix in files:
        provider = _extract_provider(filepath)
        status = "OK" if provider else "SKIPPED (no credentials)"
        print(f"  {filepath} [{suffix}] — {status}")

    print()

    # Map suffixes to provider names
    provider_map: dict[str, dict] = {}
    for filepath, suffix in files:
        provider = _extract_provider(filepath)
        if not provider:
            continue

        if suffix == "default":
            # Try to infer provider name from base_url
            name = _infer_provider_name(provider["base_url"])
        else:
            name = suffix

        provider_map[name] = provider

    if not provider_map:
        print("No valid provider credentials found in settings files.")
        return

    # Merge into config
    for name, prov in provider_map.items():
        if name in cfg.providers:
            cfg.providers[name].base_url = prov["base_url"]
            cfg.providers[name].api_key = prov["api_key"]
            print(f"  Updated provider: {name}")
        else:
            cfg.providers[name] = ProviderConfig(
                name=name,
                base_url=prov["base_url"],
                api_key=prov["api_key"],
            )
            print(f"  Added provider: {name}")

    save_config(cfg, config_path)
    print(f"\nConfig saved to {config_path}")


def _infer_provider_name(base_url: str) -> str:
    """Guess provider name from the base_url."""
    url_lower = base_url.lower()
    if "z.ai" in url_lower:
        return "zai"
    if "minimax" in url_lower:
        return "minimax"
    if "openai" in url_lower:
        return "openai"
    if "anthropic" in url_lower:
        return "anthropic"
    # Fallback: use hostname
    from urllib.parse import urlparse
    parsed = urlparse(base_url)
    return parsed.hostname or "unknown"


def settings_apply(config_path: str) -> None:
    """Apply config.yaml to ~/.claude/settings.json (with backup)."""
    cfg = load_config(config_path)

    settings_file = os.path.join(CLAUDE_DIR, "settings.json")
    if not os.path.exists(settings_file):
        print(f"Warning: {settings_file} does not exist, creating new file.")

    # Backup existing settings.json
    if os.path.exists(settings_file):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = os.path.join(CLAUDE_DIR, f"settings.json.bak.{timestamp}")
        shutil.copy2(settings_file, backup_path)
        print(f"Backup: {backup_path}")

    # Build new settings
    # Preserve existing non-env fields (permissions, skip_prompts, etc.)
    existing: dict = {}
    if os.path.exists(settings_file):
        try:
            with open(settings_file) as f:
                existing = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    new_settings: dict = {}

    # Keep permissions and other fields
    for key in ("permissions", "skipDangerousModePermissionPrompt", "skipAutoPermissionPrompt"):
        if key in existing:
            new_settings[key] = existing[key]

    # Set env with gateway connection
    cs = cfg.claude_settings
    new_settings["env"] = {
        "ANTHROPIC_BASE_URL": f"http://{cfg.server.host}:{cfg.server.port}",
        "ANTHROPIC_AUTH_TOKEN": "gateway",
        "API_TIMEOUT_MS": str(cs.api_timeout_ms),
        "ANTHROPIC_DEFAULT_OPUS_MODEL": cs.default_opus_model,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": cs.default_sonnet_model,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": cs.default_haiku_model,
    }

    with open(settings_file, "w") as f:
        json.dump(new_settings, f, indent=2)
        f.write("\n")

    print(f"Applied: {settings_file}")
    print(f"  ANTHROPIC_BASE_URL = http://{cfg.server.host}:{cfg.server.port}")
    print(f"  OPUS_MODEL  = {cs.default_opus_model}")
    print(f"  SONNET_MODEL = {cs.default_sonnet_model}")
    print(f"  HAIKU_MODEL  = {cs.default_haiku_model}")
