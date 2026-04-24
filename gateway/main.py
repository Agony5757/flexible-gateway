from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import sys
import time

from gateway.config import load_config
from gateway.server import create_app

PID_FILE = os.path.join(os.path.dirname(__file__), "..", "gateway.pid")


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


def cmd_start(args: argparse.Namespace) -> None:
    pid = _read_pid()
    if pid and _is_running(pid):
        print(f"Gateway already running (PID {pid})")
        sys.exit(1)

    config = load_config(args.config)
    port = args.port or config.server.port

    cmd = [sys.executable, "-m", "gateway.main"]
    if args.config:
        cmd += ["--config", args.config]
    if args.port:
        cmd += ["--port", str(args.port)]
    cmd.append("run")

    log_path = os.path.join(os.path.dirname(__file__), "..", "gateway.log")
    log_f = open(log_path, "a")
    proc = subprocess.Popen(cmd, stdout=log_f, stderr=log_f, start_new_session=True)
    _write_pid(proc.pid)

    print(f"Gateway started (PID {proc.pid}) on http://{config.server.host}:{port}")
    print(f"Log: {os.path.abspath(log_path)}")


def cmd_stop(args: argparse.Namespace) -> None:
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


def cmd_restart(args: argparse.Namespace) -> None:
    pid = _read_pid()
    if pid and _is_running(pid):
        cmd_stop(args)
    cmd_start(args)


def cmd_status(args: argparse.Namespace) -> None:
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


def cmd_run(args: argparse.Namespace) -> None:
    import uvicorn

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    config = load_config(args.config)
    port = args.port or config.server.port
    app = create_app(config)
    uvicorn.run(app, host=config.server.host, port=port, log_level="info")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="gateway",
        description="Flexible API Gateway for Claude Code",
    )
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument("--port", type=int, default=None, help="Override listen port")

    sub = parser.add_subparsers(dest="command")
    sub.add_parser("start", help="Start gateway as background service")
    sub.add_parser("stop", help="Stop background gateway")
    sub.add_parser("restart", help="Restart gateway")
    sub.add_parser("status", help="Check gateway status")
    sub.add_parser("run", help="Run gateway in foreground (for debug)")

    args = parser.parse_args()

    commands = {
        "start": cmd_start,
        "stop": cmd_stop,
        "restart": cmd_restart,
        "status": cmd_status,
        "run": cmd_run,
    }

    handler = commands.get(args.command)
    if not handler:
        parser.print_help()
        sys.exit(1)

    handler(args)


if __name__ == "__main__":
    main()
