"""confsync config sync — push/pull ~/.flexgate/config.yaml via a confsync server.

Connection details come from the shared confsync credentials
(``confsync login --server <url>`` → ~/.confsync/credentials.json); the
document is fixed at app "flexgate", name "config.yaml".

Pull semantics: the remote document always replaces the local config
(a timestamped backup is kept first; on a machine without config.yaml the
pull simply bootstraps the file).
"""
from __future__ import annotations

import os
import shutil
import sys
import time

from flexgate.config import ensure_home_dir

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


def _get_client():
    """Build a confsync client from the shared confsync credentials."""
    confsync, credentials = _import_confsync()
    try:
        client = credentials.load_client()
    except confsync.ConfsyncError as e:
        print(str(e))
        sys.exit(1)
    return client, DEFAULT_APP


def sync_push(config_path: str) -> None:
    """Upload the local config.yaml to the confsync server."""
    if not os.path.exists(config_path):
        print(f"No config at {config_path} — nothing to push.")
        sys.exit(1)
    with open(config_path, encoding="utf-8") as f:
        content = f.read()

    client, app = _get_client()
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


def sync_pull(config_path: str, dry_run: bool = False) -> None:
    """Pull the remote config: replace the local file entirely (backup first)."""
    client, app = _get_client()
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

    if dry_run:
        print(f"Dry run: would replace {config_path} with {app}/{DOC_NAME}.")
        return
    _full_pull(client, app, config_path, remote_text)
    from flexgate.service import reload_service_if_active
    msg = reload_service_if_active(config_path)
    if msg:
        print(msg)
