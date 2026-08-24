"""Per-provider usage / quota inspection.

Every provider platform exposes a different way to check remaining quota
(and some expose none at all). This module maps a provider's base_url to a
known adapter and queries it with the provider's own API key:

* MiniMax   → GET {origin}/v1/api/openplatform/coding_plan/remains
              (officially documented token-plan endpoint; reports per-quota
              entries — we keep only the "general" text-model one, where
              counts are usually 0/0 and usage shows up in
              *_remaining_percent fields instead)
* Kimi Code → GET {base}/v1/usages
              (undocumented endpoint used by Kimi Code CLI's /usage;
              returns weekly quota, 5-hour window and parallel limit)
* z.ai      → GET {origin}/api/monitor/usage/quota/limit
              (undocumented but used by z.ai's own coding plugin)
* LiteLLM   → GET {origin}/key/info   (e.g. USTC api.llm.ustc.edu.cn)
* anything else (e.g. Xiaomi MiMo, which has no key-based usage API)
            → minimal chat probe: POST /v1/messages with input "hi" and
              max_tokens=128 to verify the key can still serve requests
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlparse

import httpx

from flexgate.config import ApiKey, GatewayConfig, ProviderConfig
from flexgate.healthcheck import _is_placeholder_key

logger = logging.getLogger("flexgate.usage")

# Minimal probe spec: input "hi", cap output at 128 tokens.
_PROBE_INPUT = "hi"
_PROBE_MAX_TOKENS = 128


@dataclass
class UsageResult:
    provider: str
    key_label: str  # e.g. "key #1 (sk-c***wxyz)"
    method: str  # how the information was obtained
    ok: bool
    lines: list[str] = field(default_factory=list)


def _mask_key(key: str) -> str:
    if len(key) <= 8:
        return "***"
    return key[:4] + "***" + key[-4:]


def _origin(base_url: str) -> str:
    parts = urlparse(base_url)
    return f"{parts.scheme}://{parts.netloc}"


def _fmt_epoch_ms(value: int | float | None) -> str:
    if not value:
        return "?"
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(float(value) / 1000))
    except (ValueError, OverflowError, OSError):
        return "?"


def _fmt_epoch_s(value) -> str:
    if not value:
        return "?"
    try:
        if isinstance(value, str):
            # LiteLLM returns ISO timestamps for budget_reset_at
            return value.replace("T", " ")[:16]
        return time.strftime("%m-%d %H:%M", time.localtime(float(value)))
    except (ValueError, OverflowError, OSError):
        return "?"


def _fmt_iso(value) -> str:
    """Format an ISO-8601 timestamp (e.g. Kimi's resetTime) in local time."""
    if not value:
        return "?"
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.astimezone().strftime("%m-%d %H:%M")
    except ValueError:
        return "?"


async def _get_json(
    client: httpx.AsyncClient, url: str, headers: dict[str, str], timeout: float
) -> tuple[dict | None, str | None]:
    """GET helper. Returns (json_dict, error_message)."""
    try:
        resp = await client.get(url, headers=headers, timeout=timeout)
    except httpx.TimeoutException:
        return None, f"timeout after {timeout:g}s"
    except httpx.HTTPError as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if resp.status_code != 200:
        return None, f"HTTP {resp.status_code} — {resp.text[:200]}"
    try:
        data = resp.json()
    except ValueError:
        return None, "response is not JSON"
    if not isinstance(data, dict):
        return None, "unexpected response shape"
    return data, None


# ── platform adapters ───────────────────────────────────────────────


async def _usage_minimax(
    client: httpx.AsyncClient, provider: ProviderConfig, key: str, timeout: float
) -> tuple[list[str], str | None]:
    url = f"{_origin(provider.base_url)}/v1/api/openplatform/coding_plan/remains"
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    data, err = await _get_json(client, url, headers, timeout)
    if err:
        return [], err
    base_resp = data.get("base_resp", {})
    if base_resp.get("status_code", 0) != 0:
        return [], f"platform error {base_resp.get('status_code')}: {base_resp.get('status_msg', '')}"
    lines: list[str] = []
    for entry in data.get("model_remains", []):
        if not isinstance(entry, dict):
            continue
        # model_remains also carries non-text quotas ("video", ...); only the
        # "general" entry describes the text/coding model quota.
        if entry.get("model_name") != "general":
            continue
        parts = []
        # The text plan reports counts as 0/0; usage is only exposed via the
        # *_remaining_percent fields, so prefer counts when present, else percent.
        total = entry.get("current_interval_total_count")
        used = entry.get("current_interval_usage_count")
        pct = entry.get("current_interval_remaining_percent")
        if total:
            parts.append(f"5h window used {used}/{total} (reset {_fmt_epoch_ms(entry.get('end_time'))})")
        elif pct is not None:
            parts.append(f"5h window remaining {pct}% (reset {_fmt_epoch_ms(entry.get('end_time'))})")
        w_total = entry.get("current_weekly_total_count")
        w_used = entry.get("current_weekly_usage_count")
        w_pct = entry.get("current_weekly_remaining_percent")
        if w_total:
            parts.append(f"weekly used {w_used}/{w_total} (reset {_fmt_epoch_ms(entry.get('weekly_end_time'))})")
        elif w_pct is not None:
            parts.append(f"weekly remaining {w_pct}% (reset {_fmt_epoch_ms(entry.get('weekly_end_time'))})")
        if parts:
            lines.append("; ".join(parts))
    return lines or ["plan active (no text-model quota data)"], None


async def _usage_zai(
    client: httpx.AsyncClient, provider: ProviderConfig, key: str, timeout: float
) -> tuple[list[str], str | None]:
    url = f"{_origin(provider.base_url)}/api/monitor/usage/quota/limit"
    headers = {"Authorization": key, "Content-Type": "application/json"}
    data, err = await _get_json(client, url, headers, timeout)
    if err:
        return [], err
    if not data.get("success", False):
        return [], f"platform error: {data.get('msg', 'unknown')}"
    payload = data.get("data", {}) or {}
    lines: list[str] = []
    level = payload.get("level")
    for limit in payload.get("limits", []):
        if not isinstance(limit, dict):
            continue
        ltype = limit.get("type")
        if ltype == "TOKENS_LIMIT":
            # unit 3 = hours window, unit 6 = weekly window
            window = f"{limit.get('number', '?')}h" if limit.get("unit") == 3 else "weekly"
            lines.append(
                f"{window} window: {limit.get('percentage', '?')}% used "
                f"(reset {_fmt_epoch_ms(limit.get('nextResetTime'))})"
            )
        elif ltype == "TIME_LIMIT":
            lines.append(f"tool calls (monthly): remaining {limit.get('remaining', '?')}/{limit.get('usage', '?')}")
    if level:
        lines.insert(0, f"plan: {level}")
    return lines or ["no quota data"], None


async def _usage_litellm(
    client: httpx.AsyncClient, provider: ProviderConfig, key: str, timeout: float
) -> tuple[list[str], str | None]:
    url = f"{_origin(provider.base_url)}/key/info"
    headers = {"Authorization": f"Bearer {key}"}
    data, err = await _get_json(client, url, headers, timeout)
    if err:
        return [], err
    info = data.get("info", data)
    if not isinstance(info, dict):
        return [], "unexpected response shape"
    lines: list[str] = []
    spend = info.get("spend")
    max_budget = info.get("max_budget")
    if spend is not None:
        budget = f"${max_budget}" if max_budget else "no budget cap"
        reset = info.get("budget_reset_at")
        suffix = f", reset {_fmt_epoch_s(reset)}" if reset else ""
        lines.append(f"spend ${spend:.4f} / {budget}{suffix}")
    tpm, rpm = info.get("tpm_limit"), info.get("rpm_limit")
    if tpm or rpm:
        lines.append(f"limits: tpm={tpm or '-'} rpm={rpm or '-'}")
    if info.get("blocked"):
        lines.append("KEY IS BLOCKED")
    expires = info.get("expires")
    if expires:
        lines.append(f"expires {_fmt_epoch_s(expires)}")
    return lines or ["key valid (no quota fields exposed)"], None


async def _usage_kimi(
    client: httpx.AsyncClient, provider: ProviderConfig, key: str, timeout: float
) -> tuple[list[str], str | None]:
    base = provider.base_url.rstrip("/")
    url = f"{base}/usages" if base.endswith("/v1") else f"{base}/v1/usages"
    headers = {"Authorization": f"Bearer {key}"}
    data, err = await _get_json(client, url, headers, timeout)
    if err:
        return [], err
    lines: list[str] = []
    level = ((data.get("user") or {}).get("membership") or {}).get("level")
    if level:
        lines.append(f"plan: {str(level).removeprefix('LEVEL_').lower()}")
    weekly = data.get("usage") or {}
    if weekly.get("limit"):
        lines.append(
            f"weekly remaining {weekly.get('remaining', '?')}/{weekly['limit']} "
            f"(reset {_fmt_iso(weekly.get('resetTime'))})"
        )
    for limit in data.get("limits", []):
        if not isinstance(limit, dict):
            continue
        window = limit.get("window") or {}
        detail = limit.get("detail") or {}
        if window.get("timeUnit") != "TIME_UNIT_MINUTE":
            continue
        try:
            label = f"{int(window.get('duration')) // 60}h"
        except (TypeError, ValueError):
            label = "rate"
        lines.append(
            f"{label} window remaining {detail.get('remaining', '?')}/{detail.get('limit', '?')} "
            f"(reset {_fmt_iso(detail.get('resetTime'))})"
        )
    parallel = (data.get("parallel") or {}).get("limit")
    if parallel:
        lines.append(f"parallel limit: {parallel}")
    return lines or ["key valid (no quota data)"], None


# (url substring, adapter, method label). First match wins.
_ADAPTERS = [
    ("minimaxi.com", _usage_minimax, "MiniMax coding_plan API"),
    ("minimax.io", _usage_minimax, "MiniMax coding_plan API"),
    ("api.kimi.com", _usage_kimi, "Kimi Code usages API (unofficial)"),
    ("z.ai", _usage_zai, "z.ai quota API (unofficial)"),
    ("bigmodel.cn", _usage_zai, "z.ai quota API (unofficial)"),
    ("llm.ustc.edu.cn", _usage_litellm, "LiteLLM /key/info"),
]

# Platforms known to have NO key-based usage API — go straight to the probe.
_PROBE_ONLY_MARKERS = ("xiaomimimo.com",)


def _find_adapter(base_url: str):
    for marker, adapter, label in _ADAPTERS:
        if marker in base_url:
            return adapter, label
    return None, None


async def _probe_chat(
    client: httpx.AsyncClient, provider: ProviderConfig, key: str, timeout: float
) -> tuple[list[str], str | None]:
    """Minimal availability probe: input "hi", max_tokens=128."""
    model = provider.default_model or "claude-haiku-4-5"
    url = f"{provider.base_url}/v1/messages"
    headers = {
        "content-type": "application/json",
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
    }
    body = {
        "model": model,
        "max_tokens": _PROBE_MAX_TOKENS,
        "messages": [{"role": "user", "content": _PROBE_INPUT}],
    }
    try:
        resp = await client.post(url, headers=headers, json=body, timeout=timeout)
    except httpx.TimeoutException:
        return [], f"timeout after {timeout:g}s"
    except httpx.HTTPError as exc:
        return [], f"{type(exc).__name__}: {exc}"
    if resp.status_code == 200:
        detail = ""
        try:
            usage = resp.json().get("usage", {})
            out = usage.get("output_tokens")
            if out is not None:
                detail = f" ({out} output tokens used)"
        except ValueError:
            pass
        return [f"key can serve {model}{detail}"], None
    return [], f"HTTP {resp.status_code} — {resp.text[:200]}"


async def check_key_usage(
    client: httpx.AsyncClient,
    provider: ProviderConfig,
    entry: ApiKey,
    index: int,
    timeout: float,
) -> UsageResult:
    """Check usage/availability of one key of one provider."""
    key = entry.key
    label = f"key #{index + 1} ({_mask_key(key)})"
    if entry.note:
        label += f" [{entry.note}]"
    if _is_placeholder_key(key):
        return UsageResult(provider.name, label, "-", False, ["api_key looks like a placeholder"])

    if any(marker in provider.base_url for marker in _PROBE_ONLY_MARKERS):
        adapter, method = None, None
    else:
        adapter, method = _find_adapter(provider.base_url)

    if adapter is not None:
        lines, err = await adapter(client, provider, key, timeout)
        if err is None:
            return UsageResult(provider.name, label, method, True, lines)
        logger.debug("usage adapter %s failed for %s: %s; falling back to probe", method, provider.name, err)
        probe_lines, probe_err = await _probe_chat(client, provider, key, timeout)
        note = f"{method} failed ({err}); probe: "
        if probe_err is None:
            return UsageResult(provider.name, label, method, True, [note + probe_lines[0]])
        return UsageResult(provider.name, label, method, False, [note + probe_err])

    lines, err = await _probe_chat(client, provider, key, timeout)
    method = f'minimal chat probe ("{_PROBE_INPUT}", max_tokens={_PROBE_MAX_TOKENS})'
    if err is None:
        return UsageResult(provider.name, label, method, True, lines)
    return UsageResult(provider.name, label, method, False, [err])


async def check_all_usage(
    config: GatewayConfig, timeout: float = 15.0
) -> dict[str, list[UsageResult]]:
    """Query usage for every key of every provider, concurrently."""
    results: dict[str, list[UsageResult]] = {}
    async with httpx.AsyncClient() as client:
        tasks = []
        refs: list[tuple[str, int]] = []
        for name, provider in config.providers.items():
            results[name] = [None] * len(provider.api_keys)  # type: ignore[list-item]
            for index, entry in enumerate(provider.api_keys):
                tasks.append(check_key_usage(client, provider, entry, index, timeout))
                refs.append((name, index))
        done = await asyncio.gather(*tasks)
    for (name, index), result in zip(refs, done):
        results[name][index] = result
    return results


def run_usage_check(config: GatewayConfig, timeout: float = 15.0) -> dict[str, list[UsageResult]]:
    return asyncio.run(check_all_usage(config, timeout))
