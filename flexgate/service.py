"""Manage Flexgate's authoritative systemd user service.

Persistent serving belongs to ``flexgate.service``. ``flexgate run`` remains a
foreground development command; legacy PID/guardian processes are migrated
away when the service starts.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass

from flexgate.config import FLEXGATE_HOME, get_default_config_path, load_config
from flexgate.ui import bold, dim, err, fail, green, ok, red, yellow

SERVICE_NAME = "flexgate.service"
UNIT_MARKER = "# Managed by flexgate service mode v5"
LEGACY_PID_FILE = os.path.join(FLEXGATE_HOME, "flexgate.pid")
LEGACY_GUARDIAN_PID_FILE = os.path.join(FLEXGATE_HOME, "flexgate.guardian.pid")
STATE_FILE = os.path.join(FLEXGATE_HOME, "service-state.json")


@dataclass
class UnitBackup:
    content: str | None
    was_enabled: bool
    was_active: bool
    runtime_was_stopped: bool


@dataclass
class AppliedState:
    config_path: str
    host: str
    port: int


@dataclass
class LegacyRuntime:
    gateway_pid: int | None = None
    gateway_argv: list[str] | None = None
    gateway_cwd: str | None = None
    guardian_pid: int | None = None

    @property
    def running(self) -> bool:
        return self.gateway_pid is not None or self.guardian_pid is not None


# ── paths and systemd access ──────────────────────────────────────

def _systemd_user_dir() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "systemd", "user")


def _unit_path() -> str:
    return os.path.join(_systemd_user_dir(), SERVICE_NAME)


def service_installed() -> bool:
    return os.path.exists(_unit_path())


def _systemctl(*args: str, capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["systemctl", "--user", *args],
        text=True,
        capture_output=capture,
    )


def _systemd_user_available() -> tuple[bool, str]:
    if shutil.which("systemctl") is None:
        return False, "systemctl not found — this feature requires systemd (Linux)."
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-system-running"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"Could not query the systemd user instance: {e}"

    state = (result.stdout or "").strip()
    error = (result.stderr or "").lower()
    if "failed to connect to bus" in error or "no medium" in error:
        return False, "No systemd user session bus available (try `systemctl --user`)."
    if state in ("offline", "unknown") and result.returncode != 0:
        return False, f"systemd user instance not available (state: {state or 'unknown'})."
    return True, ""


def _ensure_available() -> None:
    available, reason = _systemd_user_available()
    if available:
        return
    err(reason)
    print(
        "\n'flexgate service' requires a systemd user instance (Linux).\n"
        "Alternative: run the foreground server with 'flexgate run'.",
        file=sys.stderr,
    )
    sys.exit(1)


def _service_active() -> bool:
    return _systemctl(
        "is-active",
        "--quiet",
        SERVICE_NAME,
        capture=True,
    ).returncode == 0


def service_active() -> bool:
    """True iff the systemd user service is currently active.

    Returns False (instead of raising) when systemd is unavailable, so
    callers can use it for state detection on any platform.
    """
    available, _ = _systemd_user_available()
    if not available:
        return False
    return _service_active()


def _service_main_pid() -> int | None:
    result = _systemctl(
        "show",
        SERVICE_NAME,
        "-p",
        "MainPID",
        "--value",
        capture=True,
    )
    try:
        pid = int((result.stdout or "").strip())
    except ValueError:
        return None
    return pid or None


def _enable_linger() -> None:
    if shutil.which("loginctl") is None:
        return
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    command = ["loginctl", "enable-linger"]
    if user:
        command.append(user)
    try:
        subprocess.run(command, check=False, capture_output=True, text=True)
    except OSError:
        pass


# ── unit generation and inspection ────────────────────────────────

def _q(token: str) -> str:
    if any(character.isspace() for character in token):
        escaped = token.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return token


def _unit_content(config_path: str) -> str:
    config_abs = os.path.abspath(config_path)
    python = os.path.abspath(sys.executable)
    exec_start = " ".join(
        [_q(python), "-m", "flexgate.main", "--config", _q(config_abs)]
    )
    return f"""\
{UNIT_MARKER}
[Unit]
Description=Flexgate — local Anthropic API gateway for Claude Code
After=network-online.target
Wants=network-online.target
ConditionPathExists={_q(config_abs)}
StartLimitIntervalSec=30
StartLimitBurst=5

[Service]
Type=simple
Environment={_q(f"FLEXGATE_CONFIG={config_abs}")}
Environment=FLEXGATE_SERVICE=1
ExecStart={exec_start}
ExecReload=/bin/kill -USR1 $MAINPID
Restart=on-failure
RestartSec=3
TimeoutStopSec=10
SyslogIdentifier=flexgate

[Install]
WantedBy=default.target
"""


def _read_unit_content() -> str:
    try:
        with open(_unit_path()) as file:
            return file.read()
    except OSError:
        return ""


def _installed_config_path() -> str | None:
    content = _read_unit_content()
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line.startswith("Environment="):
            continue
        try:
            tokens = shlex.split(line.split("=", 1)[1])
        except ValueError:
            tokens = []
        for token in tokens:
            if token.startswith("FLEXGATE_CONFIG="):
                return os.path.abspath(token.split("=", 1)[1])

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line.startswith("ExecStart="):
            continue
        try:
            tokens = shlex.split(line.split("=", 1)[1])
        except ValueError:
            return None
        if "--config" in tokens:
            index = tokens.index("--config")
            if index + 1 < len(tokens):
                return os.path.abspath(tokens[index + 1])
    return None


def _installed_exec_path() -> str | None:
    for raw_line in _read_unit_content().splitlines():
        line = raw_line.strip()
        if not line.startswith("ExecStart="):
            continue
        try:
            tokens = shlex.split(line.split("=", 1)[1])
        except ValueError:
            return None
        return tokens[0] if tokens else None
    return None


def _is_temporary_path(path: str) -> bool:
    path_real = os.path.realpath(path)
    volatile_roots = {
        os.path.realpath(root)
        for root in ("/tmp", "/var/tmp", "/run", "/dev/shm")
    }
    for root in volatile_roots:
        try:
            if os.path.commonpath([path_real, root]) == root:
                return True
        except ValueError:
            continue
    return False


def _validate_persistent_config(config_path: str) -> str:
    config_abs = os.path.abspath(config_path)
    if _is_temporary_path(config_abs):
        err(
            f"Refusing to install a persistent service with temporary config: {config_abs}\n"
            f"Copy it to {get_default_config_path()} or another durable path first.",
            1,
        )
    if not os.path.exists(config_abs):
        err(f"Config not found: {config_abs} — run 'flexgate config init' first.", 1)
    try:
        load_config(config_abs)
    except Exception as e:
        err(f"Config error: {e} — fix the config before installing or starting the service.", 1)
    return config_abs


def _unit_repair_reason(config_path: str) -> str | None:
    if not service_installed():
        return "service is not installed"
    installed_config = _installed_config_path()
    if installed_config != os.path.abspath(config_path):
        return (
            f"configured path changed from {installed_config or 'unknown'} "
            f"to {os.path.abspath(config_path)}"
        )
    if _is_temporary_path(installed_config):
        return f"unit references temporary config {installed_config}"
    if not os.path.exists(installed_config):
        return f"configured file is missing: {installed_config}"
    content = _read_unit_content()
    if UNIT_MARKER not in content or " -m flexgate.main " not in content:
        return "unit format is outdated"
    executable = _installed_exec_path()
    if not executable or not os.path.exists(executable):
        return f"service executable is missing: {executable or 'unknown'}"
    return None


# ── applied runtime state ─────────────────────────────────────────

def _read_applied_state() -> AppliedState | None:
    try:
        with open(STATE_FILE) as file:
            raw = json.load(file)
        config_path = raw["config_path"]
        host = raw["host"]
        port = raw["port"]
        if (
            not isinstance(config_path, str)
            or not isinstance(host, str)
            or isinstance(port, bool)
            or not isinstance(port, int)
        ):
            return None
        return AppliedState(
            config_path=os.path.realpath(config_path),
            host=host,
            port=port,
        )
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return None


def _write_applied_state(config_path: str) -> None:
    config = load_config(config_path)
    os.makedirs(FLEXGATE_HOME, exist_ok=True)
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=FLEXGATE_HOME,
            prefix=".service-state.",
            delete=False,
        ) as file:
            json.dump(
                {
                    "config_path": os.path.realpath(config_path),
                    "host": config.server.host,
                    "port": config.server.port,
                },
                file,
            )
            temp_path = file.name
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, STATE_FILE)
    except OSError as e:
        if temp_path:
            _remove_file(temp_path)
        print(yellow(f"Warning: could not record applied service state: {e}"))


def record_applied_state(config_path: str) -> None:
    """Record the config and endpoint after the service has bound successfully."""
    _write_applied_state(config_path)


def _runtime_matches(config_path: str) -> bool:
    state = _read_applied_state()
    if state is None:
        return False
    config = load_config(config_path)
    return (
        state.config_path == os.path.realpath(config_path)
        and state.host == config.server.host
        and state.port == config.server.port
    )


def _bootstrap_applied_state() -> None:
    if not _service_active():
        return
    state = _read_applied_state()
    main_pid = _service_main_pid()
    if state is not None and main_pid:
        try:
            process_started = os.stat(f"/proc/{main_pid}").st_ctime_ns
            state_written = os.stat(STATE_FILE).st_mtime_ns
        except OSError:
            pass
        else:
            if state_written >= process_started:
                return
    config_path = _installed_config_path()
    if (
        not config_path
        or not os.path.exists(config_path)
        or _is_temporary_path(config_path)
    ):
        return
    try:
        load_config(config_path)
    except Exception:
        return
    if main_pid:
        try:
            process_started = os.stat(f"/proc/{main_pid}").st_ctime_ns
            config_written = os.stat(config_path).st_mtime_ns
        except OSError:
            return
        if config_written > process_started:
            return
    _write_applied_state(config_path)


# ── legacy PID daemon migration ───────────────────────────────────

def _read_pid_file(path: str) -> int | None:
    try:
        with open(path) as file:
            return int(file.read().strip())
    except (FileNotFoundError, ValueError):
        return None


def _remove_file(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def _pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _process_argv(pid: int) -> list[str]:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as file:
            return [
                token.decode(errors="replace")
                for token in file.read().split(b"\0")
                if token
            ]
    except OSError:
        return []


def _matches_legacy_process(pid: int, module: str) -> bool:
    argv = _process_argv(pid)
    return (
        len(argv) >= 3
        and os.path.basename(argv[0]).startswith("python")
        and argv[1:3] == ["-m", module]
    )


def _inspect_legacy_runtime() -> LegacyRuntime:
    runtime = LegacyRuntime()
    gateway_pid = _read_pid_file(LEGACY_PID_FILE)
    if (
        gateway_pid
        and _pid_running(gateway_pid)
        and _matches_legacy_process(gateway_pid, "flexgate.main")
    ):
        runtime.gateway_pid = gateway_pid
        runtime.gateway_argv = _process_argv(gateway_pid)
        try:
            runtime.gateway_cwd = os.readlink(f"/proc/{gateway_pid}/cwd")
        except OSError:
            pass

    guardian_pid = _read_pid_file(LEGACY_GUARDIAN_PID_FILE)
    if (
        guardian_pid
        and _pid_running(guardian_pid)
        and _matches_legacy_process(guardian_pid, "flexgate.guardian")
    ):
        runtime.guardian_pid = guardian_pid
    return runtime


def _argv_option(argv: list[str], name: str) -> str | None:
    for index, token in enumerate(argv):
        if token == name and index + 1 < len(argv):
            return argv[index + 1]
        prefix = f"{name}="
        if token.startswith(prefix):
            return token[len(prefix):]
    return None


def _legacy_config_path(legacy: LegacyRuntime) -> str | None:
    if not legacy.gateway_argv:
        return None
    config_path = _argv_option(legacy.gateway_argv, "--config")
    if config_path and not os.path.isabs(config_path) and legacy.gateway_cwd:
        config_path = os.path.join(legacy.gateway_cwd, config_path)
    return os.path.abspath(config_path) if config_path else get_default_config_path()


def _select_config(
    requested_config: str | None,
    legacy: LegacyRuntime,
) -> str:
    installed_config = _installed_config_path() if service_installed() else None

    if (
        requested_config is None
        and legacy.gateway_pid is None
        and _service_active()
        and (
            installed_config is None
            or _is_temporary_path(installed_config)
            or not os.path.exists(installed_config)
        )
    ):
        err(
            "The active service references a missing or volatile config. "
            "Refusing to replace it implicitly.\n"
            "Select a durable replacement explicitly with: flexgate --config PATH service start",
            1,
        )

    config_path = requested_config
    if config_path is None and legacy.gateway_argv:
        config_path = _legacy_config_path(legacy)
    if config_path is None and installed_config:
        if os.path.exists(installed_config) and not _is_temporary_path(installed_config):
            config_path = installed_config
    if config_path is None:
        config_path = get_default_config_path()

    config_path = _validate_persistent_config(config_path)
    if legacy.gateway_argv:
        port_override = _argv_option(legacy.gateway_argv, "--port")
        if port_override is not None:
            try:
                legacy_port = int(port_override)
            except ValueError:
                err(f"Invalid legacy --port value: {port_override}", 1)
            configured_port = load_config(config_path).server.port
            if legacy_port != configured_port:
                err(
                    f"Legacy gateway uses --port {legacy_port}, but {config_path} "
                    f"configures server.port={configured_port}.\n"
                    "Update server.port in the config before migrating to service mode.",
                    1,
                )
    return config_path


def _terminate_process(pid: int, module: str) -> bool:
    if not _matches_legacy_process(pid, module):
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except PermissionError:
        fail(f"Permission denied stopping legacy Flexgate PID {pid}")
        return False

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not _pid_running(pid):
            return True
        time.sleep(0.1)
    if _matches_legacy_process(pid, module):
        try:
            os.kill(pid, signal.SIGKILL)
        except (PermissionError, ProcessLookupError):
            pass
    return not _pid_running(pid)


def _stop_legacy_runtime(runtime: LegacyRuntime) -> None:
    if runtime.guardian_pid:
        if _terminate_process(runtime.guardian_pid, "flexgate.guardian"):
            ok(f"stopped legacy unmanaged guardian process {dim(f'(PID {runtime.guardian_pid})')}")
    if runtime.gateway_pid:
        if _terminate_process(runtime.gateway_pid, "flexgate.main"):
            ok(f"stopped legacy unmanaged gateway process {dim(f'(PID {runtime.gateway_pid})')}")
    _remove_file(LEGACY_GUARDIAN_PID_FILE)
    _remove_file(LEGACY_PID_FILE)


def _clean_stale_legacy_files() -> None:
    for path, module, label in (
        (LEGACY_GUARDIAN_PID_FILE, "flexgate.guardian", "guardian"),
        (LEGACY_PID_FILE, "flexgate.main", "gateway"),
    ):
        pid = _read_pid_file(path)
        if pid is None:
            _remove_file(path)
        elif not _pid_running(pid) or not _matches_legacy_process(pid, module):
            _remove_file(path)
            print(dim(f"Removed stale legacy {label} PID file: {path}"))


def _print_legacy_status(runtime: LegacyRuntime) -> None:
    if runtime.gateway_pid:
        print(yellow(f"Legacy unmanaged gateway is running (PID {runtime.gateway_pid})."))
    if runtime.guardian_pid:
        print(yellow(f"Legacy unmanaged guardian is running (PID {runtime.guardian_pid})."))


# ── endpoint preflight ────────────────────────────────────────────

def _probe_endpoint(host: str, port: int) -> tuple[bool, str]:
    try:
        addresses = socket.getaddrinfo(
            host or None,
            port,
            type=socket.SOCK_STREAM,
            flags=socket.AI_PASSIVE,
        )
    except OSError as e:
        return False, str(e)

    sockets: list[socket.socket] = []
    seen: set[tuple[int, tuple]] = set()
    try:
        for family, socktype, proto, _, sockaddr in addresses:
            key = (family, sockaddr)
            if key in seen:
                continue
            seen.add(key)
            sock = socket.socket(family, socktype, proto)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if family == socket.AF_INET6:
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            try:
                sock.bind(sockaddr)
                sock.listen(1)
            except OSError:
                sock.close()
                raise
            sockets.append(sock)
    except OSError as e:
        return False, str(e)
    finally:
        for sock in sockets:
            sock.close()
    if not seen:
        return False, "no bindable address"
    return True, ""


def _preflight_endpoint(config_path: str) -> None:
    config = load_config(config_path)
    target = (config.server.host, config.server.port)
    if _service_active():
        state = _read_applied_state()
        if state:
            applied = (state.host, state.port)
            if target == applied:
                return
            if target[1] == applied[1]:
                err(
                    "Changing server.host on the currently active port requires a controlled stop.\n"
                    "Run 'flexgate service stop', then 'flexgate service start'.",
                    1,
                )
    available, error = _probe_endpoint(*target)
    if not available:
        err(f"Cannot bind configured endpoint {target[0]}:{target[1]}: {error}", 1)


# ── unit transaction ──────────────────────────────────────────────

def _write_service_unit(
    config_path: str,
    *,
    stop_active: bool,
) -> UnitBackup:
    unit_dir = _systemd_user_dir()
    os.makedirs(unit_dir, exist_ok=True)
    old_content = _read_unit_content() if service_installed() else None
    was_active = _service_active()
    was_enabled = _systemctl(
        "is-enabled",
        "--quiet",
        SERVICE_NAME,
        capture=True,
    ).returncode == 0
    backup = UnitBackup(
        content=old_content,
        was_enabled=was_enabled,
        was_active=was_active,
        runtime_was_stopped=stop_active and (service_installed() or was_active),
    )

    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=unit_dir,
            prefix=".flexgate.service.",
            delete=False,
        ) as file:
            file.write(_unit_content(config_path))
            temp_path = file.name
    except OSError as e:
        err(f"Failed to prepare service unit: {e}", 1)

    if backup.runtime_was_stopped:
        result = _systemctl("stop", SERVICE_NAME, capture=True)
        if result.returncode != 0:
            _remove_file(temp_path)
            err(
                f"Failed to stop the existing {SERVICE_NAME}; unit was not changed."
                + (f"\n{result.stderr.strip()}" if result.stderr else ""),
                1,
            )

    try:
        os.replace(temp_path, _unit_path())
    except OSError as e:
        _remove_file(temp_path)
        if was_active:
            _systemctl("start", SERVICE_NAME, capture=True)
        err(f"Failed to replace service unit: {e}", 1)
    ok(f"wrote unit {dim(_unit_path())}")

    reload_result = _systemctl("daemon-reload", capture=True)
    if reload_result.returncode != 0:
        err(
            "Failed to reload the systemd user manager."
            + (f"\n{reload_result.stderr.strip()}" if reload_result.stderr else "")
        )
        _restore_service_unit(backup)
        sys.exit(1)

    enable_result = _systemctl("enable", SERVICE_NAME, capture=True)
    if enable_result.returncode != 0:
        err(
            f"Failed to enable {SERVICE_NAME}."
            + (f"\n{enable_result.stderr.strip()}" if enable_result.stderr else "")
        )
        _restore_service_unit(backup)
        sys.exit(1)
    ok(f"enabled {SERVICE_NAME} {dim('(will start on login)')}")
    return backup


def _restore_service_unit(
    backup: UnitBackup,
    *,
    stop_current: bool = False,
) -> None:
    if stop_current or backup.runtime_was_stopped:
        _systemctl("stop", SERVICE_NAME, capture=True)
    try:
        if backup.content is None:
            _remove_file(_unit_path())
        else:
            with open(_unit_path(), "w") as file:
                file.write(backup.content)
    except OSError as e:
        print(yellow(f"Warning: could not restore the previous service unit: {e}"))
        return

    _systemctl("daemon-reload", capture=True)
    _systemctl("reset-failed", SERVICE_NAME, capture=True)
    action = "enable" if backup.was_enabled else "disable"
    _systemctl(action, SERVICE_NAME, capture=True)
    if backup.was_active:
        result = _systemctl("start", SERVICE_NAME, capture=True)
        if result.returncode == 0 and _wait_for_active():
            ok(f"restored the previous {SERVICE_NAME}")
        else:
            print(yellow(f"Warning: previous {SERVICE_NAME} could not be restarted."))


def _ensure_service_unit(
    config_path: str,
    *,
    install_if_missing: bool,
    stop_active: bool,
) -> tuple[bool, UnitBackup | None]:
    was_installed = service_installed()
    if not was_installed and not install_if_missing:
        err(f"Service not installed (no unit at {_unit_path()}) — run 'flexgate service install' first.", 1)
    reason = _unit_repair_reason(config_path)
    if reason is None:
        return False, None
    print(f"{yellow('Repairing')} {SERVICE_NAME}: {dim(reason)}.")
    backup = _write_service_unit(config_path, stop_active=stop_active)
    if not was_installed:
        _enable_linger()
    return True, backup


# ── start/reload helpers ──────────────────────────────────────────

def _wait_for_active(timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    active_since: float | None = None
    while time.monotonic() < deadline:
        state = _systemctl("is-active", SERVICE_NAME, capture=True).stdout.strip()
        if state == "active":
            if active_since is None:
                active_since = time.monotonic()
            elif time.monotonic() - active_since >= 0.5:
                return True
        else:
            active_since = None
        time.sleep(0.1)
    return False


def _print_start_failure(result: subprocess.CompletedProcess | None = None) -> None:
    print(red(f"Failed to start {SERVICE_NAME}."), file=sys.stderr)
    if result is not None and result.stderr:
        print(result.stderr.strip(), file=sys.stderr)
    status = _systemctl("status", SERVICE_NAME, "--no-pager", "--full", capture=True)
    details = (status.stdout or status.stderr or "").strip()
    if details:
        print(details, file=sys.stderr)
    print(f"Inspect logs with: {dim('journalctl --user -u flexgate -e')}", file=sys.stderr)


def _start_service(config_path: str, *, restart: bool) -> None:
    _systemctl("reset-failed", SERVICE_NAME, capture=True)
    action = "restart" if restart else "start"
    result = _systemctl(action, SERVICE_NAME, capture=True)
    if result.returncode != 0:
        _print_start_failure(result)
        sys.exit(1)
    if not _wait_for_active():
        _print_start_failure()
        sys.exit(1)
    _write_applied_state(config_path)
    verb = "restarted" if restart else "started"
    ok(f"{verb} {bold(SERVICE_NAME)}")
    _print_status_brief()


def _start_with_rollback(
    config_path: str,
    *,
    restart: bool,
    backup: UnitBackup | None,
) -> None:
    try:
        _start_service(config_path, restart=restart)
    except SystemExit:
        if backup is not None:
            _restore_service_unit(backup, stop_current=True)
        raise


def _reload_validation_error(config_path: str) -> str | None:
    if not os.path.exists(config_path):
        return f"configured file is missing: {config_path}"
    try:
        load_config(config_path)
    except Exception as e:
        return f"invalid config {config_path}: {e}"
    return None


def _print_status_brief() -> None:
    active = _systemctl("is-active", SERVICE_NAME, capture=True).stdout.strip()
    enabled = _systemctl("is-enabled", SERVICE_NAME, capture=True).stdout.strip()
    active_disp = green(active) if active == "active" else red(active or "unknown")
    print(
        f"{dim('Service:')} {bold(SERVICE_NAME)}  "
        f"{dim('active=')}{active_disp}  "
        f"{dim('enabled=')}{enabled or 'unknown'}"
    )


# ── Claude settings prompt ────────────────────────────────────────

def _maybe_apply_claude_settings(config_path: str, *, skip: bool) -> None:
    if skip:
        return

    from flexgate.settings import CLAUDE_DIR, settings_apply

    settings_file = os.path.join(CLAUDE_DIR, "settings.json")
    if not sys.stdin.isatty():
        if not os.path.exists(settings_file):
            settings_apply(config_path)
        return
    if not os.path.exists(settings_file):
        settings_apply(config_path)
        return

    try:
        answer = input(
            "A Claude Code settings.json already exists at ~/.claude/settings.json. "
            "Rewrite its flexgate env keys to point at the gateway? "
            "(other fields are preserved) [Yes/No]: "
        ).strip().lower()
    except EOFError:
        return
    if answer not in ("y", "yes"):
        return
    settings_apply(config_path)


# ── public commands ───────────────────────────────────────────────

def _refuse_running_legacy_gateway(gateway_pid: int) -> None:
    err(
        f"A legacy unmanaged gateway is still running (PID {gateway_pid}).\n"
        "The systemd unit has been prepared, but the legacy process was left untouched.\n"
        f"Stop it with 'kill {gateway_pid}', then run 'flexgate service start'.",
        1,
    )


def service_install(
    config_path: str | None,
    start: bool = True,
    *,
    no_claude_settings: bool = False,
) -> None:
    _ensure_available()
    _bootstrap_applied_state()
    legacy = _inspect_legacy_runtime()
    config_path = _select_config(config_path, legacy)

    if legacy.gateway_pid:
        _write_service_unit(config_path, stop_active=False)
        _enable_linger()
        if not start:
            print(yellow(f"Legacy gateway remains running until you stop it manually {dim(f'(kill {legacy.gateway_pid})')}."))
            print(yellow("Service installed but not started (use 'flexgate service start' afterward)."))
            _print_status_brief()
            return
        _refuse_running_legacy_gateway(legacy.gateway_pid)

    if start:
        _preflight_endpoint(config_path)
    backup = _write_service_unit(config_path, stop_active=start)
    _enable_linger()

    if not start:
        if legacy.running:
            print(yellow("Legacy gateway remains running until 'flexgate service start'."))
        if not no_claude_settings:
            print(yellow("Claude settings were left unchanged because the service was not started."))
        if _service_active():
            print(yellow("Service unit updated; the existing process remains active until restart."))
        else:
            print(yellow("Service installed but not started (use 'flexgate service start')."))
        _print_status_brief()
        return

    if legacy.guardian_pid:
        _stop_legacy_runtime(legacy)
    _clean_stale_legacy_files()
    _start_with_rollback(config_path, restart=True, backup=backup)
    _maybe_apply_claude_settings(config_path, skip=no_claude_settings)


def service_start(
    config_path: str | None = None,
    *,
    install_if_missing: bool = False,
) -> None:
    _ensure_available()
    _bootstrap_applied_state()
    legacy = _inspect_legacy_runtime()
    config_path = _select_config(config_path, legacy)
    was_active = _service_active()

    if legacy.gateway_pid:
        _ensure_service_unit(
            config_path,
            install_if_missing=install_if_missing,
            stop_active=False,
        )
        _refuse_running_legacy_gateway(legacy.gateway_pid)

    _preflight_endpoint(config_path)
    changed, backup = _ensure_service_unit(
        config_path,
        install_if_missing=install_if_missing,
        stop_active=True,
    )
    if legacy.guardian_pid:
        _stop_legacy_runtime(legacy)
    _clean_stale_legacy_files()

    if was_active and not changed and _runtime_matches(config_path):
        print(f"{bold(SERVICE_NAME)} is already {green('active')}.")
        _print_status_brief()
        return
    _start_with_rollback(
        config_path,
        restart=changed or was_active,
        backup=backup,
    )


def service_restart(
    config_path: str | None = None,
    *,
    install_if_missing: bool = False,
) -> None:
    _ensure_available()
    _bootstrap_applied_state()
    legacy = _inspect_legacy_runtime()
    config_path = _select_config(config_path, legacy)
    was_active = _service_active()

    if legacy.gateway_pid:
        _ensure_service_unit(
            config_path,
            install_if_missing=install_if_missing,
            stop_active=False,
        )
        _refuse_running_legacy_gateway(legacy.gateway_pid)

    _preflight_endpoint(config_path)
    _, backup = _ensure_service_unit(
        config_path,
        install_if_missing=install_if_missing,
        stop_active=True,
    )
    if legacy.guardian_pid:
        _stop_legacy_runtime(legacy)
    _clean_stale_legacy_files()
    _start_with_rollback(
        config_path,
        restart=was_active or service_installed(),
        backup=backup,
    )


def service_stop() -> None:
    legacy = _inspect_legacy_runtime()
    if legacy.running:
        _stop_legacy_runtime(legacy)
    _clean_stale_legacy_files()

    available, reason = _systemd_user_available()
    if not available:
        err(reason)
        if legacy.running:
            return
        sys.exit(1)
    if not service_installed() and not _service_active():
        print(yellow(f"{SERVICE_NAME} is not installed."))
        return
    result = _systemctl("stop", SERVICE_NAME, capture=True)
    if result.returncode != 0:
        if result.stderr:
            err(result.stderr.strip())
        sys.exit(1)
    ok(f"stopped {bold(SERVICE_NAME)}")


def service_reload() -> None:
    _ensure_available()
    _bootstrap_applied_state()
    if not service_installed():
        err(f"Service not installed (no unit at {_unit_path()}).", 1)
    if not _service_active():
        err(f"{SERVICE_NAME} is not active.", 1)
    config_path = _installed_config_path()
    if not config_path:
        err("Service unit does not record a config path.", 1)
    validation_error = _reload_validation_error(config_path)
    if validation_error:
        err(f"Refusing to reload {SERVICE_NAME}: {validation_error}", 1)
    if not _runtime_matches(config_path):
        print(yellow("Service config path or endpoint changed; restarting instead of reloading."))
        service_restart(config_path)
        return
    result = _systemctl("reload", SERVICE_NAME, capture=True)
    if result.returncode != 0:
        if result.stderr:
            err(result.stderr.strip())
        sys.exit(1)
    ok(f"reloaded {bold(SERVICE_NAME)}")


def reload_service_if_active(config_path: str | None = None) -> str | None:
    available, _ = _systemd_user_available()
    if not available or not _service_active():
        return None
    _bootstrap_applied_state()
    installed_config = _installed_config_path()
    if config_path and (
        not installed_config
        or os.path.realpath(config_path) != os.path.realpath(installed_config)
    ):
        return yellow(
            f"Service not reloaded: active unit uses {installed_config or 'an unknown config'}, "
            f"not {os.path.abspath(config_path)}"
        )
    if not installed_config:
        return yellow(f"Warning: could not reload {SERVICE_NAME}: unit config path is unknown")
    validation_error = _reload_validation_error(installed_config)
    if validation_error:
        return yellow(f"Warning: could not reload {SERVICE_NAME}: {validation_error}")
    if not _runtime_matches(installed_config):
        try:
            service_restart(installed_config)
        except SystemExit:
            if _service_active():
                return yellow(
                    f"Warning: {SERVICE_NAME} could not switch to the new config/endpoint; "
                    "the previous process remains active"
                )
            return yellow(f"Warning: {SERVICE_NAME} failed to restart; inspect the journal")
        return green(f"Restarted {SERVICE_NAME} because its config path or endpoint changed")
    result = _systemctl("reload", SERVICE_NAME, capture=True)
    if result.returncode == 0:
        return green(f"Reloaded {SERVICE_NAME}")
    error = (result.stderr or result.stdout or "unknown systemctl error").strip()
    return yellow(f"Warning: could not reload {SERVICE_NAME}: {error}")


def service_uninstall() -> None:
    _ensure_available()
    _systemctl("stop", SERVICE_NAME, capture=True)
    _systemctl("disable", SERVICE_NAME, capture=True)
    legacy = _inspect_legacy_runtime()
    if legacy.running:
        _stop_legacy_runtime(legacy)
    _clean_stale_legacy_files()
    if service_installed():
        os.remove(_unit_path())
        ok(f"removed unit {dim(_unit_path())}")
    else:
        print(dim(f"No unit file at {_unit_path()}"))
    _remove_file(STATE_FILE)
    _systemctl("daemon-reload", capture=True)
    _systemctl("reset-failed", SERVICE_NAME, capture=True)
    ok(f"{bold(SERVICE_NAME)} uninstalled")


def service_status() -> str | None:
    legacy = _inspect_legacy_runtime()
    _print_legacy_status(legacy)
    available, reason = _systemd_user_available()
    if not available:
        print(reason)
        return None
    if not service_installed() and not _service_active():
        print(f"Service not installed (no unit at {_unit_path()}).")
        print("Run 'flexgate service install' to install it.")
        return None

    config_path = _installed_config_path()
    print(f"{bold('Unit:')} {_unit_path()}\n")
    status = _systemctl("status", SERVICE_NAME, "--no-pager", "--full", capture=True)
    details = (status.stdout or status.stderr or "").rstrip()
    if details:
        print(details)
    if config_path:
        print(f"\n{dim('Config:')} {config_path}")
    state = _read_applied_state()
    if state:
        print(f"{dim('Applied endpoint:')} {bold(f'{state.host}:{state.port}')}")
    return config_path
