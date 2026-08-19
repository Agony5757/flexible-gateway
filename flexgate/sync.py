"""confsync config sync — push/pull ~/.flexgate/config.yaml via a confsync server.

Connection details come from the shared confsync credentials
(``confsync login --server <url>`` → ~/.confsync/credentials.json); the
optional ``confsync:`` section in config.yaml only overrides the server URL
and the document's app name (default app: "flexgate", name: "config.yaml").

Pull semantics:
  * local config missing → bootstrap: the remote document becomes the config
  * local config present → merge: api_keys of matching providers are updated,
    providers present only remotely are imported; local routes/schedules are
    untouched (``--full`` replaces the whole file instead, with a backup)
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time

from flexgate.config import ProviderConfig, ensure_home_dir, load_config, save_config

DOC_NAME = "config.yaml"
DEFAULT_APP = "flexgate"


def _import_confsync():
    try:
        import confsync  # noqa: PLC0415
        from confsync import credentials  # noqa: PLC0415
        return confsync, credentials
    except ImportError:
        # confsync-client is a declared dependency; reaching here means the
        # installation is broken (e.g. an editable install predating the dep).
        print("The 'confsync' client package is missing from flexgate's environment.")
        print("Reinstall/upgrade flexgate to fix it (it is a declared dependency):")
        print("  uv tool install --force -e .        # from the flexgate repo")
        print("or inject it manually:  uv tool inject flexgate confsync-client")
        sys.exit(1)


def _mask_key(key: str) -> str:
    if len(key) <= 8:
        return "***"
    return key[:4] + "***" + key[-4:]


def _get_client(config_path: str):
    """Build a confsync client: shared credentials, optionally overridden by
    the config's confsync: section (server_url / app)."""
    confsync, credentials = _import_confsync()

    server_url_override, app = "", DEFAULT_APP
    if os.path.exists(config_path):
        cfg = load_config(config_path)
        server_url_override = cfg.confsync.server_url
        app = cfg.confsync.app or DEFAULT_APP

    try:
        client = credentials.load_client()
    except confsync.ConfsyncError as e:
        print(str(e))
        sys.exit(1)

    if server_url_override and server_url_override.rstrip("/") != client.server_url:
        creds_path = credentials.get_credentials_path()
        with open(creds_path) as f:
            api_key = json.load(f)["api_key"]
        client.close()
        try:
            client = confsync.ConfsyncClient(server_url_override, api_key)
        except confsync.ConfsyncError as e:
            print(str(e))
            sys.exit(1)

    return client, app


def sync_push(config_path: str) -> None:
    """Upload the local config.yaml to the confsync server."""
    if not os.path.exists(config_path):
        print(f"No config at {config_path} — nothing to push.")
        sys.exit(1)
    with open(config_path, encoding="utf-8") as f:
        content = f.read()

    client, app = _get_client(config_path)
    confsync, _ = _import_confsync()
    with client:
        try:
            version = client.push(app, DOC_NAME, content)
        except confsync.ConfsyncError as e:
            print(str(e))
            sys.exit(1)
    print(f"Pushed {config_path} → {app}/{DOC_NAME} v{version} on {client.server_url}")


def _bootstrap_pull(client, app: str, config_path: str, remote_text: str) -> None:
    ensure_home_dir()
    with open(config_path, "w", encoding="utf-8") as f:
        f.write(remote_text)
    print(f"Bootstrapped {config_path} from {app}/{DOC_NAME} ({client.server_url}).")


def _full_pull(client, app: str, config_path: str, remote_text: str) -> None:
    backup = f"{config_path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(config_path, backup)
    with open(config_path, "w", encoding="utf-8") as f:
        f.write(remote_text)
    print(f"Replaced {config_path} with {app}/{DOC_NAME} (backup: {backup}).")


def sync_pull(config_path: str, dry_run: bool = False, full: bool = False) -> None:
    """Pull the remote config: bootstrap, full replace (--full), or key merge."""
    import yaml

    client, app = _get_client(config_path)
    confsync, _ = _import_confsync()
    with client:
        try:
            remote_text = client.pull(app, DOC_NAME)
        except confsync.ConfsyncError as e:
            print(str(e))
            sys.exit(1)

    if not os.path.exists(config_path):
        if dry_run:
            print(f"Dry run: would bootstrap {config_path} from {app}/{DOC_NAME}.")
            return
        _bootstrap_pull(client, app, config_path, remote_text)
        return

    if full:
        if dry_run:
            print(f"Dry run: would replace {config_path} with {app}/{DOC_NAME}.")
            return
        _full_pull(client, app, config_path, remote_text)
        from flexgate.service import reload_service_if_active
        msg = reload_service_if_active(config_path)
        if msg:
            print(msg)
        return

    # ── merge mode: keys + new providers, local routes/schedules kept ──
    try:
        remote_raw = yaml.safe_load(remote_text)
    except yaml.YAMLError as e:
        print(f"Remote document {app}/{DOC_NAME} is not valid YAML: {e}")
        sys.exit(1)
    if not isinstance(remote_raw, dict):
        print(f"Remote document {app}/{DOC_NAME} is not a config mapping.")
        sys.exit(1)
    remote_providers = remote_raw.get("providers", {}) or {}

    config = load_config(config_path)
    updated: list[str] = []
    imported: list[str] = []
    unchanged: list[str] = []

    for name, rp in remote_providers.items():
        remote_key = str(rp.get("api_key", ""))
        if name in config.providers:
            prov = config.providers[name]
            if remote_key and remote_key != prov.api_key:
                prov.api_key = remote_key
                updated.append(name)
            else:
                unchanged.append(name)
        else:
            base_url = str(rp.get("base_url", ""))
            if not (base_url and remote_key):
                print(f"  WARNING: remote provider '{name}' lacks base_url/api_key, skipped.")
                continue
            config.providers[name] = ProviderConfig(
                name=name,
                base_url=base_url.rstrip("/"),
                api_key=remote_key,
                available_models=[str(m) for m in (rp.get("available_models") or [])],
            )
            imported.append(name)

    for name in updated:
        print(f"  {name:16s} updated  (key: {_mask_key(config.providers[name].api_key)})")
    for name in imported:
        print(f"  {name:16s} imported (new provider, base_url: {config.providers[name].base_url})")
    for name in unchanged:
        print(f"  {name:16s} unchanged")
    for name in [n for n in config.providers if n not in remote_providers]:
        print(f"  {name:16s} local-only (not in remote {app}/{DOC_NAME})")

    if not (updated or imported):
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
