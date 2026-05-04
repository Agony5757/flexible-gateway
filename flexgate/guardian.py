"""Port Guardian.

Monitors port 8765 and refuses to start the gateway if the port is already
occupied. While running, it continuously monitors the port and logs an ERROR
if a foreign process grabs it.

Does NOT kill other processes — it only detects and reports.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

logger = logging.getLogger("flexgate.guardian")


# ── Types ────────────────────────────────────────────────────────────────────

class PortOwner(NamedTuple):
    pid: int
    command: str


# ── Config ───────────────────────────────────────────────────────────────────

@dataclass
class GuardianConfig:
    port: int = 8765
    interval: float = 3.0
    strict: bool = True
    guardian_pid_file: str = "flexgate.guardian.pid"
    gateway_pid: int | None = None


# ── Port ownership detection ──────────────────────────────────────────────────

def _port_hex(port: int) -> str:
    return f"{port:04X}"


def check_port_owner(port: int) -> PortOwner | None:
    """Return the PID+command of the process bound to `port`, or None.

    Works on Linux by parsing /proc/net/tcp and matching socket inodes
    against /proc/<pid>/fd/* symlinks.
    """
    target = f":{_port_hex(port)}"
    inodes: set[int] = set()

    for filename in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            text = Path(filename).read_text()
        except OSError:
            continue
        for line in text.splitlines()[1:]:  # skip header line
            parts = line.split()
            if len(parts) < 10:
                continue
            local_addr = parts[1].upper()
            if not local_addr.endswith(target):
                continue
            try:
                inodes.add(int(parts[9]))
            except ValueError:
                continue

    if not inodes:
        return None

    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        fd_dir = Path(f"/proc/{pid}/fd")
        try:
            for fd in fd_dir.iterdir():
                try:
                    link = os.readlink(fd)
                except OSError:
                    continue
                for inode in inodes:
                    if link == f"socket:[{inode}]":
                        comm_path = Path(f"/proc/{pid}/comm")
                        try:
                            comm = comm_path.read_text().strip()
                        except OSError:
                            comm = f"pid-{pid}"
                        return PortOwner(pid=pid, command=comm)
        except OSError:
            continue

    return None


def is_port_free(port: int) -> tuple[bool, str]:
    """Return (True, "") if the port is free, or (False, reason) if occupied."""
    owner = check_port_owner(port)
    if owner is None:
        return True, ""
    return False, f"Port {port} is already occupied by PID {owner.pid} ({owner.command})."


# ── Guardian lifecycle ───────────────────────────────────────────────────────

def _write_guardian_pid(pid: int, path: str) -> None:
    Path(path).write_text(str(pid))


def _remove_guardian_pid(path: str) -> None:
    try:
        Path(path).unlink()
    except FileNotFoundError:
        pass


class PortGuardian:
    """Background monitor for a single port.

    In strict mode: exits immediately if the port is already occupied.
    In monitor mode: logs an ERROR each time a foreign process binds the port.
    """

    def __init__(self, config: GuardianConfig) -> None:
        self.config = config
        self._running = True
        self._occupied_at_start: bool | None = None

    def _signal_stop(self, signum: int, _frame) -> None:
        logger.info("Guardian received %s, shutting down", signal.Signals(signum).name)
        self._running = False

    def run(self) -> None:
        cfg = self.config
        pid_path = os.path.abspath(cfg.guardian_pid_file)

        signal.signal(signal.SIGTERM, self._signal_stop)
        signal.signal(signal.SIGINT, self._signal_stop)

        logger.info(
            "Guardian started, protecting port %d (strict=%s, interval=%.1fs)",
            cfg.port, cfg.strict, cfg.interval,
        )

        # Strict mode: fail immediately if port is already taken
        if cfg.strict:
            free, reason = is_port_free(cfg.port)
            if not free:
                # Also print to stderr so it surfaces in CLI output
                sys.stderr.write(f"[Guardian] ERROR: {reason}\n")
                sys.stderr.write("[Guardian] Please stop that process first, then run 'flexgate gateway start' again.\n")
                sys.stderr.flush()
                _remove_guardian_pid(pid_path)
                sys.exit(1)
            logger.info("Port %d is free — gateway can bind", cfg.port)
            self._occupied_at_start = False
        else:
            self._occupied_at_start = check_port_owner(cfg.port) is not None

        _write_guardian_pid(os.getpid(), pid_path)

        while self._running:
            try:
                self._check_and_alert()
            except Exception as exc:
                logger.exception("Error in guardian loop: %s", exc)

            time.sleep(cfg.interval)

        _remove_guardian_pid(pid_path)
        logger.info("Guardian stopped")

    def _check_and_alert(self) -> None:
        owner = check_port_owner(self.config.port)
        if owner is None:
            return  # port is free
        if self.config.gateway_pid and owner.pid == self.config.gateway_pid:
            return  # port is held by the gateway itself — all good
        logger.error(
            "Port %d is now occupied by PID %d (%s). Gateway is now unreachable!",
            self.config.port, owner.pid, owner.command,
        )


# ── Subprocess spawner (called from cli.py) ──────────────────────────────────

def start_guardian_subprocess(
    port: int,
    interval: float,
    strict: bool,
    log_path: str,
    pid_file: str,
    gateway_pid: int | None = None,
) -> subprocess.Popen:
    """Spawn the guardian as a detached subprocess.

    Redirects stdout/stderr to `log_path` so all guardian log lines
    appear in flexgate.log alongside the gateway's own logs.
    """
    cmd = [
        sys.executable, "-m", "flexgate.guardian",
        str(port), str(interval), str(strict).lower(),
    ]
    if gateway_pid is not None:
        cmd.append(str(gateway_pid))
    log_file = open(log_path, "a")
    proc = subprocess.Popen(
        cmd,
        stdout=log_file,
        stderr=log_file,
        start_new_session=True,
    )
    return proc


# ── Entry point: python -m flexgate.guardian ────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s flexgate.guardian: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if len(sys.argv) not in (4, 5):
        sys.stderr.write(f"Usage: python -m flexgate.guardian <port> <interval> <strict> [gateway_pid]\n")
        sys.exit(1)

    port = int(sys.argv[1])
    interval = float(sys.argv[2])
    strict = sys.argv[3] == "true"
    gateway_pid = int(sys.argv[4]) if len(sys.argv) == 5 else None

    script_dir = Path(__file__).parent.parent.resolve()
    pid_file = str(script_dir / "flexgate.guardian.pid")

    cfg = GuardianConfig(
        port=port,
        interval=interval,
        strict=strict,
        guardian_pid_file=pid_file,
        gateway_pid=gateway_pid,
    )
    guardian = PortGuardian(cfg)
    guardian.run()
