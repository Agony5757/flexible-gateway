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
flexgate status                             # providers, fallback chains, per-key usage, active routes
flexgate usage [--force]                    # per-key usage/quota only; --force rechecks cached failures
flexgate log                                # service journal (-f follow, -r route lines only)

# Config
flexgate config {init|show|set|path|edit}
flexgate config set all <provider>          # batch-set all tiers
flexgate config set sonnet minimax MiniMax-M3
flexgate config edit                        # interactive curses TUI: pick provider/model per tier

# Claude Code settings bridge
flexgate settings import                    # read from ~/.claude/settings.json* (merges keys into api_keys)
flexgate settings apply                     # rewrite the flexgate env keys → localhost (other fields preserved)
flexgate settings apply --dry-run           # preview the env diff without writing

# confsync config sync
flexgate sync                               # pull: replace local config.yaml with the remote document (backup first)
flexgate sync push                          # upload local config.yaml to the server
flexgate sync --dry-run                     # preview changes without writing

# Versioning / upgrades
flexgate --version                          # print package version
flexgate doctor                             # diagnose install/config + upstream connectivity (exit 1 on failure)
flexgate doctor --offline                   # skip all network checks (PyPI + upstream probing)
flexgate check                              # DEPRECATED alias: delegates to doctor
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
  → server.py   (Starlette app, /v1/messages + /v1/images/* routes)
  → router.py   (resolve(): regex match model, check schedule windows first, then default routes)
  → proxy.py    (rewrite model field if override, swap x-api-key, SSE streaming pass-through)
  → Upstream provider (z.ai, minimax, xiaomi, etc.)

OpenAI-style client → POST /v1/images/generations
  → images.py   (translate to MiniMax /v1/image_generation; provider auto-detected by base_url)
```

### Source files (`flexgate/`)

| File | Role |
|------|------|
| `cli.py` | argparse CLI; service lifecycle commands, foreground `run`, config/settings/sync commands, `flexgate log` journal viewer |
| `ui.py` | Shared terminal styling: ANSI helpers (`bold`/`dim`/`red`/...), `ok`/`warn`/`fail`/`err` output helpers, `FlexgateHelpFormatter` + `FlexgateParser` (colored help on every Python version; all gated by `NO_COLOR` + stdout isatty) |
| `config.py` | Pydantic-like dataclasses (`GatewayConfig`, `ProviderConfig`, `RouteConfig`, `ScheduleEntry`), YAML load/save, `TIER_PATTERNS` regex map, `is_placeholder_key` |
| `router.py` | `resolve(config, model)` — schedule-first then default routes, first regex match wins; model aliases normalized before matching |
| `proxy.py` | `handle_request()` — httpx async proxy, per-key fallback retry loop, SSE streaming + JSON pass-through |
| `usage.py` | `flexgate status` usage queries — per-platform adapters (MiniMax coding_plan API, Kimi Code usages API, z.ai quota API, LiteLLM `/key/info`) plus a minimal chat probe fallback |
| `server.py` | Starlette app creation, `POST /v1/messages` + `POST /v1/images/{generations,edits}` endpoints, `SIGUSR1` lifespan reload |
| `images.py` | OpenAI Images API → MiniMax `/v1/image_generation` translation (`image-01`, size→width/height clamped to 512–2048 mult of 8, `data.image_base64[]`→`data[].b64_json`); edits endpoint returns 501; unknown image model names get 501 `model_not_supported` |
| `fallback.py` | Catch-all route: merged Anthropic/OpenAI-shaped JSON errors for every unserved path — 404 `route_not_found` (lists valid routes), 405 `method_not_allowed`, 501 `endpoint_not_supported` per recognized API family with an actionable reason |
| `main.py` | Thin bootstrap: load config → create app → run uvicorn |
| `service.py` | Authoritative systemd user-service install/start/stop/restart/reload/status and legacy PID migration |
| `healthcheck.py` | Upstream connectivity probing (`POST /v1/messages`, max_tokens=1) for each referenced (provider, model) pair; called by `doctor` (`flexgate check` is a deprecated alias of doctor) |
| `settings.py` | Bridges `config.yaml` ↔ `~/.claude/settings.json`: import merges credentials into `api_keys`; apply rewrites only the managed env keys, preserving every other settings.json field |
| `sync.py` | `flexgate sync` — pushes/pulls the whole config.yaml as an encrypted document on a confsync server (lazily imports the `confsync` client package) |
| `migrate.py` | Config schema versioning: `config_version` marker, per-step `MIGRATIONS` chain (N → N+1), backup + atomic rewrite |
| `doctor.py` | `flexgate doctor` — read-only diagnostics (Python, PyPI update, config schema/semantics, port, systemd, Claude settings) plus upstream connectivity via `healthcheck` (skipped with `--offline`) |
| `update.py` | `flexgate update` — PyPI version check, package upgrade via detected installer (pipx/uv/pip), config migration, service reload; after a successful package upgrade the config migration is re-run in a fresh process so it uses the new code's schema version (`FLEXGATE_UPDATE_DELEGATED` guards against re-delegation); also the cached (24h) new-version notice shown by bare `flexgate` / `service status` |

### Key design points

- **Regex-first routing**: Routes are regex patterns matched against the `model` field in the request body. First match wins. A catch-all `".*"` pattern at the end handles fallback.
- **Model alias normalization** (`router.py`): before matching, bare aliases (`sonnet`/`opus`/`haiku`/`default`, case-insensitive, optional `[plan]` suffix) map to `claude-<tier>` prefixes (`default` → sonnet, Claude Code's default tier), and legacy numbered names (`claude-3-7-sonnet-latest`) reduce to their tier prefix; everything else passes through unchanged, so custom patterns and the catch-all behave exactly as before.
- **Model resolution & `available_models` fallback**: A route may omit `model`; `router.resolve()` then falls back to the provider's first `available_models` entry, so `model_override` handed to the proxy is always a concrete name. `config._parse_routes` rejects routes that omit `model` on a provider with no `available_models` — so adding a provider without models requires an explicit `model` on every route using it.
- **Proxy rewrite contract** (`proxy.py`): the upstream request gets the provider's `x-api-key` plus a fixed header set, and the JSON `model` field is rewritten only when the route set an override. Streaming responses are forwarded as raw bytes (`aiter_bytes`), never parsed.
- **Key fallback** (`proxy.py`): a provider's `api_keys` list holds one or more keys for the same upstream (entries are key strings or `{key, note}` mappings; `note` is a free-form label shown by `flexgate status` and in fallback logs). Multiple accounts on one upstream — e.g. several MiniMax subscriptions — belong in ONE provider, not separate ones. Each *route* has an active-key pointer (`active_key` in YAML, 1-based; `RouteConfig.key_index` 0-based, default first key): requests on that route start from the pointed key, and on a retryable failure (HTTP 401/402/403/429/500/502/503/529, connect error, or timeout) the next key is tried and the pointer advances with it (in memory only, wrapping around — it is not written back to the file); every key is tried at most once and a full circle of failures returns the last error. The pointer is set interactively via `flexgate config edit` → "api keys" (pick a route, with live per-key usage) or by editing the route's `active_key` directly. `resolve()` (`router.py`) returns the matched `RouteConfig` so the proxy can read/advance its pointer. Streaming requests can only fall back before the upstream returns 200. `_regular_proxy`/`_stream_proxy` return `(response, retryable)`; `handle_request` owns the loop and returns the last error when all keys fail. The legacy `api_key` + `fallback_keys` schema is still parsed (`config._parse_api_keys`); migration v3 → v4 rewrites it to `api_keys`. `ProviderConfig.api_key` remains as a first-key property (getter/setter) for the settings import code.
- **Usage inspection** (`usage.py`): `flexgate status` maps `base_url` to a platform adapter (`_ADAPTERS`): MiniMax → `{origin}/v1/api/openplatform/coding_plan/remains` (only the "general" text-model quota entry is used; its counts are usually 0/0, so usage is read from the `*_remaining_percent` fields), Kimi Code → `{base}/v1/usages` (undocumented, same endpoint as the CLI's `/usage`; reports weekly quota, 5-hour window and parallel limit), z.ai/bigmodel → `{origin}/api/monitor/usage/quota/limit` (undocumented but used by z.ai's own plugin), USTC/LiteLLM → `{origin}/key/info`. Hosts in `_PROBE_ONLY_MARKERS` (e.g. xiaomimimo.com — no key-based usage API) and unknown platforms get the minimal chat probe: `POST /v1/messages` with input `"hi"`, `max_tokens=128`. A failed adapter call also degrades to the probe. Any key whose usage query errored — a hard failure (adapter AND probe), or an adapter error merely recovered by the probe — is recorded in `~/.flexgate/usage-cache.json` (sha256 fingerprint, never key material; entry keeps the `ok` flag and output lines) and skipped by later `status`/`usage` runs, shown as `cached (… ago)` / `cached failure (… ago)` with a hint; `flexgate usage --force` rechecks them and a clean success clears the entry. Placeholder keys are never cached.
- **Multimodal degradation**: `MULTIMODAL_MODELS` (currently `{"MiniMax-M3", "glm-4.6v"}`) is the allowlist. Requests carrying image blocks aimed at any other model have images stripped and a `[flexgate]` text note injected into both the outgoing request and the returned response, so non-multimodal backends don't 4xx.
- **Schedule-based overrides**: Optional time windows (e.g. 22:00-06:00) override default routes. Overnight wrap is supported.
- **Service-first lifecycle**: `flexgate.service` is the sole persistent runtime. systemd owns restart, boot startup, logs, and process state.
- **Hot config reload**: `flexgate service reload` sends `SIGUSR1` for routing-only changes and restarts when the applied config path or endpoint changed. Same-port host changes require an explicit stop/start.
- **Conflict prevention**: Service startup removes stale legacy PID files, stops verified legacy Flexgate daemons, validates the configured port, and rejects temporary config paths.
- **Tier patterns** in `config.py`: `opus`, `sonnet`, `haiku` map to regex patterns for CLI shorthand (`config set sonnet ...`).
- **Single-source versioning**: the package version lives only in `flexgate/__init__.py` (`__version__`); hatchling reads it via `[tool.hatch.version]`. `--version`, `service status` and the bare `flexgate` command all print it.
- **Settings apply is non-destructive** (`settings.py`): only the six managed `env` keys are rewritten (BASE_URL, AUTH_TOKEN, API_TIMEOUT_MS, three `ANTHROPIC_DEFAULT_*_MODEL`); every other settings.json field (hooks, permissions, unrelated env vars) is preserved. Token policy is one code path shared with `service install`: explicit arg → token already in the file → `"gateway"`. `settings import` merges into the provider's `api_keys` list (appends new keys, never overwrites key #1 only).
- **Terminal styling** (`ui.py`): every colored output goes through `ui.bold`/`dim`/`ok`/`err`/... — all gated on `NO_COLOR` + stdout isatty, so pipes never receive escape codes. `err()` writes to stderr. Help screens use `FlexgateParser` (subparsers inherit it automatically); on Python 3.14+ argparse's own help theming is disabled so the look matches older versions.
- **Config schema versioning**: `config.yaml` carries `config_version` (current: `migrate.CURRENT_CONFIG_VERSION`). Each schema change adds one rule to `migrate.MIGRATIONS` upgrading N → N+1; upgrades walk the chain step by step. `save_config` always stamps the current version; `load_config` rejects configs written by a newer flexgate; `flexgate update` applies pending migrations with a timestamped backup.

### Config location

Config lives at `~/.flexgate/config.yaml` (override with `FLEXGATE_CONFIG`).
`flexgate sync` needs no flexgate-side setup — connection details come from
the shared confsync credentials written by `confsync login --server <url>`
(`~/.confsync/credentials.json`). The whole config.yaml is synced as one
encrypted document (app `flexgate`, name `config.yaml`): pull always replaces
the local file with the remote document (a timestamped backup is kept), or
bootstraps the file when missing. The `confsync-client` package is a declared
dependency (PyPI), so sync works out of the box once logged in.
The persistent unit lives at `~/.config/systemd/user/flexgate.service`; logs are
in the systemd user journal. `~/.flexgate/service-state.json` records the last
successfully applied config path and endpoint. PID/guardian files are legacy artifacts only.

## Python Style

- Python >= 3.11, uses dataclasses (not Pydantic), stdlib argparse
- Async throughout: `async def` handlers, `httpx.AsyncClient`, `uvicorn`
- No type checking, linting, or formatting tools configured
