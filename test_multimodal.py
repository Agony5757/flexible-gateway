#!/usr/bin/env python3
"""Directly test multimodal (image) support for each upstream provider, bypassing Flexgate."""

import asyncio
import base64
import sys
from pathlib import Path

import httpx

IMAGE_PATH = Path(__file__).parent / "test.png"

# (provider name in ~/.flexgate/config.yaml, model to test). API keys are read
# from the flexgate config at runtime — never hardcode keys here.
PROVIDERS = [
    {"provider": "xiaomi", "model": "mimo-v2.5-pro"},
    {"provider": "minimax", "model": "MiniMax-M3"},
    {"provider": "zai", "model": "glm-5.1"},
]


def _load_providers() -> list[dict]:
    import yaml

    config_path = Path.home() / ".flexgate" / "config.yaml"
    with open(config_path) as f:
        raw = yaml.safe_load(f)
    providers = []
    for spec in PROVIDERS:
        entry = (raw.get("providers") or {}).get(spec["provider"])
        if not entry:
            print(f"Skipping {spec['provider']}: not found in {config_path}")
            continue
        providers.append({
            "name": f"{spec['provider']} ({spec['model']})",
            "base_url": entry["base_url"],
            "api_key": entry["api_key"],
            "model": spec["model"],
        })
    if not providers:
        print(f"No testable providers found in {config_path}")
        sys.exit(1)
    return providers


async def test_provider(client: httpx.AsyncClient, provider: dict, image_b64: str) -> None:
    name = provider["name"]
    url = f"{provider['base_url']}/v1/messages"
    headers = {
        "x-api-key": provider["api_key"],
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {
        "model": provider["model"],
        "max_tokens": 256,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": image_b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": "Describe this image in one sentence.",
                    },
                ],
            }
        ],
    }

    print(f"\n{'='*60}")
    print(f"Testing: {name}")
    print(f"  URL:   {url}")
    print(f"  Model: {provider['model']}")

    try:
        resp = await client.post(url, json=headers | {"_body": body}, timeout=60)
        # Re-do: httpx.post takes json=, headers= separately
    except Exception:
        pass

    try:
        resp = await client.post(url, headers=headers, json=body, timeout=120)
        status = resp.status_code
        text = resp.text[:500]

        if status == 200:
            data = resp.json()
            content = data.get("content", [])
            reply = content[0]["text"] if content else "(empty)"
            print(f"  OK (200) — Model reply: {reply[:200]}")
        else:
            print(f"  FAIL ({status})")
            print(f"  Response: {text}")
    except httpx.TimeoutException:
        print(f"  TIMEOUT after 120s")
    except Exception as e:
        print(f"  ERROR: {type(e).__name__}: {e}")


async def main():
    if not IMAGE_PATH.exists():
        print(f"Image not found: {IMAGE_PATH}")
        sys.exit(1)

    image_b64 = base64.b64encode(IMAGE_PATH.read_bytes()).decode()
    print(f"Image: {IMAGE_PATH} ({IMAGE_PATH.stat().st_size} bytes, base64 len={len(image_b64)})")

    async with httpx.AsyncClient() as client:
        tasks = [test_provider(client, p, image_b64) for p in _load_providers()]
        await asyncio.gather(*tasks)

    print(f"\n{'='*60}")
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
