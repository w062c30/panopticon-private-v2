"""
Phase 1: NVIDIA Chat Completions over urllib (single swap point for Phase 2 local LLM).

Do not import panopticon_py.cognitive from here (avoid cycles). cognitive re-exports DEFAULT_MODEL.
"""

from __future__ import annotations

import json
import logging
import os
import socket
from typing import Any

from urllib import error as urlerror
from urllib import request

logger = logging.getLogger(__name__)

NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
DEFAULT_MODEL = "minimaxai/minimax-m2.7"


def post_nvidia_chat_completion(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    temperature: float = 0.1,
    max_tokens: int = 256,
    top_p: float | None = None,
    stream: bool = False,
    timeout_sec: float = 60.0,
    api_key: str | None = None,
) -> str:
    """
    POST chat/completions; return assistant message content (raw string).
    Raises on HTTP/parse errors — callers that need soft-fail should catch.
    """
    key = api_key or os.getenv("NVIDIA_API_KEY")
    if not key:
        raise RuntimeError("NVIDIA_API_KEY missing")

    mid = model or os.getenv("NVIDIA_SEMANTIC_MODEL") or DEFAULT_MODEL
    payload: dict[str, Any] = {
        "model": mid,
        "messages": messages,
        "temperature": float(temperature),
        "max_tokens": int(max_tokens),
        "stream": bool(stream),
    }
    if top_p is not None:
        payload["top_p"] = float(top_p)

    req = request.Request(
        f"{NVIDIA_BASE_URL}/chat/completions",
        method="POST",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
    )
    with request.urlopen(req, timeout=timeout_sec) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return str(body["choices"][0]["message"]["content"])


def post_nvidia_chat_completion_safe(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    temperature: float = 0.1,
    max_tokens: int = 256,
    top_p: float | None = None,
    stream: bool = False,
    timeout_sec: float = 60.0,
    api_key: str | None = None,
) -> str | None:
    """Same as post_nvidia_chat_completion but returns None on failure (no key, timeout, HTTP)."""
    try:
        return post_nvidia_chat_completion(
            messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=top_p,
            stream=stream,
            timeout_sec=timeout_sec,
            api_key=api_key,
        )
    except (
        RuntimeError,
        urlerror.URLError,
        urlerror.HTTPError,
        TimeoutError,
        socket.timeout,
        KeyError,
        IndexError,
        json.JSONDecodeError,
        OSError,
    ) as e:
        logger.warning("post_nvidia_chat_completion_safe failed: %s", e)
        return None
