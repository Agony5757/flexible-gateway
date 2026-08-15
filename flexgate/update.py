"""Package self-update via pip/pipx/uv and config migration.

``flexgate update`` handles both halves of an upgrade:

1. package — detect how flexgate was installed (pipx / uv tool / pip) and
   upgrade to the latest release published on PyPI;
2. config — apply pending config.yaml schema migrations (with backup).

Use ``--check`` to only report what would change.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import urllib.request

from flexgate import __version__
from flexgate.migrate import CURRENT_CONFIG_VERSION, migrate_config_file, read_raw_config, detect_config_version, pending_steps

PYPI_JSON_URL = "https://pypi.org/pypi/flexgate/json"


def parse_version(text: str) -> tuple[int, ...]:
    """Parse '1.2.3' into (1, 2, 3); pre-release suffixes are ignored."""
    parts = re.findall(r"\d+", text)
    return tuple(int(p) for p in parts) if parts else (0,)


def fetch_latest_version(timeout: float = 5.0) -> str | None:
    """Latest released version on PyPI, or None when unreachable/offline."""
    try:
        req = urllib.request.Request(PYPI_JSON_URL, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        return str(data["info"]["version"])
    except Exception:
        return None


def detect_installer() -> tuple[str, list[str]] | None:
    """Return (label, upgrade command) for the tool that installed flexgate."""
    if shutil.which("pipx"):
        out = subprocess.run(["pipx", "list", "--short"], capture_output=True, text=True).stdout
        if any(line.split()[0] == "flexgate" for line in out.splitlines() if line.strip()):
            return ("pipx", ["pipx", "upgrade", "flexgate"])
    if shutil.which("uv"):
        out = subprocess.run(["uv", "tool", "list"], capture_output=True, text=True).stdout
        if any(line.split()[0] == "flexgate" for line in out.splitlines() if line.strip()):
            return ("uv tool", ["uv", "tool", "upgrade", "flexgate"])
    return ("pip", [sys.executable, "-m", "pip", "install", "--upgrade", "flexgate"])


def _config_status(config_path: str) -> tuple[int, list[int]] | None:
    """(current_version, pending steps), or None if the config cannot be read."""
    try:
        data = read_raw_config(config_path)
    except Exception:
        return None
    version = detect_config_version(data)
    return version, pending_steps(version)


def run_update(config_path: str, *, check: bool = False, config_only: bool = False) -> int:
    import os

    print(f"flexgate {__version__} (config schema v{CURRENT_CONFIG_VERSION})")
    failures = 0

    # ── package update ────────────────────────────────────────────
    latest = fetch_latest_version()
    if latest is None:
        print("\nPackage: could not reach PyPI — skipping package update check.")
    else:
        newer = parse_version(latest) > parse_version(__version__)
        print(f"\nPackage: installed {__version__}, latest {latest}"
              + (" — update available" if newer else " — up to date"))
        if newer and not config_only:
            installer = detect_installer()
            if installer is None:
                print("  Could not detect how flexgate was installed; upgrade manually.")
                failures += 1
            else:
                label, cmd = installer
                if check:
                    print(f"  Would run: {' '.join(cmd)}")
                else:
                    print(f"  Upgrading via {label}: {' '.join(cmd)}")
                    proc = subprocess.run(cmd)
                    if proc.returncode != 0:
                        print(f"  Package upgrade failed (exit {proc.returncode}).")
                        failures += 1
                    else:
                        print(f"  Upgraded to {latest}. Restart the service to use it:")
                        print("    flexgate service restart")

    # ── config migration ──────────────────────────────────────────
    print()
    if not os.path.exists(config_path):
        print(f"Config: {config_path} not found — nothing to migrate.")
        return 1 if failures else 0

    status = _config_status(config_path)
    if status is None:
        print(f"Config: could not parse {config_path} — run 'flexgate doctor' for details.")
        return 1

    version, steps = status
    if not steps:
        print(f"Config: schema v{version} is current — nothing to migrate.")
    else:
        print(f"Config: schema v{version} → v{CURRENT_CONFIG_VERSION}, {len(steps)} migration(s) pending.")
        if check:
            for step in steps:
                print(f"  Would apply v{step} → v{step + 1}")
        else:
            result = migrate_config_file(config_path)
            for line in result.applied:
                print(f"  Applied {line}")
            if result.backup_path:
                print(f"  Backup: {result.backup_path}")
            print(f"  Migrated: {config_path}")

    # ── reload the running service with the migrated config ──────
    if not check:
        from flexgate.service import reload_service_if_active
        msg = reload_service_if_active(config_path)
        if msg:
            print(f"\n{msg}")

    return 1 if failures else 0
