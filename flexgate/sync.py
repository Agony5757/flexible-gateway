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
from flexgate.ui import cyan, dim, err, ok, yellow

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
        err("The 'confsync' client package is missing from flexgate's environment.")
        print("Reinstall/upgrade flexgate to fix it (it is a declared dependency):", file=sys.stderr)
        print(f"  {dim('uv tool install --force -e .')}        # from the flexgate repo", file=sys.stderr)
        print(f"or inject it manually:  {dim('uv tool inject flexgate confsync-client')}", file=sys.stderr)
        sys.exit(1)


def _get_client(as_json: bool = False):
    """Build a confsync client from the shared confsync credentials."""
    from flexgate.ui import json_fail

    confsync, credentials = _import_confsync()
    try:
        client = credentials.load_client()
    except confsync.ConfsyncError as e:
        if as_json:
            json_fail(str(e))
        err(str(e), 1)
    return client, DEFAULT_APP


def sync_push(config_path: str, as_json: bool = False) -> dict | None:
    """Upload the local config.yaml to the confsync server."""
    from flexgate.ui import emit_json, json_fail

    if not os.path.exists(config_path):
        message = f"No config at {config_path} — nothing to push."
        if as_json:
            json_fail(message)
        err(message, 1)
    with open(config_path, encoding="utf-8") as f:
        content = f.read()

    client, app = _get_client(as_json)
    confsync, _ = _import_confsync()
    with client:
        try:
            version = client.push(app, DOC_NAME, content)
        except confsync.ConfsyncError as e:
            if as_json:
                json_fail(str(e))
            err(str(e), 1)
    report = {
        "action": "push",
        "server": client.server_url,
        "document": f"{app}/{DOC_NAME}",
        "version": version,
    }
    if as_json:
        emit_json({"ok": True, **report})
        return None
    ok(f"pushed {cyan(config_path)} → {dim(f'{app}/{DOC_NAME} v{version} on {client.server_url}')}")
    return report


def _bootstrap_pull(client, app: str, config_path: str, remote_text: str, as_json: bool = False) -> dict:
    ensure_home_dir()
    with open(config_path, "w", encoding="utf-8") as f:
        f.write(remote_text)
    report = {"result": "bootstrapped", "backup": None}
    if as_json:
        return report
    ok(f"bootstrapped {cyan(config_path)} from {dim(f'{app}/{DOC_NAME} ({client.server_url})')}")
    return report


def _full_pull(client, app: str, config_path: str, remote_text: str, as_json: bool = False) -> dict:
    backup = f"{config_path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(config_path, backup)
    with open(config_path, "w", encoding="utf-8") as f:
        f.write(remote_text)
    report = {"result": "replaced", "backup": backup}
    if as_json:
        return report
    ok(f"replaced {cyan(config_path)} with {dim(f'{app}/{DOC_NAME}')} (backup: {dim(backup)})")
    return report


def sync_pull(config_path: str, dry_run: bool = False, as_json: bool = False) -> dict | None:
    """Pull the remote config: replace the local file entirely (backup first)."""
    from flexgate.ui import emit_json, json_fail

    client, app = _get_client(as_json)
    confsync, _ = _import_confsync()
    with client:
        try:
            remote_text = client.pull(app, DOC_NAME)
        except confsync.ConfsyncError as e:
            if as_json:
                json_fail(str(e))
            err(str(e), 1)

    report: dict = {
        "action": "pull",
        "server": client.server_url,
        "document": f"{app}/{DOC_NAME}",
    }

    def finish(payload: dict) -> dict:
        if as_json:
            emit_json({"ok": True, **report, **payload})
            return None
        return {**report, **payload}

    if not os.path.exists(config_path):
        if dry_run:
            payload = {"result": "would-bootstrap"}
            if not as_json:
                print(f"{yellow('Dry run:')} would bootstrap {config_path} from {app}/{DOC_NAME}.")
            return finish(payload)
        payload = _bootstrap_pull(client, app, config_path, remote_text, as_json)
        return finish(payload)

    if dry_run:
        payload = {"result": "would-replace"}
        if not as_json:
            print(f"{yellow('Dry run:')} would replace {config_path} with {app}/{DOC_NAME}.")
        return finish(payload)
    payload = _full_pull(client, app, config_path, remote_text, as_json)
    from flexgate.service import reload_service_if_active
    msg = reload_service_if_active(config_path)
    finish({**payload, "reload": msg})
    if msg and not as_json:
        print(msg)
    return {**report, **payload, "reload": msg} if not as_json else None
