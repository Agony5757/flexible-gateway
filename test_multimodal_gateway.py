#!/usr/bin/env python3
"""Test multimodal degradation through Flexgate."""
import asyncio
import base64
import json
from pathlib import Path

import httpx

IMAGE_PATH = Path(__file__).parent / "test.png"
FLEXGATE_URL = "http://127.0.0.1:8765/v1/messages"


async def test_case(client: httpx.AsyncClient, model: str, label: str) -> None:
    image_b64 = base64.b64encode(IMAGE_PATH.read_bytes()).decode()
    body = {
        "model": model,
        "max_tokens": 128,
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
                        "text": "What is in this image?",
                    },
                ],
            }
        ],
    }

    print(f"\n{'='*60}")
    print(f"Test: {label} (model={model})")
    try:
        resp = await client.post(FLEXGATE_URL, json=body, timeout=120)
        print(f"  Status: {resp.status_code}")
        data = resp.json()
        if resp.status_code == 200:
            content = data.get("content", [])
            text_blocks = [b["text"] for b in content if isinstance(b, dict) and b.get("type") == "text"]
            text = text_blocks[0] if text_blocks else "(no text block)"
            has_note = "multimodal is not supported" in text
            print(f"  Has degradation note: {has_note}")
            print(f"  Reply: {text[:300]}")
        else:
            print(f"  Error: {json.dumps(data, indent=2)[:400]}")
    except Exception as e:
        print(f"  ERROR: {type(e).__name__}: {e}")


async def main():
    async with httpx.AsyncClient() as client:
        # Should strip images and add note — route → xiaomi, actual model mimo-v2.5-pro
        await test_case(client, "claude-sonnet-4-6", "xiaomi / mimo-v2.5-pro (should degrade)")
        # Should pass images through — route → minimax-tmy, actual model MiniMax-M3
        await test_case(client, "MiniMax-M3", "minimax-tmy / MiniMax-M3 (should support images)")

    print(f"\n{'='*60}")
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
