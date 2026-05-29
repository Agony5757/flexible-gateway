"""Provider connectivity verification.

Tests each unique ``(provider, model)`` combination referenced by the
configured routes (default + schedule) by issuing a minimal
``POST /v1/messages`` request with ``max_tokens=1``.

This catches the common pre-flight failure modes before the gateway
starts accepting traffic:

* DNS / TCP / TLS unreachable (wrong base_url, no network)
* Invalid or expired API key (401 / 403)
* Provider-side outage (5xx)
* Unknown model name for that provider (404 / 400)
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import httpx

from flexgate.config import GatewayConfig, ProviderConfig

logger = logging.getLogger("flexgate.healthcheck")

# Fallback model used when a provider is referenced only by routes without
# an explicit ``model:`` field. May produce a 404 but still exercises the
# auth path.
_FALLBACK_TEST_MODEL = "claude-haiku-4-5"

# Heuristic placeholders that the default template ships with; we don't
# want to hit upstream with these.
_PLACEHOLDER_MARKERS = ("your-", "xxx", "changeme", "placeholder")


@dataclass
class CheckResult:
    provider: str
    model: str | None
    ok: bool
    status: int | None
    message: str

    @property
    def target(self) -> str:
        if self.model:
            return f"{self.provider} / {self.model}"
        return self.provider


def _is_placeholder_key(key: str) -> bool:
    if not key:
        return True
    low = key.lower()
    return any(marker in low for marker in _PLACEHOLDER_MARKERS)


def _collect_targets(config: GatewayConfig) -> list[tuple[ProviderConfig, str | None]]:
    """Collect unique ``(provider, model)`` targets to verify.

    De-duplicates across the default routes and every schedule entry. If a
    provider is configured but never referenced by any route, it is still
    probed once with no model override so misconfigured base_urls/keys are
    surfaced.
    """
    seen: set[tuple[str, str | None]] = set()
    targets: list[tuple[ProviderConfig, str | None]] = []

    all_routes = list(config.routes)
    for entry in config.schedule:
        all_routes.extend(entry.routes)

    for route in all_routes:
        provider = config.providers.get(route.provider_name)
        if provider is None:
            continue
        key = (route.provider_name, route.model)
        if key in seen:
            continue
        seen.add(key)
        targets.append((provider, route.model))

    covered = {name for name, _ in seen}
    for name, prov in config.providers.items():
        if name not in covered:
            targets.append((prov, None))

    return targets


async def _check_one(
    client: httpx.AsyncClient,
    provider: ProviderConfig,
    model: str | None,
    timeout: float,
) -> CheckResult:
    if _is_placeholder_key(provider.api_key):
        return CheckResult(
            provider.name, model, False, None,
            "api_key looks like a placeholder — edit your config.yaml",
        )

    url = f"{provider.base_url}/v1/messages"
    headers = {
        "content-type": "application/json",
        "x-api-key": provider.api_key,
        "anthropic-version": "2023-06-01",
    }
    body = {
        "model": model or _FALLBACK_TEST_MODEL,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "ping"}],
    }

    try:
        resp = await client.post(url, headers=headers, json=body, timeout=timeout)
    except httpx.ConnectError as exc:
        return CheckResult(provider.name, model, False, None, f"connect error: {exc}")
    except httpx.TimeoutException:
        return CheckResult(provider.name, model, False, None, f"timeout after {timeout:g}s")
    except httpx.HTTPError as exc:
        return CheckResult(
            provider.name, model, False, None,
            f"{type(exc).__name__}: {exc}",
        )

    status = resp.status_code

    if status == 200:
        return CheckResult(provider.name, model, True, status, "ok")

    # Try to extract a structured error message.
    detail = ""
    try:
        data = resp.json()
        if isinstance(data, dict):
            err = data.get("error", data)
            if isinstance(err, dict):
                etype = err.get("type", "")
                emsg = err.get("message", "")
                detail = f"{etype}: {emsg}".strip(": ").strip()
            else:
                detail = str(err)[:200]
    except ValueError:
        text = (resp.text or "").strip()
        if text:
            detail = text[:200]

    message = f"HTTP {status}" + (f" — {detail}" if detail else "")

    # 401/403 → auth failure (fatal). 5xx → upstream issue (fatal).
    # 4xx with valid auth (e.g. model-not-found when we used the fallback
    # model) → connection works; report as warning if the route had no
    # explicit model, otherwise still fail.
    if status in (401, 403):
        return CheckResult(provider.name, model, False, status, message)
    if status >= 500:
        return CheckResult(provider.name, model, False, status, message)
    if model is None and status in (400, 404):
        return CheckResult(
            provider.name, model, True, status,
            f"reachable (HTTP {status} on fallback model probe)",
        )
    return CheckResult(provider.name, model, False, status, message)


async def check_providers_async(
    config: GatewayConfig,
    timeout: float = 15.0,
) -> list[CheckResult]:
    targets = _collect_targets(config)
    if not targets:
        return []
    async with httpx.AsyncClient() as client:
        tasks = [_check_one(client, p, m, timeout) for p, m in targets]
        return await asyncio.gather(*tasks)


def check_providers(
    config: GatewayConfig,
    timeout: float = 15.0,
) -> list[CheckResult]:
    """Synchronously run the connectivity checks."""
    return asyncio.run(check_providers_async(config, timeout))


def print_results(results: list[CheckResult]) -> None:
    if not results:
        print("  (no providers configured)")
        return
    width = max(len(r.target) for r in results)
    for r in results:
        marker = "✓" if r.ok else "✗"
        print(f"  {marker} {r.target.ljust(width)}  {r.message}")
