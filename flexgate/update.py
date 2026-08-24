"""Package self-update via pip/pipx/uv and config migration.

``flexgate update`` handles both halves of an upgrade:

1. package — detect how flexgate was installed (pipx / uv tool / pip) and
   upgrade to the latest release published on PyPI;
2. config — apply pending config.yaml schema migrations (with backup).
   When the package was just upgraded, this half is delegated to a fresh
   process so the migration runs with the NEW code's schema version,
   not the stale one still loaded in this process.

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
from flexgate.ui import bold, dim, green, ok, red, yellow

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


# ── cached background version check ───────────────────────────────
#
# Interactive commands (bare `flexgate`, `service status`) show a one-line
# upgrade hint when PyPI carries a newer release. The PyPI lookup is cached
# on disk so it hits the network at most once per UPDATE_CHECK_INTERVAL and
# never slows down or breaks the CLI (every failure is silent).

UPDATE_CHECK_INTERVAL = 24 * 3600  # seconds between PyPI lookups


def _update_check_cache_path() -> str:
    import os

    from flexgate.config import FLEXGATE_HOME
    return os.path.join(FLEXGATE_HOME, "update-check.json")


def latest_version_cached(interval: float = UPDATE_CHECK_INTERVAL) -> str | None:
    """Latest PyPI version, cached on disk; None on any failure."""
    import os
    import time

    path = _update_check_cache_path()
    try:
        with open(path) as f:
            cache = json.load(f)
        if time.time() - float(cache.get("checked_at", 0)) < interval:
            return cache.get("latest") or None
    except Exception:
        pass

    latest = fetch_latest_version(timeout=1.5)
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"checked_at": time.time(), "latest": latest}, f)
        os.replace(tmp, path)
    except Exception:
        pass
    return latest


def update_notice() -> str | None:
    """One-line upgrade hint when PyPI has a newer release, else None."""
    try:
        latest = latest_version_cached()
    except Exception:
        return None
    if latest and parse_version(latest) > parse_version(__version__):
        return (f"A newer flexgate {latest} is available "
                f"(installed {__version__}) — run: flexgate update")
    return None


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

    print(bold(f"flexgate {__version__}") + dim(f" (config schema v{CURRENT_CONFIG_VERSION})"))
    failures = 0
    package_upgraded = False

    # ── package update ────────────────────────────────────────────
    latest = fetch_latest_version()
    if latest is None:
        print("\n" + bold("Package:") + " could not reach PyPI — skipping package update check.")
    else:
        newer = parse_version(latest) > parse_version(__version__)
        print(f"\n{bold('Package:')} installed {__version__}, latest {latest}"
              + (yellow(" — update available") if newer else green(" — up to date")))
        if newer and not config_only:
            installer = detect_installer()
            if installer is None:
                print(red("  Could not detect how flexgate was installed; upgrade manually."))
                failures += 1
            else:
                label, cmd = installer
                if check:
                    print(f"  {dim('Would run:')} {' '.join(cmd)}")
                else:
                    print(f"  Upgrading via {bold(label)}: {dim(' '.join(cmd))}")
                    proc = subprocess.run(cmd)
                    if proc.returncode != 0:
                        print(red(f"  Package upgrade failed (exit {proc.returncode})."))
                        failures += 1
                    else:
                        ok(f"upgraded to {latest}. Restart the service to use it:")
                        print(f"    {dim('flexgate service restart')}")
                        package_upgraded = True

    # ── config migration ──────────────────────────────────────────
    print()
    if package_upgraded and not os.environ.get("FLEXGATE_UPDATE_DELEGATED"):
        # This process still runs the OLD code: its CURRENT_CONFIG_VERSION and
        # MIGRATIONS chain predate the release just installed, so an outdated
        # config would be misreported as current (e.g. "schema v2 is current"
        # right after upgrading to a v4 release). Hand the migration to the
        # new code in a fresh process. The env guard prevents re-delegation
        # if the upgrade somehow left an older version installed.
        cmd = [sys.executable, "-m", "flexgate", "--config", config_path, "update", "--config-only"]
        env = dict(os.environ, FLEXGATE_UPDATE_DELEGATED="1")
        proc = subprocess.run(cmd, env=env)
        return proc.returncode or (1 if failures else 0)

    if not os.path.exists(config_path):
        print(f"{bold('Config:')} {config_path} not found — nothing to migrate.")
        return 1 if failures else 0

    status = _config_status(config_path)
    if status is None:
        print(red(f"Config: could not parse {config_path} — run 'flexgate doctor' for details."))
        return 1

    version, steps = status
    if not steps:
        print(f"{bold('Config:')} schema v{version} is current — nothing to migrate.")
    else:
        print(f"{bold('Config:')} schema v{version} → v{CURRENT_CONFIG_VERSION}, {len(steps)} migration(s) pending.")
        if check:
            for step in steps:
                print(f"  {dim(f'Would apply v{step} → v{step + 1}')}")
        else:
            result = migrate_config_file(config_path)
            for line in result.applied:
                ok(f"applied {dim(line)}")
            if result.backup_path:
                print(f"  Backup: {dim(result.backup_path)}")
            ok(f"migrated {config_path}")

    # ── reload the running service with the migrated config ──────
    if not check:
        from flexgate.service import reload_service_if_active
        msg = reload_service_if_active(config_path)
        if msg:
            print(f"\n{msg}")

    return 1 if failures else 0
