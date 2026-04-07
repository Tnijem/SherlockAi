"""
Sherlock Cloud LLM Client — privacy-gated access to Anthropic Claude and OpenAI APIs.

All cloud requests must pass through privacy_gateway first.
This module handles API communication, streaming, and token tracking.
"""

import json
import time
import logging
from typing import AsyncIterator, Optional

import httpx

log = logging.getLogger("sherlock.cloud")


# ══════════════════════════════════════════════════════════════════════════════
# Configuration helpers
# ══════════════════════════════════════════════════════════════════════════════

def cloud_available() -> bool:
    """Check if cloud LLM is configured and enabled."""
    from config import CLOUD_ENABLED, CLOUD_API_KEY
    return CLOUD_ENABLED and bool(CLOUD_API_KEY)


def get_cloud_config() -> dict:
    """Return current cloud configuration (safe to expose — no API key)."""
    from config import (
        CLOUD_ENABLED, CLOUD_PROVIDER, CLOUD_MODEL,
        CLOUD_MODE, SENSITIVITY_THRESHOLD, CLOUD_API_KEY,
    )
    return {
        "enabled": CLOUD_ENABLED,
        "provider": CLOUD_PROVIDER,
        "model": CLOUD_MODEL,
        "mode": CLOUD_MODE,
        "sensitivity_threshold": SENSITIVITY_THRESHOLD,
        "api_key_set": bool(CLOUD_API_KEY),
    }


# ══════════════════════════════════════════════════════════════════════════════
# Token cost estimation
# ══════════════════════════════════════════════════════════════════════════════

# Prices per million tokens (as of 2026)
_PRICING = {
    "anthropic": {
        "claude-sonnet-4-20250514": {"input": 3.0, "output": 15.0},
        "claude-opus-4-20250514": {"input": 15.0, "output": 75.0},
        "claude-3-5-haiku-20241022": {"input": 1.0, "output": 5.0},
    },
    "openai": {
        "gpt-4o": {"input": 2.5, "output": 10.0},
        "gpt-4o-mini": {"input": 0.15, "output": 0.6},
        "gpt-4-turbo": {"input": 10.0, "output": 30.0},
    },
}


def estimate_cost(
    provider: str, model: str,
    input_tokens: int, output_tokens: int,
) -> float:
    """Estimate cost in USD for a cloud API call."""
    prices = _PRICING.get(provider, {}).get(model)
    if not prices:
        # Unknown model — rough estimate
        prices = {"input": 5.0, "output": 15.0}
    return (
        (input_tokens * prices["input"] / 1_000_000) +
        (output_tokens * prices["output"] / 1_000_000)
    )


# ══════════════════════════════════════════════════════════════════════════════
# Anthropic Claude Streaming
# ══════════════════════════════════════════════════════════════════════════════

async def _stream_anthropic(
    system_prompt: str,
    user_prompt: str,
    model: str,
    api_key: str,
    max_tokens: int = 4096,
) -> AsyncIterator[dict]:
    """Stream from Anthropic Messages API."""
    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "stream": True,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
    }

    input_tokens = 0
    output_tokens = 0

    async with httpx.AsyncClient(timeout=180.0) as client:
        async with client.stream("POST", url, headers=headers, json=payload) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                log.error("anthropic_error: %d %s", resp.status_code, body.decode()[:500])
                yield {"token": f"[Cloud error: {resp.status_code}]", "done": True, "usage": {}}
                return

            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    break

                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue

                event_type = event.get("type", "")

                if event_type == "content_block_delta":
                    delta = event.get("delta", {})
                    text = delta.get("text", "")
                    if text:
                        yield {"token": text, "done": False}

                elif event_type == "message_delta":
                    usage = event.get("usage", {})
                    output_tokens = usage.get("output_tokens", 0)

                elif event_type == "message_start":
                    usage = event.get("message", {}).get("usage", {})
                    input_tokens = usage.get("input_tokens", 0)

    yield {
        "token": "",
        "done": True,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": estimate_cost("anthropic", model, input_tokens, output_tokens),
        },
    }


# ══════════════════════════════════════════════════════════════════════════════
# OpenAI Streaming
# ══════════════════════════════════════════════════════════════════════════════

async def _stream_openai(
    system_prompt: str,
    user_prompt: str,
    model: str,
    api_key: str,
    max_tokens: int = 4096,
) -> AsyncIterator[dict]:
    """Stream from OpenAI Chat Completions API."""
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }

    input_tokens = 0
    output_tokens = 0

    async with httpx.AsyncClient(timeout=180.0) as client:
        async with client.stream("POST", url, headers=headers, json=payload) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                log.error("openai_error: %d %s", resp.status_code, body.decode()[:500])
                yield {"token": f"[Cloud error: {resp.status_code}]", "done": True, "usage": {}}
                return

            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    break

                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue

                choices = event.get("choices", [])
                if choices:
                    delta = choices[0].get("delta", {})
                    text = delta.get("content", "")
                    if text:
                        yield {"token": text, "done": False}

                usage = event.get("usage")
                if usage:
                    input_tokens = usage.get("prompt_tokens", 0)
                    output_tokens = usage.get("completion_tokens", 0)

    yield {
        "token": "",
        "done": True,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": estimate_cost("openai", model, input_tokens, output_tokens),
        },
    }


# ══════════════════════════════════════════════════════════════════════════════
# Unified Streaming Interface
# ══════════════════════════════════════════════════════════════════════════════

async def stream_cloud_response(
    system_prompt: str,
    user_prompt: str,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    max_tokens: int = 4096,
) -> AsyncIterator[dict]:
    """Stream a response from the configured cloud LLM.

    Yields dicts:
        {"token": "...", "done": False}       — content token
        {"token": "", "done": True, "usage": {...}}  — final stats

    Args:
        system_prompt: System/instruction prompt
        user_prompt: User message (should already be scrubbed by privacy_gateway)
        provider: "anthropic" or "openai" (defaults to config)
        model: Model name (defaults to config)
        api_key: API key (defaults to config)
        max_tokens: Max response tokens
    """
    from config import CLOUD_PROVIDER, CLOUD_MODEL, CLOUD_API_KEY

    provider = provider or CLOUD_PROVIDER
    model = model or CLOUD_MODEL
    api_key = api_key or CLOUD_API_KEY

    if not api_key:
        yield {"token": "[Cloud LLM not configured — no API key]", "done": True, "usage": {}}
        return

    log.info("cloud_request: provider=%s model=%s", provider, model)
    t0 = time.perf_counter()

    try:
        if provider == "anthropic":
            async for chunk in _stream_anthropic(system_prompt, user_prompt, model, api_key, max_tokens):
                yield chunk
        elif provider == "openai":
            async for chunk in _stream_openai(system_prompt, user_prompt, model, api_key, max_tokens):
                yield chunk
        else:
            yield {"token": f"[Unknown cloud provider: {provider}]", "done": True, "usage": {}}
            return
    except Exception as e:
        log.error("cloud_stream_error: %s", e)
        yield {"token": f"[Cloud error: {e}]", "done": True, "usage": {}}
        return

    elapsed = time.perf_counter() - t0
    log.info("cloud_complete: %.1fs", elapsed)
