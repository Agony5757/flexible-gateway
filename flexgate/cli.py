from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time

import re

from flexgate.config import (
    FLEXGATE_HOME,
    GatewayConfig,
    RouteConfig,
    DEFAULT_CONFIG_TEMPLATE,
    TIER_PATTERNS,
    get_default_config_path,
    ensure_home_dir,
    load_config,
    save_config,
)
from flexgate.guardian import start_guardian_subprocess, check_port_owner
from flexgate.healthcheck import check_providers, print_results
from flexgate.main import run_server

PATTERN_TIERS = {v: k for k, v in TIER_PATTERNS.items()}

PID_FILE = os.path.join(FLEXGATE_HOME, "flexgate.pid")
LOG_FILE = os.path.join(FLEXGATE_HOME, "flexgate.log")
GUARDIAN_PID_FILE = os.path.join(FLEXGATE_HOME, "flexgate.guardian.pid")


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

    # Connectivity pre-check: verify each provider/model is reachable and
    # the API key is accepted before spawning the daemon.
    if not getattr(args, "no_verify", False):
        timeout = getattr(args, "verify_timeout", 15.0)
        print(f"Verifying provider connectivity (timeout {timeout:g}s)...")
        results = check_providers(config, timeout=timeout)
        print_results(results)
        failures = [r for r in results if not r.ok]
        if failures:
            print(
                f"\nConnectivity check failed for {len(failures)} target(s). "
                f"Refusing to start.\n"
                f"Re-run with --no-verify to skip, or fix the issues above."
            )
            sys.exit(1)

    ensure_home_dir()

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

    try:
        config = load_config(args.config)
    except (FileNotFoundError, ValueError):
        config = None

    port = args.port or (config.server.port if config else 8765)

    if pid and _is_running(pid):
        lines = [f"Gateway running (PID {pid}) on http://127.0.0.1:{port}"]
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

    print(" | ".join(lines))

    if config:
        _print_active_routes(config)


def _print_active_routes(config: GatewayConfig) -> None:
    from datetime import datetime

    now = datetime.now()
    now_minutes = now.hour * 60 + now.minute

    active_routes = None
    label = "default"

    for entry in config.schedule:
        s, e = entry.start_minutes, entry.end_minutes
        if s < e:
            active = s <= now_minutes < e
        elif s > e:
            active = now_minutes >= s or now_minutes < e
        else:
            active = True
        if active and entry.routes:
            active_routes = entry.routes
            label = entry.name or f"{s // 60:02d}:{s % 60:02d}-{e // 60:02d}:{e % 60:02d}"
            break

    if active_routes is None:
        active_routes = config.routes

    if not active_routes:
        return

    print(f"\nRoutes ({label}):")
    _print_route_table(active_routes)


def cmd_gateway_run(args: argparse.Namespace) -> None:
    run_server(args.config, args.port)


def cmd_gateway_check(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    timeout = getattr(args, "verify_timeout", 15.0)
    print(f"Verifying provider connectivity (timeout {timeout:g}s)...")
    results = check_providers(config, timeout=timeout)
    print_results(results)
    failures = [r for r in results if not r.ok]
    if failures:
        print(f"\n{len(failures)} target(s) failed.")
        sys.exit(1)
    print(f"\nAll {len(results)} target(s) OK.")


# ── settings subcommands ────────────────────────────────────────────

def cmd_settings_import(args: argparse.Namespace) -> None:
    from flexgate.settings import settings_import
    settings_import(args.config)


def cmd_settings_apply(args: argparse.Namespace) -> None:
    from flexgate.settings import settings_apply
    settings_apply(args.config)


# ── config subcommands ────────────────────────────────────────────

def _mask_key(key: str) -> str:
    if len(key) <= 8:
        return "***"
    return key[:4] + "***" + key[-4:]


def _print_route_table(routes: list[RouteConfig], indent: int = 2) -> None:
    prefix = " " * indent
    for r in routes:
        tier = PATTERN_TIERS.get(r.pattern.pattern, "")
        tier_col = f"({tier}) " if tier else ""
        target = r.provider_name
        if r.model:
            target += f" / {r.model}"
        print(f"{prefix}{tier_col}{r.pattern.pattern:20s} → {target}")


def cmd_config_init(args: argparse.Namespace) -> None:
    config_path = args.config
    if os.path.exists(config_path):
        print(f"Config already exists: {config_path}")
        return
    with open(config_path, "w") as f:
        f.write(DEFAULT_CONFIG_TEMPLATE)
    print(f"Created: {config_path}")
    print("Edit the file to add your API keys and providers.")


def cmd_config_show(args: argparse.Namespace) -> None:
    from datetime import datetime

    config_path = args.config
    if not os.path.exists(config_path):
        print(f"Config not found: {config_path}")
        print("Run 'flexgate config init' to create one.")
        return

    config = load_config(config_path)

    print(f"Config: {os.path.abspath(config_path)}")
    print(f"Server: {config.server.host}:{config.server.port}")

    print(f"\nProviders:")
    for name, prov in config.providers.items():
        print(f"  {name:16s} {prov.base_url}  (key: {_mask_key(prov.api_key)})")

    print(f"\nRoutes (default):")
    _print_route_table(config.routes)

    if config.schedule:
        now = datetime.now()
        now_min = now.hour * 60 + now.minute
        print(f"\nSchedule:")
        for entry in config.schedule:
            s, e = entry.start_minutes, entry.end_minutes
            start_s = f"{s // 60:02d}:{s % 60:02d}"
            end_s = f"{e // 60:02d}:{e % 60:02d}"
            if s < e:
                active = s <= now_min < e
            elif s > e:
                active = now_min >= s or now_min < e
            else:
                active = True
            label = entry.name or f"{start_s}-{end_s}"
            marker = " ← active" if active else ""
            print(f"  {label} ({start_s}-{end_s}){marker}:")
            _print_route_table(entry.routes, indent=4)


def cmd_config_set(args: argparse.Namespace) -> None:
    tier = args.tier.lower()
    target = args.target
    model_arg = args.model

    if tier not in TIER_PATTERNS:
        print(f"Unknown tier '{tier}'.")
        print(f"Available: {', '.join(TIER_PATTERNS)}")
        sys.exit(1)

    pattern = TIER_PATTERNS[tier]
    config_path = args.config

    if not os.path.exists(config_path):
        print(f"Config not found: {config_path}")
        print("Run 'flexgate config init' first.")
        sys.exit(1)

    config = load_config(config_path)

    provider_name: str | None = None
    model_override: str | None = model_arg

    if target in config.providers:
        # Target is a known provider name
        provider_name = target
    else:
        if model_arg is not None:
            # model arg given but target isn't a known provider
            print(f"Provider '{target}' not found.")
            print(f"Known providers: {', '.join(config.providers)}")
            sys.exit(1)

        # Try to resolve target as a model name from existing routes
        all_routes = list(config.routes)
        for entry in config.schedule:
            all_routes.extend(entry.routes)

        matches: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for route in all_routes:
            if route.model and route.model.lower() == target.lower():
                key = (route.provider_name, route.model)
                if key not in seen:
                    seen.add(key)
                    matches.append(key)

        if len(matches) == 1:
            provider_name, model_override = matches[0]
        elif len(matches) > 1:
            print(f"Ambiguous: '{target}' found in multiple providers:")
            for prov, mdl in sorted(matches):
                print(f"  flexgate config set {tier} {prov} {mdl}")
            sys.exit(1)
        else:
            print(f"Unknown target '{target}'.")
            print(f"Not a known provider or model name.\n")
            print(f"Known providers: {', '.join(config.providers)}")
            known_models = sorted({r.model for r in all_routes if r.model})
            if known_models:
                print(f"Known models: {', '.join(known_models)}")
            print(f"\nUsage: flexgate config set {tier} <provider> [model]")
            sys.exit(1)

    if provider_name not in config.providers:
        print(f"Provider '{provider_name}' not found in config.")
        print(f"Known providers: {', '.join(config.providers)}")
        print(f"\nAdd it to {config_path} first (base_url and api_key required).")
        sys.exit(1)

    provider = config.providers[provider_name]
    if model_override is None and not provider.available_models:
        print(f"Provider '{provider_name}' has no 'available_models' to fall back to.")
        print(f"Either add 'available_models' to provider '{provider_name}' in {config_path},")
        print(f"or specify a model: flexgate config set {tier} {provider_name} <model>")
        sys.exit(1)

    # Update existing route or insert new one
    updated = False
    for route in config.routes:
        if route.pattern.pattern == pattern:
            route.provider_name = provider_name
            route.model = model_override
            updated = True
            break

    if not updated:
        new_route = RouteConfig(
            pattern=re.compile(pattern),
            provider_name=provider_name,
            model=model_override,
        )
        # Insert before catch-all if it exists
        inserted = False
        for i, route in enumerate(config.routes):
            if route.pattern.pattern == ".*":
                config.routes.insert(i, new_route)
                inserted = True
                break
        if not inserted:
            config.routes.append(new_route)

    save_config(config, config_path)

    display = provider_name
    if model_override:
        display += f" / {model_override}"
    elif provider.available_models:
        display += f" / {provider.available_models[0]} (fallback)"
    print(f"Set {tier} ({pattern}) → {display}")
    print(f"Saved: {config_path}")


def cmd_config_path(args: argparse.Namespace) -> None:
    print(args.config)


# ── main ───────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="flexgate",
        description="Flexgate — Flexible API Gateway for Claude Code",
    )
    parser.add_argument("--config", default=None, help="Config file (default: ~/.flexgate/config.yaml)")

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
    gw_start.add_argument(
        "--no-verify", action="store_true",
        help="Skip upstream connectivity verification before starting"
    )
    gw_start.add_argument(
        "--verify-timeout", type=float, default=15.0,
        help="Per-provider connectivity check timeout in seconds (default: 15.0)"
    )
    gw_sub.add_parser("stop", help="Stop background gateway")
    gw_sub.add_parser("restart", help="Restart gateway")
    gw_sub.add_parser("status", help="Check gateway status")
    gw_sub.add_parser("run", help="Run gateway in foreground (for debug)")
    gw_check = gw_sub.add_parser("check", help="Verify upstream provider connectivity")
    gw_check.add_argument(
        "--verify-timeout", type=float, default=15.0,
        help="Per-provider connectivity check timeout in seconds (default: 15.0)"
    )

    # flexgate settings ...
    st = sub.add_parser("settings", help="Manage Claude Code settings")
    st_sub = st.add_subparsers(dest="command")
    st_sub.add_parser("import", help="Import ~/.claude/settings.json* into config.yaml")
    st_sub.add_parser("apply", help="Apply config.yaml to ~/.claude/settings.json")

    # flexgate config ...
    cf = sub.add_parser("config", help="View and manage configuration")
    cf_sub = cf.add_subparsers(dest="command")
    cf_sub.add_parser("init", help="Create default config at ~/.flexgate/config.yaml")
    cf_sub.add_parser("show", help="Show current configuration")
    cf_sub.add_parser("path", help="Print config file path")
    cf_set = cf_sub.add_parser("set", help="Set route for a tier")
    cf_set.add_argument("tier", help="Tier: opus, sonnet, or haiku")
    cf_set.add_argument("target", help="Provider name or model name")
    cf_set.add_argument("model", nargs="?", default=None, help="Model override (when target is a provider)")

    args = parser.parse_args()

    # Resolve config path and ensure home directory
    if args.config is None:
        args.config = get_default_config_path()
    ensure_home_dir()

    if args.group == "gateway":
        handlers = {
            "start": cmd_gateway_start,
            "stop": cmd_gateway_stop,
            "restart": cmd_gateway_restart,
            "status": cmd_gateway_status,
            "run": cmd_gateway_run,
            "check": cmd_gateway_check,
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

    elif args.group == "config":
        handlers = {
            "init": cmd_config_init,
            "show": cmd_config_show,
            "set": cmd_config_set,
            "path": cmd_config_path,
        }
        handler = handlers.get(args.command)
        if not handler:
            cf.print_help()
            sys.exit(1)
        handler(args)

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
