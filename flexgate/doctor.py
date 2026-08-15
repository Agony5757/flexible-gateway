"""``flexgate doctor`` — diagnose common installation and config problems.

Runs a series of read-only checks and prints OK / WARN / FAIL per item.
Exit code is 1 when any check FAILs, so it can gate a release or CI job.
It never modifies anything; fixes are applied by ``flexgate update``.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from dataclasses import dataclass

from flexgate import __version__
from flexgate.config import load_config
from flexgate.migrate import (
    CURRENT_CONFIG_VERSION,
    detect_config_version,
    pending_steps,
    read_raw_config,
)
from flexgate.update import fetch_latest_version

OK, WARN, FAIL, SKIP = "OK", "WARN", "FAIL", "SKIP"


@dataclass
class Finding:
    status: str
    label: str
    detail: str = ""


def _check_python(findings: list[Finding]) -> None:
    v = sys.version_info
    if v >= (3, 11):
        findings.append(Finding(OK, "Python", f"{v.major}.{v.minor}.{v.micro}"))
    else:
        findings.append(Finding(FAIL, "Python", f"{v.major}.{v.minor}.{v.micro} — flexgate requires >= 3.11"))


def _check_package(findings: list[Finding], offline: bool) -> None:
    if offline:
        findings.append(Finding(SKIP, "Package update", "skipped (--offline)"))
        return
    latest = fetch_latest_version()
    if latest is None:
        findings.append(Finding(SKIP, "Package update", "PyPI unreachable"))
    elif latest == __version__:
        findings.append(Finding(OK, "Package update", f"{__version__} is the latest release"))
    else:
        findings.append(Finding(WARN, "Package update",
                                f"{__version__} → {latest} available — run 'flexgate update'"))


def _load_validated_config(config_path: str, findings: list[Finding]):
    """Add findings for config existence/parse/schema. Returns GatewayConfig or None."""
    if not os.path.exists(config_path):
        findings.append(Finding(FAIL, "Config file",
                                f"{config_path} not found — run 'flexgate config init'"))
        return None
    findings.append(Finding(OK, "Config file", config_path))

    try:
        raw = read_raw_config(config_path)
    except Exception as e:
        findings.append(Finding(FAIL, "Config YAML", f"cannot parse: {e}"))
        return None

    try:
        version = detect_config_version(raw)
    except ValueError as e:
        findings.append(Finding(FAIL, "Config schema", str(e)))
        return None

    if version > CURRENT_CONFIG_VERSION:
        findings.append(Finding(FAIL, "Config schema",
                                f"v{version} is newer than this flexgate (v{CURRENT_CONFIG_VERSION}) "
                                f"— upgrade flexgate first: flexgate update"))
        return None
    steps = pending_steps(version)
    if steps:
        findings.append(Finding(WARN, "Config schema",
                                f"v{version} is outdated — run 'flexgate update' "
                                f"to migrate to v{CURRENT_CONFIG_VERSION}"))
    else:
        findings.append(Finding(OK, "Config schema", f"v{version} (current)"))

    try:
        config = load_config(config_path)
    except Exception as e:
        findings.append(Finding(FAIL, "Config semantics", str(e)))
        return None
    findings.append(Finding(OK, "Config semantics", "providers/routes validate"))
    return config


def _check_providers(config, findings: list[Finding]) -> None:
    from flexgate.healthcheck import _is_placeholder_key

    if not config.providers:
        findings.append(Finding(FAIL, "Providers", "none configured"))
        return

    placeholders = [name for name, p in config.providers.items() if _is_placeholder_key(p.api_key)]
    if placeholders:
        findings.append(Finding(WARN, "Provider keys",
                                f"placeholder api_key in: {', '.join(placeholders)}"))
    else:
        findings.append(Finding(OK, "Provider keys", f"{len(config.providers)} provider(s) configured"))


def _check_routes(config, findings: list[Finding]) -> None:
    if not config.routes:
        findings.append(Finding(WARN, "Routes", "no routes — unmatched models will 404"))
        return
    catch_all_idx = [i for i, r in enumerate(config.routes) if r.pattern.pattern == ".*"]
    if catch_all_idx and catch_all_idx[0] < len(config.routes) - 1:
        shadowed = [r.pattern.pattern for r in config.routes[catch_all_idx[0] + 1:]]
        findings.append(Finding(WARN, "Routes",
                                f"catch-all '.*' is not last; unreachable: {', '.join(shadowed)}"))
    elif not catch_all_idx:
        findings.append(Finding(WARN, "Routes", "no catch-all '.*' route — unmatched models will 404"))
    else:
        findings.append(Finding(OK, "Routes", f"{len(config.routes)} route(s), catch-all last"))


def _check_port(config, findings: list[Finding]) -> None:
    host, port = config.server.host, config.server.port
    with socket.socket() as sock:
        sock.settimeout(1.0)
        occupied = sock.connect_ex((host, port)) == 0

    from flexgate.service import service_active, service_installed

    if occupied:
        if service_active():
            findings.append(Finding(OK, "Port", f"{host}:{port} serving (service active)"))
        else:
            findings.append(Finding(WARN, "Port",
                                    f"{host}:{port} is in use but the flexgate service is not active "
                                    f"— another process may hold it"))
    else:
        if service_installed() and not service_active():
            findings.append(Finding(WARN, "Port",
                                    f"service installed but not running — 'flexgate service start'"))
        else:
            findings.append(Finding(OK, "Port", f"{host}:{port} free"))


def _check_systemd(findings: list[Finding]) -> None:
    from flexgate.service import (
        UNIT_MARKER,
        _systemd_user_available,
        _unit_path,
        service_active,
        service_installed,
    )

    available, reason = _systemd_user_available()
    if not available:
        findings.append(Finding(WARN, "systemd", reason))
        return
    if not service_installed():
        findings.append(Finding(WARN, "systemd service",
                                "not installed — 'flexgate service install'"))
        return
    try:
        with open(_unit_path()) as f:
            content = f.read()
        if UNIT_MARKER not in content:
            findings.append(Finding(WARN, "systemd service",
                                    "unit predates the current format — reinstall with "
                                    "'flexgate service install'"))
            return
    except OSError as e:
        findings.append(Finding(FAIL, "systemd service", f"cannot read unit file: {e}"))
        return
    if service_active():
        findings.append(Finding(OK, "systemd service", "installed and active"))
    else:
        findings.append(Finding(WARN, "systemd service",
                                "installed but not active — 'flexgate service start'"))


def _check_claude_settings(config, findings: list[Finding]) -> None:
    settings_path = os.path.expanduser("~/.claude/settings.json")
    if not os.path.exists(settings_path):
        findings.append(Finding(SKIP, "Claude settings", "~/.claude/settings.json not found"))
        return
    try:
        with open(settings_path) as f:
            settings = json.load(f)
    except Exception as e:
        findings.append(Finding(WARN, "Claude settings", f"cannot parse: {e}"))
        return
    base_url = (settings.get("env") or {}).get("ANTHROPIC_BASE_URL", "")
    expected = f"http://{config.server.host}:{config.server.port}"
    if base_url.rstrip("/") == expected:
        findings.append(Finding(OK, "Claude settings", f"ANTHROPIC_BASE_URL → {expected}"))
    else:
        findings.append(Finding(WARN, "Claude settings",
                                f"ANTHROPIC_BASE_URL is {base_url!r}, expected {expected!r} "
                                f"— 'flexgate settings apply'"))


def run_doctor(config_path: str, *, offline: bool = False) -> int:
    print(f"flexgate doctor — version {__version__}, config schema v{CURRENT_CONFIG_VERSION}\n")

    findings: list[Finding] = []
    _check_python(findings)
    _check_package(findings, offline)

    config = _load_validated_config(config_path, findings)
    if config is not None:
        _check_providers(config, findings)
        _check_routes(config, findings)
        _check_port(config, findings)
        _check_claude_settings(config, findings)
    _check_systemd(findings)

    width = max(len(f.label) for f in findings)
    for f in findings:
        line = f"[{f.status:4s}] {f.label:{width}s}"
        if f.detail:
            line += f"  {f.detail}"
        print(line)

    fails = sum(1 for f in findings if f.status == FAIL)
    warns = sum(1 for f in findings if f.status == WARN)
    print()
    if fails:
        print(f"{fails} problem(s) must be fixed; {warns} warning(s).")
        return 1
    if warns:
        print(f"No blocking problems; {warns} warning(s). 'flexgate update' fixes outdated versions.")
    else:
        print("Everything looks healthy.")
    return 0
