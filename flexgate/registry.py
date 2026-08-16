"""Registry of known upstream LLM services.

`KNOWN_BASE_URLS` maps a service name prefix to its Anthropic-compatible
base_url; `KNOWN_DEFAULT_MODELS` maps the same prefix to its latest model(s),
first entry being the default. `flexgate sync` uses them to auto-import
providers that exist in Infisical but not yet in the config: the Infisical
folder name is matched against these prefixes on a dash boundary, longest
prefix first (e.g. 'minimax-tmy' -> minimax's base_url and models). A
folder's own MODELS secret, when present, overrides the registry default.

To onboard a new service, add one entry to each table — no other code
changes needed. Providers already present in the config are merged on top of
these tables at sync time, so their (possibly newer) values take precedence.
"""

from __future__ import annotations

# prefix -> base_url
KNOWN_BASE_URLS: dict[str, str] = {
    "minimax": "https://api.minimaxi.com/anthropic",
    "zai": "https://api.z.ai/api/anthropic",
    "ustc": "https://api.llm.ustc.edu.cn",
    "kimi": "https://api.kimi.com/coding",
}

# prefix -> latest models (first entry = default/fallback model)
KNOWN_DEFAULT_MODELS: dict[str, list[str]] = {
    "minimax": ["MiniMax-M3"],
    "zai": ["glm-5.3", "glm-4.6v"],
    "ustc": ["deepseek-v4-pro"],
    "kimi": ["kimi-k3"],
}
