from __future__ import annotations

import json
import shutil
import subprocess
import sys

from flexgate.config import ProviderConfig, load_config, save_config
from flexgate.registry import KNOWN_BASE_URLS, KNOWN_DEFAULT_MODELS

# Each provider is a folder under this path, named exactly after the provider,
# containing an API_KEY secret:
#   /providers/minimax-tmy/API_KEY
PROVIDERS_FOLDER = "/providers"


def _match_base_url(name: str, table: dict[str, str]) -> str | None:
    """Longest known prefix of `name` (dash boundary) → its base_url."""
    for prefix in sorted(table, key=len, reverse=True):
        if name == prefix or name.startswith(prefix + "-"):
            return table[prefix]
    return None


def _match_models(name: str, table: dict[str, list[str]]) -> list[str]:
    """Longest known prefix of `name` (dash boundary) → its default models."""
    for prefix in sorted(table, key=len, reverse=True):
        if name == prefix or name.startswith(prefix + "-"):
            return list(table[prefix])
    return []


def _mask_key(key: str) -> str:
    if len(key) <= 8:
        return "***"
    return key[:4] + "***" + key[-4:]


def _run_infisical(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["infisical", *args],
        capture_output=True,
        text=True,
        timeout=60,
    )


def _check_prerequisites() -> None:
    if shutil.which("infisical") is None:
        print("infisical CLI not found in PATH.")
        print("Install it first: https://infisical.com/docs/cli/overview")
        sys.exit(1)

    proc = _run_infisical(["login", "status"])
    if proc.returncode != 0:
        print("infisical CLI is not authenticated.")
        print("Run 'infisical login' first.")
        sys.exit(1)


def _parse_json(proc: subprocess.CompletedProcess[str], what: str) -> list:
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        print(f"Could not parse infisical {what} output: {e}")
        sys.exit(1)
    return data if isinstance(data, list) else []


def _list_provider_folders(project_id: str, env: str) -> list[str]:
    """List folder names under PROVIDERS_FOLDER; each name is a provider."""
    proc = _run_infisical([
        "secrets", "folders", "get",
        "--path", PROVIDERS_FOLDER,
        "-o", "json",
        f"--projectId={project_id}",
        f"--env={env}",
        "--silent",
    ])
    if proc.returncode != 0:
        print(f"Could not list folders under {PROVIDERS_FOLDER}:")
        print((proc.stderr or proc.stdout).strip())
        sys.exit(1)
    return [str(f["folderName"]) for f in _parse_json(proc, "folders") if "folderName" in f]


def _fetch_folder_secrets(project_id: str, env: str, folder: str) -> dict[str, str]:
    proc = _run_infisical([
        "export",
        "--format=json",
        f"--env={env}",
        f"--projectId={project_id}",
        f"--path={PROVIDERS_FOLDER}/{folder}",
        "--expand=false",
        "--silent",
    ])
    if proc.returncode != 0:
        print(f"Failed to fetch secrets from {PROVIDERS_FOLDER}/{folder}:")
        print((proc.stderr or proc.stdout).strip())
        sys.exit(1)

    secrets: dict[str, str] = {}
    for entry in _parse_json(proc, "export"):
        if isinstance(entry, dict) and "key" in entry:
            secrets[str(entry["key"])] = str(entry.get("value", ""))
    return secrets


def sync_pull(config_path: str, dry_run: bool = False) -> None:
    """Pull provider api_keys from Infisical into the flexgate config.

    Provider mapping is derived from the Infisical layout: a folder named
    after the provider under /providers, holding an API_KEY secret (an
    optional MODELS secret lists comma-separated model names; without one,
    the registry's latest models for the matched prefix are used). Folders
    with no matching provider in the config are auto-imported when their
    name starts with a known prefix (see KNOWN_BASE_URLS and
    KNOWN_DEFAULT_MODELS in registry.py); otherwise a warning is printed.
    """
    _check_prerequisites()

    config = load_config(config_path)
    inf = config.infisical
    if not inf.project_id:
        print("No Infisical project configured.")
        print(f"Add an 'infisical' section to {config_path}:")
        print()
        print("  infisical:")
        print("    project_id: \"<your-project-id>\"")
        print("    env: \"dev\"")
        sys.exit(1)

    if not config.providers:
        print("No providers configured; nothing to sync.")
        return

    print(f"Fetching secrets from Infisical (project {inf.project_id}, env {inf.env})...")
    folders = _list_provider_folders(inf.project_id, inf.env)

    updated: list[str] = []
    unchanged: list[str] = []
    missing: list[str] = []

    for name, prov in config.providers.items():
        if name not in folders:
            missing.append(name)
            continue
        new_key = _fetch_folder_secrets(inf.project_id, inf.env, name).get("API_KEY", "")
        if new_key and new_key != prov.api_key:
            prov.api_key = new_key
            updated.append(name)
        else:
            unchanged.append(name)

    # Providers that exist in Infisical but not in the local config: try to
    # auto-import them by matching the folder name against known prefixes.
    base_url_table = dict(KNOWN_BASE_URLS)
    base_url_table.update({n: p.base_url for n, p in config.providers.items()})
    models_table = dict(KNOWN_DEFAULT_MODELS)
    models_table.update({n: p.available_models for n, p in config.providers.items() if p.available_models})

    imported: list[str] = []
    unknown: list[str] = []

    for name in [f for f in folders if f not in config.providers]:
        base_url = _match_base_url(name, base_url_table)
        if base_url is None:
            unknown.append(name)
            continue
        secrets = _fetch_folder_secrets(inf.project_id, inf.env, name)
        api_key = secrets.get("API_KEY", "")
        if not api_key:
            unknown.append(name)
            continue
        models = [m.strip() for m in secrets.get("MODELS", "").split(",") if m.strip()]
        if not models:
            # No MODELS secret: fall back to the registry's latest models
            # for the matched prefix.
            models = _match_models(name, models_table)
        config.providers[name] = ProviderConfig(
            name=name,
            base_url=base_url,
            api_key=api_key,
            available_models=models,
        )
        imported.append(name)

    for name in updated:
        print(f"  {name:16s} updated  (key: {_mask_key(config.providers[name].api_key)})")
    for name in imported:
        print(f"  {name:16s} imported (new provider, base_url: {config.providers[name].base_url})")
    for name in unchanged:
        print(f"  {name:16s} unchanged")
    for name in missing:
        print(f"  {name:16s} no folder {PROVIDERS_FOLDER}/{name} in Infisical")
    for name in unknown:
        print(f"  WARNING: {name} has no known prefix and was skipped.")
        print(f"           Add it to the config manually, or register its prefix in flexgate/registry.py.")

    changed = updated or imported
    if not changed:
        print("\nAll keys are up to date.")
        return

    if dry_run:
        n = len(updated) + len(imported)
        print(f"\nDry run: {n} provider(s) would be updated/imported. No changes written.")
        return

    save_config(config, config_path)
    print(f"\nUpdated {len(updated)} key(s), imported {len(imported)} provider(s). Saved: {config_path}")

    # Hot-reload the authoritative systemd service, if it is active.
    from flexgate.service import reload_service_if_active

    msg = reload_service_if_active(config_path)
    if msg:
        print(msg)
