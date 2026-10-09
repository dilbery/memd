"""Chat client for an OpenAI-compatible /v1/chat/completions endpoint.

Used by background jobs (mem-summarize, mem-facts) that need a language model, by
save's optional conflict check (memd.conflicts, MEMD_CONFLICT_CHECK=llm, after
the commit under a hard deadline) and by ask (memd.ask, under
MEMD_ASK_DEADLINE_MS, with an extractive fallback); NEVER by recall or read. Off unless MEMD_LLM_URL is set. MEMD_LLM_MODEL names the
model and MEMD_LLM_TIMEOUT_S bounds one completion (Config.llm_*). Auth reuses
MEMD_MODEL_API_KEY / _FILE through config.model_headers, the same bearer the
embedding and rerank adapters send, because all three usually sit behind one
gateway.

Contract:
  enabled(cfg) -> bool
  chat(messages, *, cfg, max_tokens, temperature, response_format=None) -> str
    raises LLMError on disabled config, transport failure, non-2xx, or a reply
    without text. Callers decide what a failure means; nothing here retries.
"""
from __future__ import annotations

import json
import time

import httpx

from memd.config import Config, model_headers

_now = time.monotonic   # the deadline's clock (tests replace it)

_PATH = "/v1/chat/completions"


class LLMError(RuntimeError):
    """The chat backend is off, unreachable, or answered without usable text."""


def enabled(cfg: Config) -> bool:
    return bool(cfg.llm_url)


def completions_url(base: str) -> str:
    """The completions URL for MEMD_LLM_URL given as a bare base, a /v1 base, or in full."""
    base = base.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return base + "/chat/completions"
    return base + _PATH


def _timeout(seconds: float) -> httpx.Timeout:
    # Connect and pool are short: an absent backend should fail in seconds, while
    # the read phase may legitimately take most of the budget on a local model.
    return httpx.Timeout(seconds, connect=min(5.0, seconds), pool=min(5.0, seconds))


def chat(
    messages: list[dict],
    *,
    cfg: Config,
    max_tokens: int = 1024,
    temperature: float = 0.0,
    response_format: dict | None = None,
) -> str:
    """One chat completion; returns the first choice's message text."""
    if not enabled(cfg):
        raise LLMError("no chat model configured (set MEMD_LLM_URL)")
    if not cfg.llm_model:
        raise LLMError("MEMD_LLM_URL is set but MEMD_LLM_MODEL is not")
    payload: dict = {
        "model": cfg.llm_model,
        "messages": messages,
        "max_tokens": int(max_tokens),
        "temperature": float(temperature),
        "stream": False,
    }
    if response_format is not None:
        payload["response_format"] = response_format
    from memd.metrics import backend_call
    with backend_call("chat"):
        return _complete(payload, cfg)


def _complete(payload: dict, cfg: Config) -> str:
    url = completions_url(cfg.llm_url)
    # httpx's timeout bounds each read; the deadline bounds the whole call, so a
    # backend trickling bytes cannot hold a batch job past MEMD_LLM_TIMEOUT_S.
    deadline = _now() + cfg.llm_timeout_s
    try:
        with httpx.stream("POST", url, json=payload, headers=model_headers(cfg, url),
                          timeout=_timeout(cfg.llm_timeout_s)) as resp:
            raw = bytearray()
            for chunk in resp.iter_bytes():
                raw += chunk
                if _now() > deadline:
                    raise LLMError(f"chat backend exceeded the {cfg.llm_timeout_s:g}s deadline")
            if resp.status_code >= 400:
                raise LLMError(f"chat backend HTTP {resp.status_code}: "
                               f"{bytes(raw[:200]).decode('utf-8', 'replace')}")
        body = json.loads(bytes(raw))
    except httpx.HTTPError as e:
        raise LLMError(f"chat backend unreachable: {type(e).__name__}") from e
    except ValueError as e:
        raise LLMError("chat backend answered with invalid JSON") from e
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise LLMError(f"chat backend reply has no message: {str(body)[:200]}") from None
    if not isinstance(content, str) or not content.strip():
        raise LLMError("chat backend returned an empty message")
    return content
