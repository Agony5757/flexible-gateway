# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Flexgate is a local Anthropic-compatible API gateway (~1800 lines of Python). It sits between Claude Code and multiple upstream LLM providers, routing requests by model name/tier (opus/sonnet/haiku) to different backends. The core problem it solves: Claude Code only supports one `ANTHROPIC_BASE_URL`.

## Commands

```bash
# Install / dev setup
uv sync                                    # install dependencies
uv run flexgate config init                 # create default config
uv run flexgate gateway run                 # foreground run (debug)
uv run flexgate gateway start               # background with guardian

# Global install (recommended for daily use)
uv tool install -e .
flexgate gateway start

# Lifecycle
flexgate gateway {start|stop|restart|status|run|check}

# Config
flexgate config {init|show|set|path}
flexgate config set all <provider>          # batch-set all tiers
flexgate config set sonnet minimax MiniMax-M3

# Claude Code settings bridge
flexgate settings import                    # read from ~/.claude/settings.json
flexgate settings apply                     # write ANTHROPIC_BASE_URL → localhost

# Hot reload (no restart needed)
# config set already sends SIGUSR1 to running gateway
kill -USR1 $(cat ~/.flexgate/flexgate.pid)  # manual trigger
```

There is no test suite, linter, or CI configured.

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
| `cli.py` | argparse CLI, PID management, daemon subprocess spawning, guardian launch |
| `config.py` | Pydantic-like dataclasses (`GatewayConfig`, `ProviderConfig`, `RouteConfig`, `ScheduleEntry`), YAML load/save, `TIER_PATTERNS` regex map |
| `router.py` | `resolve(config, model)` — schedule-first then default routes, first regex match wins |
| `proxy.py` | `handle_request()` — httpx async proxy, SSE streaming + JSON pass-through |
| `server.py` | Starlette app creation, `POST /v1/messages` endpoint, `SIGUSR1` lifespan reload |
| `main.py` | Thin bootstrap: load config → create app → run uvicorn |
| `guardian.py` | Background port monitor using `/proc/net/tcp` and `/proc/<pid>/fd` (Linux-only) |
| `healthcheck.py` | Pre-flight `POST /v1/messages` (max_tokens=1) to each referenced (provider, model) pair |
| `settings.py` | Bridges `config.yaml` ↔ `~/.claude/settings.json` (import credentials, apply config) |

### Key design points

- **Regex-first routing**: Routes are regex patterns matched against the `model` field in the request body. First match wins. A catch-all `".*"` pattern at the end handles fallback.
- **Schedule-based overrides**: Optional time windows (e.g. 22:00-06:00) override default routes. Overnight wrap is supported.
- **Hot config reload**: `SIGUSR1` tells the running gateway to re-read `config.yaml` without restart.
- **Port guardian**: Separate process monitors port 8765, detects conflicts via Linux proc filesystem.
- **Tier patterns** in `config.py`: `opus`, `sonnet`, `haiku` map to regex patterns for CLI shorthand (`config set sonnet ...`).

### Config location

All runtime files live in `~/.flexgate/`: `config.yaml`, `flexgate.pid`, `flexgate.guardian.pid`, `flexgate.log`. Override with `FLEXGATE_CONFIG` env var.

## Python Style

- Python >= 3.11, uses dataclasses (not Pydantic), stdlib argparse
- Async throughout: `async def` handlers, `httpx.AsyncClient`, `uvicorn`
- No type checking, linting, or formatting tools configured
