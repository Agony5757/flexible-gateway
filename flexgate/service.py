"""Manage flexgate as a systemd *user* service.

`flexgate service install` writes a unit to ~/.config/systemd/user/flexgate.service
that runs the gateway in the foreground (`flexgate gateway run`) under systemd's
supervision (auto-restart, start on login). This is an alternative to the
PID-file daemon started by `flexgate gateway start` — use one or the other,
since both bind the same port.

Linux/systemd only. On systems without a systemd user instance, the commands
print a helpful message and exit non-zero.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

from flexgate.config import load_config

SERVICE_NAME = "flexgate.service"


# ── paths ──────────────────────────────────────────────────────────

def _systemd_user_dir() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "systemd", "user")


def _unit_path() -> str:
    return os.path.join(_systemd_user_dir(), SERVICE_NAME)


# ── systemd availability ───────────────────────────────────────────

def _systemd_user_available() -> tuple[bool, str]:
    if shutil.which("systemctl") is None:
        return False, "systemctl not found — this feature requires systemd (Linux)."
    try:
        r = subprocess.run(
            ["systemctl", "--user", "is-system-running"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"Could not query the systemd user instance: {e}"

    state = (r.stdout or "").strip()
    err = (r.stderr or "").lower()
    if "failed to connect to bus" in err or "no medium" in err:
        return False, "No systemd user session bus available (try `systemctl --user`)."
    if state in ("offline", "unknown") and r.returncode != 0:
        return False, f"systemd user instance not available (state: {state or 'unknown'})."
    return True, ""


def _ensure_available() -> None:
    ok, reason = _systemd_user_available()
    if not ok:
        print(reason)
        print("\n'flexgate service' requires a systemd user instance (Linux).")
        print("Alternative: run the gateway directly with 'flexgate gateway start'.")
        sys.exit(1)


def _systemctl(*args: str, capture: bool = False) -> subprocess.CompletedProcess:
    cmd = ["systemctl", "--user", *args]
    return subprocess.run(cmd, text=True, capture_output=capture)


# ── unit generation ────────────────────────────────────────────────

def _q(token: str) -> str:
    """Quote a token for a systemd ExecStart line if it contains whitespace."""
    if any(c.isspace() for c in token):
        escaped = token.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return token


def _resolve_exec(config_path: str) -> str:
    """Build the absolute ExecStart command line that runs the gateway foreground."""
    config_abs = os.path.abspath(config_path)

    exe: str | None = None
    argv0 = sys.argv[0] if sys.argv else ""
    if (
        argv0
        and os.path.basename(argv0).startswith("flexgate")
        and os.path.exists(os.path.abspath(argv0))
    ):
        exe = os.path.abspath(argv0)
    if exe is None:
        which = shutil.which("flexgate")
        if which:
            exe = which

    if exe is not None:
        return " ".join([_q(exe), "--config", _q(config_abs), "gateway", "run"])

    # Fallback: run the module with the current interpreter.
    python = os.path.abspath(sys.executable)
    return " ".join([_q(python), "-m", "flexgate.main", "--config", _q(config_abs)])


def _unit_content(config_path: str) -> str:
    exec_start = _resolve_exec(config_path)
    config_abs = os.path.abspath(config_path)
    return f"""\
[Unit]
Description=Flexgate — local Anthropic API gateway for Claude Code
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=FLEXGATE_CONFIG={config_abs}
ExecStart={exec_start}
ExecReload=/bin/kill -USR1 $MAINPID
Restart=on-failure
RestartSec=3
SyslogIdentifier=flexgate

[Install]
WantedBy=default.target
"""


# ── linger (keep service running without an active login) ──────────

def _enable_linger() -> None:
    if shutil.which("loginctl") is None:
        return
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    cmd = ["loginctl", "enable-linger"]
    if user:
        cmd.append(user)
    try:
        subprocess.run(cmd, check=False, capture_output=True, text=True)
    except OSError:
        pass


# ── port-conflict pre-check (reuses the guardian) ──────────────────

def _warn_if_port_busy(config_path: str) -> None:
    try:
        cfg = load_config(config_path)
        port = cfg.server.port
    except Exception:
        return
    try:
        from flexgate.guardian import check_port_owner
    except Exception:
        return
    owner = check_port_owner(port)
    if owner:
        print(
            f"Warning: port {port} is already in use by PID {owner.pid} ({owner.command}).\n"
            f"         The service may fail to start. If that PID is a flexgate gateway\n"
            f"         started with 'flexgate gateway start', stop it first:\n"
            f"           flexgate gateway stop"
        )


# ── commands ───────────────────────────────────────────────────────

def service_install(config_path: str, start: bool = True) -> None:
    _ensure_available()

    if not os.path.exists(config_path):
        print(f"Config not found: {config_path}")
        print("Run 'flexgate config init' first.")
        sys.exit(1)
    try:
        load_config(config_path)
    except Exception as e:
        print(f"Config error: {e}")
        print("Fix the config before installing the service.")
        sys.exit(1)

    unit_dir = _systemd_user_dir()
    os.makedirs(unit_dir, exist_ok=True)
    unit_path = _unit_path()
    with open(unit_path, "w") as f:
        f.write(_unit_content(config_path))
    print(f"Wrote unit: {unit_path}")

    _systemctl("daemon-reload")

    r = _systemctl("enable", SERVICE_NAME, capture=True)
    if r.returncode == 0:
        print(f"Enabled {SERVICE_NAME} (will start on login)")
    elif r.stderr:
        print(r.stderr.strip())

    _enable_linger()

    if start:
        _warn_if_port_busy(config_path)
        r = _systemctl("restart", SERVICE_NAME, capture=True)
        if r.returncode == 0:
            print(f"Started {SERVICE_NAME}")
        else:
            print("Failed to start service:")
            if r.stderr:
                print(r.stderr.strip())
            print("Inspect logs with: journalctl --user -u flexgate -e")
    else:
        print("Service installed but not started (use 'flexgate service start').")

    print()
    _print_status_brief()
    print(
        "\nNote: the systemd service and 'flexgate gateway start' both bind the same "
        "port.\n      Use one or the other, not both."
    )


def service_uninstall() -> None:
    _ensure_available()

    _systemctl("stop", SERVICE_NAME, capture=True)
    _systemctl("disable", SERVICE_NAME, capture=True)

    unit_path = _unit_path()
    if os.path.exists(unit_path):
        os.remove(unit_path)
        print(f"Removed unit: {unit_path}")
    else:
        print(f"No unit file at {unit_path}")

    _systemctl("daemon-reload")
    _systemctl("reset-failed", SERVICE_NAME, capture=True)
    print(f"{SERVICE_NAME} uninstalled.")


def _require_installed() -> None:
    if not os.path.exists(_unit_path()):
        print(f"Service not installed (no unit at {_unit_path()}).")
        print("Run 'flexgate service install' first.")
        sys.exit(1)


def service_start() -> None:
    _ensure_available()
    _require_installed()
    r = _systemctl("start", SERVICE_NAME, capture=True)
    if r.returncode == 0:
        print(f"Started {SERVICE_NAME}")
        _print_status_brief()
    else:
        print("Failed to start service:")
        if r.stderr:
            print(r.stderr.strip())
        print("Inspect logs with: journalctl --user -u flexgate -e")
        sys.exit(1)


def service_stop() -> None:
    _ensure_available()
    r = _systemctl("stop", SERVICE_NAME, capture=True)
    if r.returncode == 0:
        print(f"Stopped {SERVICE_NAME}")
    else:
        if r.stderr:
            print(r.stderr.strip())
        sys.exit(1)


def _print_status_brief() -> None:
    active = _systemctl("is-active", SERVICE_NAME, capture=True).stdout.strip()
    enabled = _systemctl("is-enabled", SERVICE_NAME, capture=True).stdout.strip()
    print(f"Service: {SERVICE_NAME}  active={active or 'unknown'}  enabled={enabled or 'unknown'}")


def service_status() -> None:
    _ensure_available()
    unit_path = _unit_path()
    if not os.path.exists(unit_path):
        print(f"Service not installed (no unit at {unit_path}).")
        print("Run 'flexgate service install' to install it.")
        return
    print(f"Unit: {unit_path}\n")
    # Stream systemctl status directly; it exits non-zero when inactive, which
    # is fine — we don't propagate that as a hard error.
    _systemctl("status", SERVICE_NAME, "--no-pager")


def service_help() -> None:
    print(
        """flexgate service — run flexgate as a systemd user service (Linux)

Usage:
  flexgate service install [--no-start]   Install + enable the user service (and start it)
  flexgate service uninstall              Stop, disable and remove the service
  flexgate service start                  Start the service
  flexgate service stop                   Stop the service
  flexgate service status                 Show service status
  flexgate service help                   Show this help

Details:
  • The unit is written to ~/.config/systemd/user/flexgate.service and runs
    'flexgate gateway run' under systemd (Type=simple, Restart=on-failure).
  • 'install' also runs 'loginctl enable-linger' so the gateway keeps running
    without an active login session and starts at boot.
  • Reload config without restarting:  systemctl --user reload flexgate
    (sends SIGUSR1, same as editing config + 'flexgate config set').
  • View logs:                         journalctl --user -u flexgate -e

Note:
  The systemd service and 'flexgate gateway start' both bind the same port.
  Use one or the other, not both.
"""
    )
