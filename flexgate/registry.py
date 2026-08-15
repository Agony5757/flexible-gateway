"""Registry of known upstream LLM services.

`KNOWN_BASE_URLS` maps a service name prefix to its Anthropic-compatible
base_url. `flexgate sync` uses it to auto-import providers that exist in
Infisical but not yet in the config: the Infisical folder name is matched
against these prefixes on a dash boundary, longest prefix first
(e.g. 'minimax-tmy' -> minimax's base_url).

To onboard a new service, add one entry here — no other code changes needed.
Providers already present in the config are merged on top of this table at
sync time, so their (possibly newer) base_urls take precedence.
"""

from __future__ import annotations

# prefix -> base_url
KNOWN_BASE_URLS: dict[str, str] = {
    "minimax": "https://api.minimaxi.com/anthropic",
    "zai": "https://api.z.ai/api/anthropic",
    "ustc": "https://api.llm.ustc.edu.cn",
    "kimi": "https://api.kimi.com/coding",
}
