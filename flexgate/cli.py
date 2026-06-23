from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time

import re

try:
    import curses
except ImportError:  # pragma: no cover - curses is unavailable on some platforms
    curses = None  # type: ignore[assignment]

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


def _set_tier_route(
    config: GatewayConfig, tier_name: str, provider_name: str, model_override: str | None
) -> None:
    """Update the default route for a tier, or insert one before the catch-all."""
    pattern = TIER_PATTERNS[tier_name]
    for route in config.routes:
        if route.pattern.pattern == pattern:
            route.provider_name = provider_name
            route.model = model_override
            return

    new_route = RouteConfig(
        pattern=re.compile(pattern),
        provider_name=provider_name,
        model=model_override,
    )
    # Insert before the catch-all (".*") if present, else append.
    for i, route in enumerate(config.routes):
        if route.pattern.pattern == ".*":
            config.routes.insert(i, new_route)
            return
    config.routes.append(new_route)


def _signal_reload() -> str | None:
    """Send SIGUSR1 to a running gateway (if any). Return a status message, or None."""
    pid = _read_pid()
    if pid and _is_running(pid):
        try:
            os.kill(pid, signal.SIGUSR1)
            return f"Gateway (PID {pid}) signaled to reload config"
        except (PermissionError, ProcessLookupError) as e:
            return f"Warning: could not signal gateway: {e}"
    return None


def _hot_reload() -> None:
    """Signal a running gateway (if any) to reload config via SIGUSR1."""
    msg = _signal_reload()
    if msg:
        print(msg)


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

    if tier == "all":
        tiers = list(TIER_PATTERNS)
    else:
        tiers = [t.strip() for t in tier.split(",") if t.strip()]
        unknown = [t for t in tiers if t not in TIER_PATTERNS]
        if not tiers or unknown:
            bad = unknown[0] if unknown else tier
            print(f"Unknown tier '{bad}'.")
            print(f"Available: all, {', '.join(TIER_PATTERNS)}")
            print("Combine multiple tiers with commas, e.g. opus,sonnet")
            sys.exit(1)
        # De-duplicate while preserving order
        seen_tiers: set[str] = set()
        tiers = [t for t in tiers if not (t in seen_tiers or seen_tiers.add(t))]

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

    # Update existing route or insert new one for each target tier
    for tier_name in tiers:
        _set_tier_route(config, tier_name, provider_name, model_override)

    save_config(config, config_path)

    display = provider_name
    if model_override:
        display += f" / {model_override}"
    elif provider.available_models:
        display += f" / {provider.available_models[0]} (fallback)"
    for tier_name in tiers:
        print(f"Set {tier_name} ({TIER_PATTERNS[tier_name]}) → {display}")
    print(f"Saved: {config_path}")

    # Hot-reload: signal the running gateway to pick up the new config
    _hot_reload()


def cmd_config_path(args: argparse.Namespace) -> None:
    print(args.config)


# ── config edit (interactive arrow-key TUI) ───────────────────────

_CANCEL = object()  # sentinel: user backed out of a menu
_CUSTOM = object()  # sentinel: user chose "enter a custom model"


def _tier_current(config: GatewayConfig, tier_name: str) -> tuple[str | None, str | None]:
    """Return (provider_name, model) for a tier's default route, or (None, None)."""
    pattern = TIER_PATTERNS[tier_name]
    for route in config.routes:
        if route.pattern.pattern == pattern:
            return route.provider_name, route.model
    return None, None


def _format_target(provider_name: str | None, model: str | None) -> str:
    if provider_name is None:
        return "(unset)"
    if model:
        return f"{provider_name} / {model}"
    return f"{provider_name} / (provider default)"


# ── curses drawing primitives ─────────────────────────────────────

def _addline(stdscr, y: int, x: int, text: str, attr: int = 0) -> None:
    h, w = stdscr.getmaxyx()
    if 0 <= y < h and x < w:
        try:
            stdscr.addnstr(y, x, text, max(0, w - 1 - x), attr)
        except Exception:
            pass


def _menu_draw(stdscr, header_lines, option_labels, index, footer_lines=()) -> None:
    stdscr.erase()
    h, _ = stdscr.getmaxyx()

    y = 0
    for line in header_lines:
        _addline(stdscr, y, 0, line, curses.A_BOLD if y == 0 else curses.A_NORMAL)
        y += 1

    footer_h = len(footer_lines)
    list_top = y
    visible = max(1, h - footer_h - list_top)
    n = len(option_labels)
    if n <= visible:
        offset = 0
    else:
        offset = min(max(0, index - visible // 2), n - visible)

    yy = list_top
    for i in range(offset, min(n, offset + visible)):
        selected = i == index
        marker = "▶ " if selected else "  "
        attr = (curses.A_REVERSE | curses.A_BOLD) if selected else curses.A_NORMAL
        _addline(stdscr, yy, 0, marker + option_labels[i], attr)
        yy += 1

    fy = h - footer_h
    for line in footer_lines:
        _addline(stdscr, fy, 0, line)
        fy += 1

    stdscr.refresh()


def _menu_select(stdscr, header_lines, options, index: int = 0):
    """Arrow-key menu. options: list[(label, value)]. Returns value or _CANCEL."""
    n = len(options)
    if n == 0:
        return _CANCEL
    index = max(0, min(index, n - 1))
    footer = ["", "↑/↓ move · Enter select · Esc/← back"]
    while True:
        _menu_draw(stdscr, header_lines, [o[0] for o in options], index, footer)
        key = stdscr.getch()
        if key in (curses.KEY_UP, ord("k")):
            index = (index - 1) % n
        elif key in (curses.KEY_DOWN, ord("j")):
            index = (index + 1) % n
        elif key == curses.KEY_HOME:
            index = 0
        elif key == curses.KEY_END:
            index = n - 1
        elif key in (curses.KEY_ENTER, 10, 13):
            return options[index][1]
        elif key in (27, ord("q"), curses.KEY_LEFT):
            return _CANCEL


def _text_input(stdscr, prompt: str) -> str | None:
    h, w = stdscr.getmaxyx()
    y = h - 1
    _addline(stdscr, y, 0, " " * (w - 1))
    _addline(stdscr, y, 0, prompt, curses.A_BOLD)
    stdscr.refresh()
    curses.echo()
    try:
        curses.curs_set(1)
    except Exception:
        pass
    try:
        raw = stdscr.getstr(y, min(len(prompt), w - 2), 200)
        text = raw.decode("utf-8", "replace").strip()
    except Exception:
        text = ""
    finally:
        curses.noecho()
        try:
            curses.curs_set(0)
        except Exception:
            pass
    return text or None


def _confirm(stdscr, question: str):
    """Yes/No prompt. Returns True, False, or None (cancel)."""
    sel = _menu_select(
        stdscr,
        [question, ""],
        [("Yes, save changes", True), ("No, discard changes", False)],
        index=0,
    )
    return None if sel is _CANCEL else sel


# ── tier edit flow ─────────────────────────────────────────────────

def _edit_tier(stdscr, config: GatewayConfig, provider_names: list[str], tier: str) -> bool:
    """Pick a provider then a model for `tier`. Returns True if a change was applied."""
    cur_prov, cur_model = _tier_current(config, tier)

    prov_options = []
    for name in provider_names:
        prov = config.providers[name]
        models = ", ".join(prov.available_models) if prov.available_models else "(no models listed)"
        prov_options.append((f"{name:16s} {models}", name))

    start = provider_names.index(cur_prov) if cur_prov in provider_names else 0
    header = [
        f"Tier '{tier}'  —  current: {_format_target(cur_prov, cur_model)}",
        "Choose a provider:",
        "",
    ]
    provider_name = _menu_select(stdscr, header, prov_options, index=start)
    if provider_name is _CANCEL:
        return False

    prov = config.providers[provider_name]
    model_options: list[tuple[str, object]] = [
        (m + ("   (provider default)" if i == 0 else ""), m)
        for i, m in enumerate(prov.available_models)
    ]
    if prov.available_models:
        model_options.append(("Use provider default (first available model)", None))
    model_options.append(("Custom model…", _CUSTOM))

    midx = 0
    if provider_name == cur_prov:
        if cur_model is None and prov.available_models:
            midx = len(prov.available_models)  # the explicit "provider default" row
        else:
            for i, (_, val) in enumerate(model_options):
                if val == cur_model:
                    midx = i
                    break

    header = [
        f"Tier '{tier}'  —  provider: {provider_name}",
        "Choose a model:",
        "",
    ]
    selection = _menu_select(stdscr, header, model_options, index=midx)
    if selection is _CANCEL:
        return False

    if selection is _CUSTOM:
        text = _text_input(stdscr, f"Custom model name for {provider_name}: ")
        if not text:
            return False
        model: str | None = text
    else:
        model = selection  # type: ignore[assignment]  # str or None

    # Guard against creating an invalid route (no model + no fallback).
    if model is None and not prov.available_models:
        return False

    _set_tier_route(config, tier, provider_name, model)
    return True


def _edit_loop(stdscr, config: GatewayConfig, config_path: str):
    """Main TUI loop. Returns (outcome, reload_msg, saved_any)."""
    try:
        curses.curs_set(0)
    except Exception:
        pass
    stdscr.keypad(True)

    tiers = list(TIER_PATTERNS)
    provider_names = list(config.providers)
    index = 0
    dirty = False
    saved_any = False
    reload_msg: str | None = None
    status = ""

    while True:
        header = [
            f"Flexgate config  —  {os.path.abspath(config_path)}",
            "↑/↓ move · Enter edit tier · s save · q quit",
            "",
        ]
        labels = []
        for tier in tiers:
            prov, model = _tier_current(config, tier)
            labels.append(f"{tier:8s}  {_format_target(prov, model)}")
        footer = [
            "",
            "● unsaved changes" if dirty else "○ no unsaved changes",
            status,
        ]
        _menu_draw(stdscr, header, labels, index, footer)

        key = stdscr.getch()
        if key in (curses.KEY_UP, ord("k")):
            index = (index - 1) % len(tiers)
            status = ""
        elif key in (curses.KEY_DOWN, ord("j")):
            index = (index + 1) % len(tiers)
            status = ""
        elif key in (curses.KEY_ENTER, 10, 13):
            if _edit_tier(stdscr, config, provider_names, tiers[index]):
                dirty = True
                prov, model = _tier_current(config, tiers[index])
                status = f"Set {tiers[index]} → {_format_target(prov, model)}"
            else:
                status = ""
        elif key == ord("s"):
            if dirty:
                save_config(config, config_path)
                reload_msg = _signal_reload()
                saved_any = True
                dirty = False
                status = "Saved" + (f" · {reload_msg}" if reload_msg else "")
            else:
                status = "No changes to save"
        elif key in (ord("q"), 27):
            if dirty:
                ans = _confirm(stdscr, "Unsaved changes — save before quitting?")
                if ans is True:
                    save_config(config, config_path)
                    reload_msg = _signal_reload()
                    return ("saved", reload_msg, True)
                if ans is False:
                    return ("discarded", reload_msg, saved_any)
                status = ""  # cancelled the quit
                continue
            return ("saved" if saved_any else "clean", reload_msg, saved_any)


def cmd_config_edit(args: argparse.Namespace) -> None:
    config_path = args.config
    if not os.path.exists(config_path):
        print(f"Config not found: {config_path}")
        print("Run 'flexgate config init' first.")
        sys.exit(1)

    config = load_config(config_path)
    if not config.providers:
        print("No providers configured.")
        print(f"Add providers to {config_path} or run 'flexgate settings import' first.")
        sys.exit(1)

    if curses is None:
        print("Interactive editor requires the 'curses' module (unavailable on this platform).")
        print("Use 'flexgate config set <tier> <provider> [model]' instead.")
        sys.exit(1)
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("Interactive editor requires an interactive terminal (TTY).")
        print("Use 'flexgate config set <tier> <provider> [model]' for non-interactive changes.")
        sys.exit(1)

    outcome, reload_msg, saved_any = curses.wrapper(_edit_loop, config, config_path)

    if outcome == "saved":
        print(f"Saved: {config_path}")
        if reload_msg:
            print(reload_msg)
    elif outcome == "discarded":
        if saved_any:
            print(f"Saved earlier changes to {config_path}; discarded changes since last save.")
            if reload_msg:
                print(reload_msg)
        else:
            print("Discarded changes.")
    else:
        print("No changes.")


# ── service subcommands ────────────────────────────────────────────

def cmd_service_install(args: argparse.Namespace) -> None:
    from flexgate.service import service_install
    service_install(args.config, start=not getattr(args, "no_start", False))


def cmd_service_uninstall(args: argparse.Namespace) -> None:
    from flexgate.service import service_uninstall
    service_uninstall()


def cmd_service_start(args: argparse.Namespace) -> None:
    from flexgate.service import service_start
    service_start()


def cmd_service_stop(args: argparse.Namespace) -> None:
    from flexgate.service import service_stop
    service_stop()


def cmd_service_status(args: argparse.Namespace) -> None:
    from flexgate.service import service_status
    service_status()


def cmd_service_help(args: argparse.Namespace) -> None:
    from flexgate.service import service_help
    service_help()


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
    cf_sub.add_parser("edit", help="Interactively choose provider/model per tier (opus/sonnet/haiku)")
    cf_set = cf_sub.add_parser("set", help="Set route for a tier")
    cf_set.add_argument("tier", help="Tier: all, or comma-separated opus,sonnet,haiku")
    cf_set.add_argument("target", help="Provider name or model name")
    cf_set.add_argument("model", nargs="?", default=None, help="Model override (when target is a provider)")

    # flexgate service ...
    svc = sub.add_parser("service", help="Manage flexgate as a systemd user service")
    svc_sub = svc.add_subparsers(dest="command")
    svc_install = svc_sub.add_parser("install", help="Install + enable the systemd user service")
    svc_install.add_argument(
        "--no-start", action="store_true",
        help="Install and enable the service but do not start it now"
    )
    svc_sub.add_parser("uninstall", help="Stop, disable and remove the systemd user service")
    svc_sub.add_parser("start", help="Start the service")
    svc_sub.add_parser("stop", help="Stop the service")
    svc_sub.add_parser("status", help="Show service status")
    svc_sub.add_parser("help", help="Show service command help")

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
            "edit": cmd_config_edit,
            "path": cmd_config_path,
        }
        handler = handlers.get(args.command)
        if not handler:
            cf.print_help()
            sys.exit(1)
        handler(args)

    elif args.group == "service":
        handlers = {
            "install": cmd_service_install,
            "uninstall": cmd_service_uninstall,
            "start": cmd_service_start,
            "stop": cmd_service_stop,
            "status": cmd_service_status,
            "help": cmd_service_help,
        }
        handler = handlers.get(args.command)
        if not handler:
            svc.print_help()
            sys.exit(1)
        handler(args)

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
