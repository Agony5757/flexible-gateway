# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Flexgate is a local Anthropic-compatible API gateway (~1800 lines of Python). It sits between Claude Code and multiple upstream LLM providers, routing requests by model name/tier (opus/sonnet/haiku) to different backends. The core problem it solves: Claude Code only supports one `ANTHROPIC_BASE_URL`.

## Commands

```bash
# Install / dev setup
uv sync                                    # install dependencies
uv run flexgate config init                 # create default config
uv run flexgate run                         # foreground run (debug)

# Global install (recommended for daily use)
uv tool install -e .
flexgate service install                    # install + enable + start systemd user service

# Lifecycle
flexgate service {install|start|stop|restart|reload|status|uninstall}
flexgate run                                # foreground/debug only
flexgate check                              # provider connectivity diagnostics

# Config
flexgate config {init|show|set|path|edit}
flexgate config set all <provider>          # batch-set all tiers
flexgate config set sonnet minimax MiniMax-M3
flexgate config edit                        # interactive curses TUI: pick provider/model per tier

# Claude Code settings bridge
flexgate settings import                    # read from ~/.claude/settings.json
flexgate settings apply                     # write ANTHROPIC_BASE_URL → localhost

# confsync config sync
flexgate sync                               # pull config.yaml from your confsync server (merge keys)
flexgate sync push                          # upload local config.yaml to the server
flexgate sync --full                        # replace local config with the remote document (backup first)
flexgate sync --dry-run                     # preview changes without writing

# Versioning / upgrades
flexgate --version                          # print package version
flexgate doctor                             # diagnose install/config problems (exit 1 on failure)
flexgate update                             # upgrade package via pip/pipx/uv + migrate config schema
flexgate update --check                     # report what would change, modify nothing
flexgate update --config-only               # only migrate the config schema

# Hot reload (no restart needed)
# config set already reloads the active service; endpoint changes trigger restart
flexgate service reload
```

There is no test suite or linter configured. CI is a single GitHub Actions
release workflow (`.github/workflows/release.yml`): pushing a `v*` tag verifies
the tag matches `__version__`, builds sdist/wheel, and publishes to PyPI via
trusted publishing. The canonical repo is
<https://github.com/Agony5757/flexible-gateway> (`origin`; the old Gitea remote
is kept as `gitea`).

## Runtime authority

The systemd user service is the only persistent serving mode on Linux.
`flexgate run` remains a single foreground process for development or systems
without a systemd user instance.

## Architecture

### Request flow

```
Claude Code → POST /v1/messages (model="claude-sonnet-4-6")
  → server.py   (Starlette app, single route)
  → router.py   (resolve(): regex match model, check schedule windows first, then default routes)
  → proxy.py    (rewrite model field if override, swap x-api-key, SSE streaming pass-through)
  → Upstream provider (z.ai, minimax, xiaomi, etc.)
```

### Source files (`flexgate/`)

| File | Role |
|------|------|
| `cli.py` | argparse CLI; service lifecycle commands, foreground `run`/`check`, config/settings/sync commands |
| `config.py` | Pydantic-like dataclasses (`GatewayConfig`, `ProviderConfig`, `RouteConfig`, `ScheduleEntry`), YAML load/save, `TIER_PATTERNS` regex map |
| `router.py` | `resolve(config, model)` — schedule-first then default routes, first regex match wins |
| `proxy.py` | `handle_request()` — httpx async proxy, SSE streaming + JSON pass-through |
| `server.py` | Starlette app creation, `POST /v1/messages` endpoint, `SIGUSR1` lifespan reload |
| `main.py` | Thin bootstrap: load config → create app → run uvicorn |
| `service.py` | Authoritative systemd user-service install/start/stop/restart/reload/status and legacy PID migration |
| `guardian.py` | Legacy port/PID helper; no longer owns persistent process supervision |
| `healthcheck.py` | Pre-flight `POST /v1/messages` (max_tokens=1) to each referenced (provider, model) pair |
| `settings.py` | Bridges `config.yaml` ↔ `~/.claude/settings.json` (import credentials, apply config) |
| `sync.py` | `flexgate sync` — pushes/pulls the whole config.yaml as an encrypted document on a confsync server (lazily imports the `confsync` client package) |
| `migrate.py` | Config schema versioning: `config_version` marker, per-step `MIGRATIONS` chain (N → N+1), backup + atomic rewrite |
| `doctor.py` | `flexgate doctor` — read-only diagnostics (Python, PyPI update, config schema/semantics, port, systemd, Claude settings) |
| `update.py` | `flexgate update` — PyPI version check, package upgrade via detected installer (pipx/uv/pip), config migration, service reload; also the cached (24h) new-version notice shown by bare `flexgate` / `service status` |

### Key design points

- **Regex-first routing**: Routes are regex patterns matched against the `model` field in the request body. First match wins. A catch-all `".*"` pattern at the end handles fallback.
- **Model resolution & `available_models` fallback**: A route may omit `model`; `router.resolve()` then falls back to the provider's first `available_models` entry, so `model_override` handed to the proxy is always a concrete name. `config._parse_routes` rejects routes that omit `model` on a provider with no `available_models` — so adding a provider without models requires an explicit `model` on every route using it.
- **Proxy rewrite contract** (`proxy.py`): the upstream request gets the provider's `x-api-key` plus a fixed header set, and the JSON `model` field is rewritten only when the route set an override. Streaming responses are forwarded as raw bytes (`aiter_bytes`), never parsed.
- **Multimodal degradation**: `MULTIMODAL_MODELS` (currently `{"MiniMax-M3", "glm-4.6v"}`) is the allowlist. Requests carrying image blocks aimed at any other model have images stripped and a `[flexgate]` text note injected into both the outgoing request and the returned response, so non-multimodal backends don't 4xx.
- **Schedule-based overrides**: Optional time windows (e.g. 22:00-06:00) override default routes. Overnight wrap is supported.
- **Service-first lifecycle**: `flexgate.service` is the sole persistent runtime. systemd owns restart, boot startup, logs, and process state.
- **Hot config reload**: `flexgate service reload` sends `SIGUSR1` for routing-only changes and restarts when the applied config path or endpoint changed. Same-port host changes require an explicit stop/start.
- **Conflict prevention**: Service startup removes stale legacy PID files, stops verified legacy Flexgate daemons, validates the configured port, and rejects temporary config paths.
- **Tier patterns** in `config.py`: `opus`, `sonnet`, `haiku` map to regex patterns for CLI shorthand (`config set sonnet ...`).
- **Single-source versioning**: the package version lives only in `flexgate/__init__.py` (`__version__`); hatchling reads it via `[tool.hatch.version]`. `--version`, `service status` and the bare `flexgate` command all print it.
- **Config schema versioning**: `config.yaml` carries `config_version` (current: `migrate.CURRENT_CONFIG_VERSION`). Each schema change adds one rule to `migrate.MIGRATIONS` upgrading N → N+1; upgrades walk the chain step by step. `save_config` always stamps the current version; `load_config` rejects configs written by a newer flexgate; `flexgate update` applies pending migrations with a timestamped backup.

### Config location

Config lives at `~/.flexgate/config.yaml` (override with `FLEXGATE_CONFIG`).
`flexgate sync` needs no flexgate-side setup — connection details come from
the shared confsync credentials written by `confsync login --server <url>`
(`~/.confsync/credentials.json`). The whole config.yaml is synced as one
encrypted document (app `flexgate`, name `config.yaml`): pull merges
provider api_keys and imports remote-only providers (local routes/schedules
untouched), bootstraps the file when missing, and `--full` replaces it with a
timestamped backup. The `confsync-client` package is a declared dependency
(PyPI), so sync works out of the box once logged in.
The persistent unit lives at `~/.config/systemd/user/flexgate.service`; logs are
in the systemd user journal. `~/.flexgate/service-state.json` records the last
successfully applied config path and endpoint. PID/guardian files are legacy artifacts only.

## Python Style

- Python >= 3.11, uses dataclasses (not Pydantic), stdlib argparse
- Async throughout: `async def` handlers, `httpx.AsyncClient`, `uvicorn`
- No type checking, linting, or formatting tools configured
