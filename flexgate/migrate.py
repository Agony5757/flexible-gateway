"""Config file schema versioning and automatic migrations.

Every config.yaml carries a ``config_version`` integer. Each release that
changes the config schema bumps :data:`CURRENT_CONFIG_VERSION` and registers
one migration function per version step in :data:`MIGRATIONS`. Upgrading
always walks the chain one step at a time (N → N+1 → … → current), so configs
from any older release are upgraded through every intermediate rule.

``flexgate doctor`` reports pending migrations; ``flexgate update`` applies
them (with a timestamped backup) via :func:`migrate_config_file`.
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass, field
from typing import Callable

CURRENT_CONFIG_VERSION = 5

# ── per-step migration rules ──────────────────────────────────────
# Each rule takes the raw config dict at version N and returns it upgraded
# to version N+1. Rules must be idempotent and must not alter key values.

def _migrate_0_to_1(data: dict) -> dict:
    """Stamp the initial versioned schema.

    Legacy configs (written before versioning existed) have no
    ``config_version`` key. The v1 schema is what 0.1.0 already parses, so
    this step only guarantees the top-level sections exist and records the
    version marker.
    """
    data.setdefault("server", {})
    data.setdefault("providers", {})
    data.setdefault("routes", [])
    return data


def _migrate_1_to_2(data: dict) -> dict:
    """Replace the removed `infisical:` section with `confsync:`.

    Infisical sync was replaced by confsync (self-hosted server). The old
    `infisical` section is dropped; an empty `confsync` section is added —
    connection details come from the shared confsync credentials
    (`confsync login --server ...`), so no values need to be carried over.
    """
    data.pop("infisical", None)
    data.setdefault("confsync", {})
    return data


def _migrate_2_to_3(data: dict) -> dict:
    """Drop the removed `confsync:` section.

    The confsync override section (server_url / app) was removed; sync now
    always uses the shared confsync credentials (`confsync login --server ...`)
    and the default document (app "flexgate", name "config.yaml").
    """
    data.pop("confsync", None)
    return data


def _migrate_3_to_4(data: dict) -> dict:
    """Merge `api_key`/`fallback_keys` into the unified `api_keys` list.

    Providers now declare all keys (primary + fallbacks) in one `api_keys`
    list, whose entries are plain key strings or `{key, note}` mappings.
    Key values are carried over unchanged; old-format configs are still
    accepted by the parser, this step just rewrites the file to the new
    schema.
    """
    for prov in (data.get("providers") or {}).values():
        if not isinstance(prov, dict) or "api_keys" in prov or "api_key" not in prov:
            continue
        keys = [prov.pop("api_key")]
        keys.extend(prov.pop("fallback_keys", None) or [])
        prov["api_keys"] = keys
    return data


def _migrate_4_to_5(data: dict) -> dict:
    """Add the optional per-route `active_key` pointer.

    Routes may now declare `active_key: N` (1-based position in the
    provider's `api_keys`) selecting which key requests on that route start
    from; the proxy advances the pointer on fallback. Existing configs
    default every route to the first key, so there is nothing to rewrite —
    this step only records the version bump.
    """
    return data


# version N → rule upgrading N to N+1
MIGRATIONS: dict[int, Callable[[dict], dict]] = {
    0: _migrate_0_to_1,
    1: _migrate_1_to_2,
    2: _migrate_2_to_3,
    3: _migrate_3_to_4,
    4: _migrate_4_to_5,
}


@dataclass
class MigrationResult:
    path: str
    from_version: int
    to_version: int
    applied: list[str] = field(default_factory=list)  # human-readable steps
    backup_path: str | None = None
    changed: bool = False


def detect_config_version(data: dict) -> int:
    raw = data.get("config_version", 0)
    try:
        version = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"config_version must be an integer, got {raw!r}")
    if version < 0:
        raise ValueError(f"config_version must be >= 0, got {version}")
    return version


def pending_steps(version: int) -> list[int]:
    """Migration steps needed to bring `version` up to CURRENT_CONFIG_VERSION."""
    return [v for v in range(version, CURRENT_CONFIG_VERSION) if v in MIGRATIONS]


def migrate_data(data: dict) -> tuple[dict, list[str]]:
    """Apply all pending migrations in memory. Returns (data, applied steps)."""
    version = detect_config_version(data)
    if version > CURRENT_CONFIG_VERSION:
        raise ValueError(
            f"config_version {version} is newer than this flexgate supports "
            f"({CURRENT_CONFIG_VERSION}) — upgrade flexgate first: flexgate update"
        )
    applied: list[str] = []
    for step in pending_steps(version):
        data = MIGRATIONS[step](data)
        applied.append(f"v{step} → v{step + 1}: {MIGRATIONS[step].__doc__.splitlines()[0] if MIGRATIONS[step].__doc__ else 'migrate'}")
    if applied or "config_version" not in data:
        data["config_version"] = CURRENT_CONFIG_VERSION
    return data, applied


def read_raw_config(path: str) -> dict:
    import yaml

    with open(path) as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict):
        raise ValueError(f"Config file {path} must be a YAML mapping")
    return raw


def migrate_config_file(path: str, *, backup: bool = True) -> MigrationResult:
    """Migrate the config file on disk to CURRENT_CONFIG_VERSION.

    Writes nothing when already current. Backs up to
    ``<path>.bak-YYYYMMDD-HHMMSS`` before rewriting.
    """
    data = read_raw_config(path)
    from_version = detect_config_version(data)
    result = MigrationResult(path=path, from_version=from_version, to_version=CURRENT_CONFIG_VERSION)

    if from_version == CURRENT_CONFIG_VERSION:
        return result
    if from_version > CURRENT_CONFIG_VERSION:
        raise ValueError(
            f"{path}: config_version {from_version} is newer than this flexgate "
            f"supports ({CURRENT_CONFIG_VERSION}) — upgrade flexgate first"
        )

    data, applied = migrate_data(data)

    if backup:
        backup_path = f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(path, backup_path)
        result.backup_path = backup_path

    # Keep config_version at the top for readability.
    out = {"config_version": data.pop("config_version")}
    out.update(data)

    import yaml

    tmp_path = f"{path}.tmp-{os.getpid()}"
    with open(tmp_path, "w") as f:
        yaml.dump(out, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    os.replace(tmp_path, path)

    result.applied = applied
    result.changed = True
    return result
