from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time

from flexgate.config import load_config
from flexgate.guardian import start_guardian_subprocess, check_port_owner
from flexgate.main import run_server

PID_FILE = os.path.join(os.path.dirname(__file__), "..", "flexgate.pid")
LOG_FILE = os.path.join(os.path.dirname(__file__), "..", "flexgate.log")
GUARDIAN_PID_FILE = os.path.join(os.path.dirname(__file__), "..", "flexgate.guardian.pid")


def _read_pid() -> int | None:
    try:
        with open(PID_FILE) as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return None


def _write_pid(pid: int) -> None:
    with open(PID_FILE, "w") as f:
        f.write(str(pid))


def _remove_pid() -> None:
    try:
        os.remove(PID_FILE)
    except FileNotFoundError:
        pass


def _read_guardian_pid() -> int | None:
    try:
        with open(GUARDIAN_PID_FILE) as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return None


def _remove_guardian_pid() -> None:
    try:
        os.remove(GUARDIAN_PID_FILE)
    except FileNotFoundError:
        pass


def _is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


# ── gateway subcommands ────────────────────────────────────────────

def cmd_gateway_start(args: argparse.Namespace) -> None:
    pid = _read_pid()
    if pid and _is_running(pid):
        print(f"Gateway already running (PID {pid})")
        sys.exit(1)

    config = load_config(args.config)
    port = args.port or config.server.port

    # Port pre-check (strict): refuse to start if the port is already occupied
    if not getattr(args, "no_guardian", False):
        owner = check_port_owner(port)
        if owner:
            print(f"[Guardian] ERROR: Port {port} is already occupied by PID {owner.pid} ({owner.command}).")
            print(f"[Guardian] Please stop that process first, then run 'flexgate gateway start' again.")
            sys.exit(1)

    cmd = [sys.executable, "-m", "flexgate.main"]
    if args.config:
        cmd += ["--config", args.config]
    if args.port:
        cmd += ["--port", str(args.port)]

    log_f = open(LOG_FILE, "a")
    proc = subprocess.Popen(cmd, stdout=log_f, stderr=log_f, start_new_session=True)
    _write_pid(proc.pid)

    # Spawn guardian monitor process
    if not getattr(args, "no_guardian", False):
        interval = getattr(args, "guardian_interval", 3.0)
        gr_proc = start_guardian_subprocess(
            port=port,
            interval=interval,
            strict=True,
            log_path=os.path.abspath(LOG_FILE),
            pid_file=os.path.abspath(GUARDIAN_PID_FILE),
            gateway_pid=proc.pid,
        )
        time.sleep(0.2)
        print(f"Gateway + Guardian started (PIDs {proc.pid}, {gr_proc.pid}) on http://{config.server.host}:{port}")
    else:
        print(f"Gateway started (PID {proc.pid}) on http://{config.server.host}:{port} (guardian disabled)")

    print(f"Log: {os.path.abspath(LOG_FILE)}")


def cmd_gateway_stop(args: argparse.Namespace) -> None:
    pid = _read_pid()
    guardian_pid = _read_guardian_pid()

    # Stop guardian first
    if guardian_pid and _is_running(guardian_pid):
        try:
            os.kill(guardian_pid, signal.SIGTERM)
        except PermissionError:
            print(f"Permission denied killing guardian PID {guardian_pid}")
        except ProcessLookupError:
            pass
        for _ in range(10):
            if not _is_running(guardian_pid):
                break
            time.sleep(0.2)
        if _is_running(guardian_pid):
            try:
                os.kill(guardian_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    _remove_guardian_pid()

    # Stop gateway
    if not pid:
        print("Gateway not running (no PID file)")
        sys.exit(1)
    if not _is_running(pid):
        _remove_pid()
        print("Gateway not running (stale PID file cleaned)")
        sys.exit(1)

    try:
        os.kill(pid, signal.SIGTERM)
    except PermissionError:
        print(f"Permission denied killing PID {pid}")
        sys.exit(1)
    except ProcessLookupError:
        _remove_pid()
        print("Gateway not running (process gone)")
        sys.exit(1)

    for _ in range(20):
        if not _is_running(pid):
            break
        time.sleep(0.25)

    if _is_running(pid):
        os.kill(pid, signal.SIGKILL)

    _remove_pid()
    print("Gateway and guardian stopped")


def cmd_gateway_restart(args: argparse.Namespace) -> None:
    pid = _read_pid()
    if pid and _is_running(pid):
        cmd_gateway_stop(args)
    cmd_gateway_start(args)


def cmd_gateway_status(args: argparse.Namespace) -> None:
    pid = _read_pid()
    guardian_pid = _read_guardian_pid()
    config = load_config(args.config)
    port = args.port or config.server.port

    if pid and _is_running(pid):
        lines = [f"Gateway running (PID {pid}) on http://{config.server.host}:{port}"]
    elif pid:
        lines = ["Gateway not running (stale PID file)"]
        _remove_pid()
    else:
        lines = ["Gateway not running"]

    if guardian_pid and _is_running(guardian_pid):
        lines.append(f"Guardian running (PID {guardian_pid})")
    elif guardian_pid:
        lines.append("Guardian PID file stale")
        _remove_guardian_pid()
    else:
        lines.append("Guardian not active")

    print(" | ".join(lines))


def cmd_gateway_run(args: argparse.Namespace) -> None:
    run_server(args.config, args.port)


# ── settings subcommands ────────────────────────────────────────────

def cmd_settings_import(args: argparse.Namespace) -> None:
    from flexgate.settings import settings_import
    settings_import(args.config)


def cmd_settings_apply(args: argparse.Namespace) -> None:
    from flexgate.settings import settings_apply
    settings_apply(args.config)


# ── main ───────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="flexgate",
        description="Flexgate — Flexible API Gateway for Claude Code",
    )
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")

    sub = parser.add_subparsers(dest="group")

    # flexgate gateway ...
    gw = sub.add_parser("gateway", help="Manage the gateway service")
    gw.add_argument("--port", type=int, default=None, help="Override listen port")
    gw_sub = gw.add_subparsers(dest="command")
    gw_start = gw_sub.add_parser("start", help="Start gateway as background service")
    gw_start.add_argument(
        "--no-guardian", action="store_true",
        help="Skip the port-guardian monitor (not recommended)"
    )
    gw_start.add_argument(
        "--guardian-interval", type=float, default=3.0,
        help="Port check interval in seconds for the guardian (default: 3.0)"
    )
    gw_sub.add_parser("stop", help="Stop background gateway")
    gw_sub.add_parser("restart", help="Restart gateway")
    gw_sub.add_parser("status", help="Check gateway status")
    gw_sub.add_parser("run", help="Run gateway in foreground (for debug)")

    # flexgate settings ...
    st = sub.add_parser("settings", help="Manage Claude Code settings")
    st_sub = st.add_subparsers(dest="command")
    st_sub.add_parser("import", help="Import ~/.claude/settings.json* into config.yaml")
    st_sub.add_parser("apply", help="Apply config.yaml to ~/.claude/settings.json")


    args = parser.parse_args()

    if args.group == "gateway":
        handlers = {
            "start": cmd_gateway_start,
            "stop": cmd_gateway_stop,
            "restart": cmd_gateway_restart,
            "status": cmd_gateway_status,
            "run": cmd_gateway_run,
        }
        handler = handlers.get(args.command)
        if not handler:
            gw.print_help()
            sys.exit(1)
        handler(args)

    elif args.group == "settings":
        handlers = {
            "import": cmd_settings_import,
            "apply": cmd_settings_apply,
        }
        handler = handlers.get(args.command)
        if not handler:
            st.print_help()
            sys.exit(1)
        handler(args)

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
