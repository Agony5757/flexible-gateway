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
* zhipu team→ GET {origin}/api/monitor/usage/quota/limit?type=2 with
              bigmodel-organization/bigmodel-project headers taken from the
              key entry's organization/project fields, plus the team
              subscription detail for the plan name/expiry; keys without
              both fields cannot be queried and get the plain probe
* LiteLLM   → GET {origin}/key/info   (e.g. USTC api.llm.ustc.edu.cn)
* anything else (e.g. Xiaomi MiMo, which has no key-based usage API)
            → minimal chat probe: POST /v1/messages with input "hi" and
              max_tokens=128 to verify the key can still serve requests

Alongside the human-readable ``lines``, every adapter fills a normalized
``QuotaInfo`` (plan name + 5-hour/weekly ``WindowQuota`` with remaining
percent and reset epoch) so the CLI can render one unified summary and
``--json`` consumers get structured fields. Data a platform did not report
(the "?" cases) stays None: an unknown balance is displayed as 0% and an
unknown reset time means no countdown is active.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlparse

import httpx

from flexgate.config import (
    FLEXGATE_HOME,
    ApiKey,
    GatewayConfig,
    ProviderConfig,
    is_placeholder_key,
)

logger = logging.getLogger("flexgate.usage")

# Minimal probe spec: input "hi", cap output at 128 tokens.
_PROBE_INPUT = "hi"
_PROBE_MAX_TOKENS = 128

# Keys whose usage check hard-failed are cached here and skipped by default
# until 'flexgate usage --force' rechecks them.
USAGE_CACHE_FILE = os.path.join(FLEXGATE_HOME, "usage-cache.json")
FORCE_HINT = "recheck with: flexgate usage --force"


@dataclass
class WindowQuota:
    """One quota window (5-hour or weekly), normalized across platforms.

    ``remaining_percent`` / ``reset_epoch`` stay None when the platform did
    not report them (displayed as 0% / "no countdown"; null in JSON).
    """

    remaining_percent: float | None = None
    reset_epoch: float | None = None  # epoch seconds
    window_hours: float | None = None  # e.g. 5.0 for a 5-hour window
    used_text: str | None = None  # raw usage text, e.g. "1513/35000 credits"


@dataclass
class QuotaInfo:
    plan: str | None = None
    five_hour: WindowQuota | None = None
    weekly: WindowQuota | None = None


@dataclass
class UsageResult:
    provider: str
    key_label: str  # e.g. "key #1 (sk-c***wxyz)"
    method: str  # how the information was obtained
    ok: bool
    lines: list[str] = field(default_factory=list)
    skipped: bool = False  # served from the failure cache, not queried now
    adapter_error: str | None = None  # adapter failed but the probe recovered
    quota: QuotaInfo | None = None  # normalized plan/window data (None for probe/cache results)
    index: int = 0  # 0-based position within the provider's key list
    masked_key: str = ""
    note: str = ""


def _mask_key(key: str) -> str:
    if len(key) <= 8:
        return "***"
    return key[:4] + "***" + key[-4:]


def _key_label(index: int, key: str, note: str) -> str:
    label = f"key #{index + 1} ({_mask_key(key)})"
    if note:
        label += f" [{note}]"
    return label


# ── failure cache ────────────────────────────────────────────────────


def _fingerprint(key: str) -> str:
    """Stable identity for a key — the cache never stores key material."""
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _load_fail_cache() -> dict[str, dict]:
    try:
        with open(USAGE_CACHE_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    entries = data.get("entries") if isinstance(data, dict) else None
    return dict(entries) if isinstance(entries, dict) else {}


def _save_fail_cache(entries: dict[str, dict]) -> None:
    tmp = USAGE_CACHE_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump({"entries": entries}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, USAGE_CACHE_FILE)
    except OSError:
        logger.debug("could not write usage cache", exc_info=True)


def _fmt_age(iso: str) -> str:
    try:
        then = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return "?"
    secs = max(0, (datetime.now() - then).total_seconds())
    if secs < 60:
        return f"{secs:.0f}s"
    if secs < 3600:
        return f"{secs / 60:.0f}m"
    if secs < 86400:
        return f"{secs / 3600:.0f}h"
    return f"{secs / 86400:.0f}d"


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


def _clamp_pct(value) -> float | None:
    try:
        pct = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(100.0, pct))


def _remaining_from_used_pct(used_pct) -> float | None:
    """remaining% = 100 - used%, clamped; None when the platform reported none."""
    used = _clamp_pct(used_pct)
    return None if used is None else 100.0 - used


def _pct_from_counts(part, total) -> float | None:
    """part/total as a percentage (both directions: used or remaining counts)."""
    try:
        total_f = float(total)
        part_f = float(part)
    except (TypeError, ValueError):
        return None
    if total_f <= 0:
        return None
    return max(0.0, min(100.0, part_f / total_f * 100.0))


def _remaining_from_used_counts(used, total) -> float | None:
    try:
        return float(total) - float(used)
    except (TypeError, ValueError):
        return None


def _epoch_ms(value) -> float | None:
    """Epoch milliseconds → epoch seconds; None when missing/invalid/zero."""
    if not value:
        return None
    try:
        secs = float(value) / 1000.0
    except (TypeError, ValueError):
        return None
    return secs if secs > 0 else None


def _epoch_from_iso(value) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


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
    client: httpx.AsyncClient, provider: ProviderConfig, entry: ApiKey, timeout: float
) -> tuple[list[str], QuotaInfo | None, str | None]:
    key = entry.key
    url = f"{_origin(provider.base_url)}/v1/api/openplatform/coding_plan/remains"
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    data, err = await _get_json(client, url, headers, timeout)
    if err:
        return [], None, err
    base_resp = data.get("base_resp", {})
    if base_resp.get("status_code", 0) != 0:
        return [], None, f"platform error {base_resp.get('status_code')}: {base_resp.get('status_msg', '')}"
    lines: list[str] = []
    quota = QuotaInfo()
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
        five = WindowQuota(window_hours=5.0, reset_epoch=_epoch_ms(entry.get("end_time")))
        if total:
            parts.append(f"5h window used {used}/{total} (reset {_fmt_epoch_ms(entry.get('end_time'))})")
            five.used_text = f"{used}/{total}"
            five.remaining_percent = _pct_from_counts(_remaining_from_used_counts(used, total), total)
        elif pct is not None:
            parts.append(f"5h window remaining {pct}% (reset {_fmt_epoch_ms(entry.get('end_time'))})")
            five.remaining_percent = _clamp_pct(pct)
        w_total = entry.get("current_weekly_total_count")
        w_used = entry.get("current_weekly_usage_count")
        w_pct = entry.get("current_weekly_remaining_percent")
        weekly = WindowQuota(reset_epoch=_epoch_ms(entry.get("weekly_end_time")))
        if w_total:
            parts.append(f"weekly used {w_used}/{w_total} (reset {_fmt_epoch_ms(entry.get('weekly_end_time'))})")
            weekly.used_text = f"{w_used}/{w_total}"
            weekly.remaining_percent = _pct_from_counts(_remaining_from_used_counts(w_used, w_total), w_total)
        elif w_pct is not None:
            parts.append(f"weekly remaining {w_pct}% (reset {_fmt_epoch_ms(entry.get('weekly_end_time'))})")
            weekly.remaining_percent = _clamp_pct(w_pct)
        quota.five_hour = five
        quota.weekly = weekly
        if parts:
            lines.append("; ".join(parts))
    return (lines or ["plan active (no text-model quota data)"]), quota, None


async def _usage_zai(
    client: httpx.AsyncClient, provider: ProviderConfig, entry: ApiKey, timeout: float
) -> tuple[list[str], QuotaInfo | None, str | None]:
    key = entry.key
    url = f"{_origin(provider.base_url)}/api/monitor/usage/quota/limit"
    headers = {"Authorization": key, "Content-Type": "application/json"}
    data, err = await _get_json(client, url, headers, timeout)
    if err:
        return [], None, err
    if not data.get("success", False):
        return [], None, f"platform error: {data.get('msg', 'unknown')}"
    payload = data.get("data", {}) or {}
    lines: list[str] = []
    quota = QuotaInfo(plan=payload.get("level") or None)
    for limit in payload.get("limits", []):
        if not isinstance(limit, dict):
            continue
        ltype = limit.get("type")
        if ltype == "TOKENS_LIMIT":
            # unit 3 = hours window, unit 6 = weekly window
            is_5h = limit.get("unit") == 3
            window = f"{limit.get('number', '?')}h" if is_5h else "weekly"
            lines.append(
                f"{window} window: {limit.get('percentage', '?')}% used "
                f"(reset {_fmt_epoch_ms(limit.get('nextResetTime'))})"
            )
            wq = WindowQuota(
                remaining_percent=_remaining_from_used_pct(limit.get("percentage")),
                reset_epoch=_epoch_ms(limit.get("nextResetTime")),
            )
            if is_5h:
                try:
                    wq.window_hours = float(limit.get("number"))
                except (TypeError, ValueError):
                    pass
                quota.five_hour = wq
            else:
                quota.weekly = wq
        elif ltype == "TIME_LIMIT":
            lines.append(f"tool calls (monthly): remaining {limit.get('remaining', '?')}/{limit.get('usage', '?')}")
    if quota.plan:
        lines.insert(0, f"plan: {quota.plan}")
    return lines or ["no quota data"], quota, None


def _zhipu_team_headers(entry: ApiKey) -> dict[str, str]:
    return {
        "Authorization": entry.key,
        "Content-Type": "application/json",
        "bigmodel-organization": entry.organization,
        "bigmodel-project": entry.project,
    }


async def _zhipu_team_detail(
    client: httpx.AsyncClient, provider: ProviderConfig, entry: ApiKey, timeout: float
) -> dict | None:
    """Best-effort team subscription detail (productName, expiry, auto-renew)."""
    url = f"{_origin(provider.base_url)}/api/biz/team/subscribe/product/querySubscribeDetail"
    data, err = await _get_json(client, url, _zhipu_team_headers(entry), timeout)
    if err or not data.get("success", False):
        return None
    detail = data.get("data") or {}
    return detail if detail.get("productName") else None


def _zhipu_team_plan_line(detail: dict) -> str:
    line = f"plan: {detail.get('productName')}"
    end = detail.get("subscribeEndTime")
    if end:
        line += f" (until {end}"
        if detail.get("autoRenew") == 0:
            line += ", auto-renew off"
        line += ")"
    return line


async def _usage_zhipu_team(
    client: httpx.AsyncClient, provider: ProviderConfig, entry: ApiKey, timeout: float
) -> tuple[list[str], QuotaInfo | None, str | None]:
    url = f"{_origin(provider.base_url)}/api/monitor/usage/quota/limit?type=2"
    data, err = await _get_json(client, url, _zhipu_team_headers(entry), timeout)
    if err:
        return [], None, err
    if not data.get("success", False):
        return [], None, f"platform error: {data.get('msg', 'unknown')}"
    payload = data.get("data", {}) or {}
    limits = payload.get("limits") or []
    if not limits:
        # type=2 answers success with empty limits when the key does not
        # belong to the configured organization/project.
        return [], None, "empty limits — key is not a member of the configured organization/project"
    lines: list[str] = []
    quota = QuotaInfo()
    detail = await _zhipu_team_detail(client, provider, entry, timeout)
    if detail:
        quota.plan = detail.get("productName")
        lines.append(_zhipu_team_plan_line(detail))
    for limit in limits:
        if not isinstance(limit, dict):
            continue
        # team plans are credit-metered; keep TOKENS_LIMIT handling anyway
        if limit.get("type") not in ("CREDIT_LIMIT", "TOKENS_LIMIT"):
            continue
        # unit 3 = N-hour rolling window, unit 6 = weekly window
        is_5h = limit.get("unit") == 3
        window = f"{limit.get('number', '?')}h" if is_5h else "weekly"
        lines.append(
            f"{window} window: used {limit.get('currentValue', '?')}/{limit.get('usage', '?')} "
            f"credits ({limit.get('percentage', '?')}%), reset {_fmt_epoch_ms(limit.get('nextResetTime'))}"
        )
        wq = WindowQuota(
            remaining_percent=_remaining_from_used_pct(limit.get("percentage")),
            reset_epoch=_epoch_ms(limit.get("nextResetTime")),
            used_text=f"{limit.get('currentValue', '?')}/{limit.get('usage', '?')} credits",
        )
        if is_5h:
            try:
                wq.window_hours = float(limit.get("number"))
            except (TypeError, ValueError):
                pass
            quota.five_hour = wq
        else:
            quota.weekly = wq
    return lines or ["no quota data"], quota, None


async def _usage_litellm(
    client: httpx.AsyncClient, provider: ProviderConfig, entry: ApiKey, timeout: float
) -> tuple[list[str], QuotaInfo | None, str | None]:
    key = entry.key
    url = f"{_origin(provider.base_url)}/key/info"
    headers = {"Authorization": f"Bearer {key}"}
    data, err = await _get_json(client, url, headers, timeout)
    if err:
        return [], None, err
    info = data.get("info", data)
    if not isinstance(info, dict):
        return [], None, "unexpected response shape"
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
    return lines or ["key valid (no quota fields exposed)"], None, None


async def _usage_kimi(
    client: httpx.AsyncClient, provider: ProviderConfig, entry: ApiKey, timeout: float
) -> tuple[list[str], QuotaInfo | None, str | None]:
    key = entry.key
    base = provider.base_url.rstrip("/")
    url = f"{base}/usages" if base.endswith("/v1") else f"{base}/v1/usages"
    headers = {"Authorization": f"Bearer {key}"}
    data, err = await _get_json(client, url, headers, timeout)
    if err:
        return [], None, err
    lines: list[str] = []
    quota = QuotaInfo()
    level = ((data.get("user") or {}).get("membership") or {}).get("level")
    if level:
        quota.plan = str(level).removeprefix("LEVEL_").lower()
        lines.append(f"plan: {quota.plan}")
    weekly = data.get("usage") or {}
    if weekly.get("limit"):
        lines.append(
            f"weekly remaining {weekly.get('remaining', '?')}/{weekly['limit']} "
            f"(reset {_fmt_iso(weekly.get('resetTime'))})"
        )
        quota.weekly = WindowQuota(
            remaining_percent=_pct_from_counts(weekly.get("remaining"), weekly.get("limit")),
            reset_epoch=_epoch_from_iso(weekly.get("resetTime")),
            used_text=f"{weekly.get('remaining', '?')}/{weekly['limit']}",
        )
    for limit in data.get("limits", []):
        if not isinstance(limit, dict):
            continue
        window = limit.get("window") or {}
        detail = limit.get("detail") or {}
        if window.get("timeUnit") != "TIME_UNIT_MINUTE":
            continue
        try:
            hours = int(window.get("duration")) / 60
            label = f"{int(window.get('duration')) // 60}h"
        except (TypeError, ValueError):
            hours = None
            label = "rate"
        lines.append(
            f"{label} window remaining {detail.get('remaining', '?')}/{detail.get('limit', '?')} "
            f"(reset {_fmt_iso(detail.get('resetTime'))})"
        )
        # The short rolling window (5h) is the "five_hour" quota; longer or
        # unknown windows stay in the raw lines only.
        if hours is not None and hours <= 12:
            quota.five_hour = WindowQuota(
                remaining_percent=_pct_from_counts(detail.get("remaining"), detail.get("limit")),
                reset_epoch=_epoch_from_iso(detail.get("resetTime")),
                window_hours=hours,
                used_text=f"{detail.get('remaining', '?')}/{detail.get('limit', '?')}",
            )
    parallel = (data.get("parallel") or {}).get("limit")
    if parallel:
        lines.append(f"parallel limit: {parallel}")
    return lines or ["key valid (no quota data)"], quota, None


# (url substring, adapter, method label). First match wins.
_ADAPTERS = [
    ("minimaxi.com", _usage_minimax, "MiniMax coding_plan API"),
    ("minimax.io", _usage_minimax, "MiniMax coding_plan API"),
    ("api.kimi.com", _usage_kimi, "Kimi Code usages API (unofficial)"),
    ("z.ai", _usage_zai, "z.ai quota API (unofficial)"),
    ("bigmodel.cn", _usage_zhipu_team, "zhipu team quota API (unofficial)"),
    ("llm.ustc.edu.cn", _usage_litellm, "LiteLLM /key/info"),
]

# Platforms known to have NO key-based usage API — go straight to the probe.
_PROBE_ONLY_MARKERS = ("xiaomimimo.com",)

# bigmodel.cn marker, reused to explain why a key got no quota adapter.
_ZHIPU_MARKER = "bigmodel.cn"


def _find_adapter(base_url: str, entry: ApiKey):
    """First matching adapter for a base_url. Returns (adapter, label).

    A bigmodel.cn (zhipu) team key whose entry lacks organization/project has
    no quota endpoint to query — the personal coding-plan endpoint does not
    apply to team keys — so it returns (None, None) and falls through to the
    availability probe.
    """
    for marker, adapter, label in _ADAPTERS:
        if marker not in base_url:
            continue
        if adapter is _usage_zhipu_team and not (entry.organization and entry.project):
            return None, None
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
    label = _key_label(index, key, entry.note)
    meta = {
        "index": index,
        "masked_key": _mask_key(key),
        "note": entry.note,
    }
    if is_placeholder_key(key):
        return UsageResult(provider.name, label, "-", False, ["api_key looks like a placeholder"], **meta)

    if any(marker in provider.base_url for marker in _PROBE_ONLY_MARKERS):
        adapter, method = None, None
    else:
        adapter, method = _find_adapter(provider.base_url, entry)

    if adapter is not None:
        lines, quota, err = await adapter(client, provider, entry, timeout)
        if err is None:
            return UsageResult(provider.name, label, method, True, lines, quota=quota, **meta)
        logger.debug("usage adapter %s failed for %s: %s; falling back to probe", method, provider.name, err)
        probe_lines, probe_err = await _probe_chat(client, provider, key, timeout)
        note = f"{method} failed ({err}); probe: "
        if probe_err is None:
            return UsageResult(
                provider.name, label, method, True, [note + probe_lines[0]],
                adapter_error=err, **meta,
            )
        return UsageResult(provider.name, label, method, False, [note + probe_err], **meta)

    lines, err = await _probe_chat(client, provider, key, timeout)
    method = f'minimal chat probe ("{_PROBE_INPUT}", max_tokens={_PROBE_MAX_TOKENS})'
    if err is None:
        if _ZHIPU_MARKER in provider.base_url and not (entry.organization and entry.project):
            lines = lines + [
                "team quota not queried: set organization/project on this key entry"
            ]
        return UsageResult(provider.name, label, method, True, lines, **meta)
    return UsageResult(provider.name, label, method, False, [err], **meta)


async def _check_key_cached(
    client: httpx.AsyncClient,
    provider: ProviderConfig,
    entry: ApiKey,
    index: int,
    timeout: float,
    view: dict[str, dict],
    cache: dict[str, dict],
) -> tuple[UsageResult, bool]:
    """check_key_usage plus failure-cache bookkeeping.

    `view` holds the failed checks to skip on this run (empty when forced);
    `cache` is the live copy that gets written back. Any result carrying an
    error (hard failure, or adapter error recovered by the probe) is cached
    and skipped next time. Returns (result, cache_changed).
    """
    key = entry.key
    fp = _fingerprint(key)
    hit = view.get(fp)
    if hit is not None:
        label = _key_label(index, key, entry.note)
        ok = bool(hit.get("ok"))
        lines = list(hit.get("lines") or [hit.get("error") or "unknown error"])
        age = _fmt_age(hit.get("failed_at", ""))
        method = f"cached ({age} ago)" if ok else f"cached failure ({age} ago)"
        return UsageResult(
            provider.name, label, method, ok, lines + [FORCE_HINT], skipped=True,
            index=index, masked_key=_mask_key(key), note=entry.note,
        ), False
    result = await check_key_usage(client, provider, entry, index, timeout)
    if is_placeholder_key(key):
        return result, False  # local config state, not a remote failure
    if result.ok and result.adapter_error is None:
        if fp in cache:
            del cache[fp]
            return result, True
        return result, False
    cache[fp] = {
        "provider": provider.name,
        "ok": result.ok,
        "lines": [line[:400] for line in result.lines][:4],
        "method": result.method,
        "failed_at": datetime.now().isoformat(timespec="seconds"),
    }
    return result, True


async def check_all_usage(
    config: GatewayConfig, timeout: float = 15.0, force: bool = False
) -> dict[str, list[UsageResult]]:
    """Query usage for every key of every provider, concurrently.

    Keys whose previous check hard-failed are served from the failure cache
    (marked `skipped`) unless `force` is set; the cache is updated afterwards.
    """
    results: dict[str, list[UsageResult]] = {}
    cache = _load_fail_cache()
    view: dict[str, dict] = {} if force else cache
    async with httpx.AsyncClient() as client:
        tasks = []
        refs: list[tuple[str, int]] = []
        for name, provider in config.providers.items():
            results[name] = [None] * len(provider.api_keys)  # type: ignore[list-item]
            for index, entry in enumerate(provider.api_keys):
                tasks.append(_check_key_cached(client, provider, entry, index, timeout, view, cache))
                refs.append((name, index))
        done = await asyncio.gather(*tasks)
    changed = False
    for (name, index), (result, touched) in zip(refs, done):
        results[name][index] = result
        changed = changed or touched
    if changed:
        _save_fail_cache(cache)
    return results


def run_usage_check(
    config: GatewayConfig, timeout: float = 15.0, force: bool = False
) -> dict[str, list[UsageResult]]:
    return asyncio.run(check_all_usage(config, timeout, force=force))


def result_to_dict(result: UsageResult, *, verbose: bool = False, now: float | None = None) -> dict:
    """One UsageResult as a JSON-ready dict.

    Semantics for missing platform data: an unknown remaining percent is
    reported as 0.0; an unknown reset time yields null (no countdown active).
    """
    now = time.time() if now is None else now

    def window(wq: WindowQuota | None) -> dict | None:
        if wq is None:
            return None
        pct = wq.remaining_percent if wq.remaining_percent is not None else 0.0
        out: dict = {
            "remaining_percent": round(pct, 1),
            "remaining_seconds": (
                max(0, int(wq.reset_epoch - now)) if wq.reset_epoch is not None else None
            ),
            "reset_at": (
                datetime.fromtimestamp(wq.reset_epoch).astimezone().isoformat(timespec="seconds")
                if wq.reset_epoch is not None
                else None
            ),
        }
        if wq.window_hours is not None:
            out["window_hours"] = wq.window_hours
        if wq.used_text is not None:
            out["used"] = wq.used_text
        return out

    quota = result.quota
    payload: dict = {
        "index": result.index + 1,
        "key": result.masked_key,
        "note": result.note or None,
        "ok": result.ok,
        "skipped": result.skipped,
        "method": result.method,
        "adapter_error": result.adapter_error,
        "plan": quota.plan if quota else None,
        "five_hour": window(quota.five_hour) if quota else None,
        "weekly": window(quota.weekly) if quota else None,
    }
    if verbose:
        payload["raw_lines"] = list(result.lines)
    return payload
