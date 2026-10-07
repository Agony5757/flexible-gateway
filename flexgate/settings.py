"""Bridge between config.yaml and Claude Code's ``~/.claude/settings.json``.

``settings import`` pulls provider credentials from every
``~/.claude/settings.json*`` file into config.yaml, merging keys into the
provider's ``api_keys`` list.

``settings apply`` is non-destructive: it rewrites only the env keys flexgate
manages and leaves every other field of the existing settings.json (hooks,
permissions, model preferences, unrelated env vars) untouched.
"""

from __future__ import annotations

import glob
import json
import os
import shutil
from datetime import datetime

from flexgate.config import (
    ApiKey,
    GatewayConfig,
    ProviderConfig,
    load_config,
    save_config,
)
from flexgate.ui import bold, cyan, dim, err, green, yellow

CLAUDE_DIR = os.path.expanduser("~/.claude")
SETTINGS_GLOB = os.path.join(CLAUDE_DIR, "settings.json*")

# env keys in settings.json that flexgate owns
_MANAGED_ENV_KEYS = (
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "API_TIMEOUT_MS",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
)


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


def settings_import(config_path: str, as_json: bool = False) -> dict | None:
    """Read ~/.claude/settings.json* files and populate config.yaml providers.

    Returns a report dict (always); with ``as_json`` the human output is
    suppressed and the caller emits the report as JSON.
    """
    from flexgate.ui import emit_json, json_fail

    def fail(message: str) -> None:
        if as_json:
            json_fail(message)
        err(message, 1)

    files = _scan_settings_files()
    if not files:
        if as_json:
            emit_json({"ok": True, "files": [], "providers": [], "saved": False,
                       "note": "No settings files found in ~/.claude/"})
            return None
        print("No settings files found in ~/.claude/")
        return {"files": [], "providers": [], "saved": False}

    # Try to load existing config, or start fresh
    try:
        cfg = load_config(config_path)
    except (FileNotFoundError, ValueError):
        cfg = GatewayConfig()

    if not as_json:
        print(bold("Found settings files"))
    provider_map: dict[str, dict] = {}
    scanned = []
    for filepath, suffix in files:
        provider = _extract_provider(filepath)
        status = green("ok") if provider else dim("skipped (no credentials)")
        if not as_json:
            print(f"  {filepath} {dim(f'[{suffix}]')} — {status}")
        scanned.append({"file": filepath, "suffix": suffix, "ok": provider is not None})
        if not provider:
            continue
        if suffix == "default":
            name = _infer_provider_name(provider["base_url"])
        else:
            name = suffix
        provider_map[name] = provider

    report: dict = {"files": scanned, "providers": [], "saved": False}
    if not provider_map:
        if as_json:
            emit_json({"ok": True, **report,
                       "note": "No valid provider credentials found in settings files."})
            return None
        print("No valid provider credentials found in settings files.")
        return report

    if not as_json:
        print()
    note = f"imported {datetime.now().strftime('%Y%m%d')}"
    for name, prov in provider_map.items():
        if name in cfg.providers:
            existing = cfg.providers[name]
            existing.base_url = prov["base_url"]
            if any(k.key == prov["api_key"] for k in existing.api_keys):
                if not as_json:
                    print(f"  {bold(cyan(name))}: key already present, base_url updated")
                report["providers"].append({"provider": name, "action": "base_url-updated"})
            else:
                existing.api_keys.append(ApiKey(key=prov["api_key"], note=note))
                if not as_json:
                    print(
                        f"  {bold(cyan(name))}: base_url updated, "
                        f"key #{len(existing.api_keys)} appended {dim(f'[{note}]')}"
                    )
                report["providers"].append(
                    {"provider": name, "action": "key-appended", "key_index": len(existing.api_keys)}
                )
        else:
            cfg.providers[name] = ProviderConfig(
                name=name,
                base_url=prov["base_url"],
                api_keys=[ApiKey(key=prov["api_key"], note=note)],
            )
            if not as_json:
                print(f"  {bold(cyan(name))}: provider added {dim(f'[{note}]')}")
            report["providers"].append({"provider": name, "action": "provider-added"})

    save_config(cfg, config_path)
    report["saved"] = True
    if as_json:
        emit_json({"ok": True, "config": config_path, **report})
        return None
    print(f"\nConfig saved to {config_path}")
    return report


def _mask_token(token: str) -> str:
    if len(token) <= 8:
        return "***"
    return token[:4] + "***" + token[-4:]


def _build_new_settings(cfg: GatewayConfig, existing: dict, auth_token: str) -> dict:
    """Return the new settings dict: all existing fields preserved, env keys merged."""
    new_settings = dict(existing)
    new_env = dict(existing.get("env") or {})
    cs = cfg.claude_settings
    new_env.update({
        "ANTHROPIC_BASE_URL": f"http://{cfg.server.host}:{cfg.server.port}",
        "ANTHROPIC_AUTH_TOKEN": auth_token,
        "API_TIMEOUT_MS": str(cs.api_timeout_ms),
        "ANTHROPIC_DEFAULT_OPUS_MODEL": cs.default_opus_model,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": cs.default_sonnet_model,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": cs.default_haiku_model,
    })
    new_settings["env"] = new_env
    return new_settings


def _print_env_diff(old_env: dict, new_env: dict) -> None:
    for key in _MANAGED_ENV_KEYS:
        old, new = old_env.get(key), new_env.get(key)
        display = _mask_token(str(new)) if key == "ANTHROPIC_AUTH_TOKEN" else str(new)
        if old == new:
            print(f"  {dim('=')} {key} {dim(display)}")
        elif old is None:
            print(f"  {green('+')} {key} = {display}")
        else:
            old_disp = _mask_token(str(old)) if key == "ANTHROPIC_AUTH_TOKEN" else str(old)
            print(f"  {yellow('~')} {key}: {old_disp} → {display}")


def settings_apply(
    config_path: str,
    *,
    auth_token: str | None = None,
    dry_run: bool = False,
    as_json: bool = False,
) -> dict:
    """Apply config.yaml to ~/.claude/settings.json (non-destructive, with backup).

    Only the ``env`` keys listed in ``_MANAGED_ENV_KEYS`` are written; every
    other field of the existing file is preserved. ``auth_token`` defaults to
    the token already present in the file (or ``"gateway"`` when absent), so
    every caller — CLI and ``service install`` — shares one token policy.
    """
    from flexgate.ui import emit_json

    cfg = load_config(config_path)

    settings_file = os.path.join(CLAUDE_DIR, "settings.json")
    existing: dict = {}
    if os.path.exists(settings_file):
        try:
            with open(settings_file) as f:
                existing = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    existing_env = existing.get("env") or {}
    effective_token = auth_token or existing_env.get("ANTHROPIC_AUTH_TOKEN") or "gateway"
    new_settings = _build_new_settings(cfg, existing, effective_token)
    new_env = new_settings["env"]
    env_report = {key: _mask_token(str(value)) if key == "ANTHROPIC_AUTH_TOKEN" else value
                  for key, value in new_env.items()}

    if as_json:
        backup = None
        existed = os.path.exists(settings_file)
        if not dry_run:
            os.makedirs(CLAUDE_DIR, exist_ok=True)
            if existed:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                backup = os.path.join(CLAUDE_DIR, f"settings.json.bak.{timestamp}")
                shutil.copy2(settings_file, backup)
            with open(settings_file, "w") as f:
                json.dump(new_settings, f, indent=2)
                f.write("\n")
        emit_json({
            "ok": True,
            "settings_file": settings_file,
            "dry_run": dry_run,
            "backup": backup,
            "env": env_report,
            "created": not existed and not dry_run,
        })
        return {"settings_file": settings_file, "backup": backup, "env": env_report}

    if dry_run:
        print(bold(f"settings apply (dry run) → {settings_file}"))
        if not os.path.exists(settings_file):
            print(f"  {dim('file does not exist, would be created')}")
        _print_env_diff(existing_env, new_env)
        return {"settings_file": settings_file, "dry_run": True, "env": env_report}

    os.makedirs(CLAUDE_DIR, exist_ok=True)
    backup = None
    if os.path.exists(settings_file):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = os.path.join(CLAUDE_DIR, f"settings.json.bak.{timestamp}")
        shutil.copy2(settings_file, backup_path)
        backup = backup_path
        print(f"Backup: {dim(backup_path)}")

    with open(settings_file, "w") as f:
        json.dump(new_settings, f, indent=2)
        f.write("\n")

    cs = cfg.claude_settings
    print(f"Applied: {bold(settings_file)}")
    print(f"  {dim('ANTHROPIC_BASE_URL')} = http://{cfg.server.host}:{cfg.server.port}")
    print(f"  {dim('OPUS_MODEL')}  = {cs.default_opus_model}")
    print(f"  {dim('SONNET_MODEL')} = {cs.default_sonnet_model}")
    print(f"  {dim('HAIKU_MODEL')} = {cs.default_haiku_model}")
    if existing:
        print(f"  {dim('other fields (hooks, permissions, ...) preserved')}")
    return {"settings_file": settings_file, "backup": backup, "env": env_report}
