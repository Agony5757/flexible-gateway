#!/usr/bin/env python3
"""Directly test multimodal (image) support for each upstream provider, bypassing Flexgate."""

import asyncio
import base64
import sys
from pathlib import Path

import httpx

IMAGE_PATH = Path(__file__).parent / "test.png"

PROVIDERS = [
    {
        "name": "xiaomi (mimo-v2.5-pro)",
        "base_url": "https://token-plan-cn.xiaomimimo.com/anthropic",
        "api_key": "tp-c4mwrxdzo5i8ptd31llrnag70dmi1r27nwguuakcfigizghh",
        "model": "mimo-v2.5-pro",
    },
    {
        "name": "minimax (MiniMax-M3)",
        "base_url": "https://api.minimaxi.com/anthropic",
        "api_key": "sk-cp-O3ESr5K0P2U0wExk5pEKipPPshT7g46zOh4KwYtXnD99ol0OU4MLH-ujUZ97HGJDXrX35NL0QVfPQX9HogEQ-60gsrRbHI3LEYuCAQTcH1qL-d8TjdEeyGE",
        "model": "MiniMax-M3",
    },
    {
        "name": "zai (glm-5.1)",
        "base_url": "https://api.z.ai/api/anthropic",
        "api_key": "e37b1f9cb583455392ad83b5475d4efe.5BEcmDRG7YXOfNgN",
        "model": "glm-5.1",
    },
]


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
        tasks = [test_provider(client, p, image_b64) for p in PROVIDERS]
        await asyncio.gather(*tasks)

    print(f"\n{'='*60}")
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
