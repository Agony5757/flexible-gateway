from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time

from flexgate.config import load_config
from flexgate.main import run_server

PID_FILE = os.path.join(os.path.dirname(__file__), "..", "flexgate.pid")
LOG_FILE = os.path.join(os.path.dirname(__file__), "..", "flexgate.log")


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

    cmd = [sys.executable, "-m", "flexgate.main"]
    if args.config:
        cmd += ["--config", args.config]
    if args.port:
        cmd += ["--port", str(args.port)]

    log_f = open(LOG_FILE, "a")
    proc = subprocess.Popen(cmd, stdout=log_f, stderr=log_f, start_new_session=True)
    _write_pid(proc.pid)

    print(f"Gateway started (PID {proc.pid}) on http://{config.server.host}:{port}")
    print(f"Log: {os.path.abspath(LOG_FILE)}")


def cmd_gateway_stop(args: argparse.Namespace) -> None:
    pid = _read_pid()
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

    for _ in range(20):
        if not _is_running(pid):
            break
        time.sleep(0.25)

    if _is_running(pid):
        os.kill(pid, signal.SIGKILL)

    _remove_pid()
    print("Gateway stopped")


def cmd_gateway_restart(args: argparse.Namespace) -> None:
    pid = _read_pid()
    if pid and _is_running(pid):
        cmd_gateway_stop(args)
    cmd_gateway_start(args)


def cmd_gateway_status(args: argparse.Namespace) -> None:
    pid = _read_pid()
    if not pid:
        print("Gateway not running")
        return
    if _is_running(pid):
        config = load_config(args.config)
        port = args.port or config.server.port
        print(f"Gateway running (PID {pid}) on http://{config.server.host}:{port}")
    else:
        _remove_pid()
        print("Gateway not running (stale PID file cleaned)")


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
    gw_sub.add_parser("start", help="Start gateway as background service")
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
