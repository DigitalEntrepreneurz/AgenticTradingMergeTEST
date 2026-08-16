"""LLM client with cost accounting, daily budget caps and graceful fallback.

Two wire protocols are supported so the firm is not locked to one vendor:

  * ``anthropic``  - api.anthropic.com /v1/messages
  * ``openai``     - any OpenAI-compatible /v1/chat/completions endpoint.
                     Covers OpenRouter, Groq, Together, DeepSeek, LM Studio,
                     Ollama and most "free LLM API" gateways.

If no API key is present the firm still runs end to end - every agent has a
deterministic heuristic path. The LLM adds judgement and narrative, never the
ability to bypass risk limits.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any

import httpx

API_URL = "https://api.anthropic.com/v1/messages"

# Known provider presets: (wire protocol, base url, env var holding the key)
PROVIDERS: dict[str, tuple[str, str, str]] = {
    "anthropic":  ("anthropic", "https://api.anthropic.com/v1/messages",
                   "ANTHROPIC_API_KEY"),
    "openrouter": ("openai", "https://openrouter.ai/api/v1/chat/completions",
                   "OPENROUTER_API_KEY"),
    "groq":       ("openai", "https://api.groq.com/openai/v1/chat/completions",
                   "GROQ_API_KEY"),
    "openai":     ("openai", "https://api.openai.com/v1/chat/completions",
                   "OPENAI_API_KEY"),
    "together":   ("openai", "https://api.together.xyz/v1/chat/completions",
                   "TOGETHER_API_KEY"),
    "deepseek":   ("openai", "https://api.deepseek.com/v1/chat/completions",
                   "DEEPSEEK_API_KEY"),
    "custom":     ("openai", "", "LLM_API_KEY"),
}

# USD per 1M tokens (input, output)
PRICES = {
    "claude-opus-4-5":   (5.00, 25.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-haiku-4-5":  (1.00,  5.00),
}


def redact(text: str, *secrets: str) -> str:
    """Strip credentials out of anything that might be logged or displayed.

    Provider errors are written to the event log and rendered on the dashboard.
    Some gateways echo the request - including the Authorization header - back
    in the error body, so an upstream 401 can put the key in plain sight. Never
    let a secret reach a log sink.
    """
    out = str(text or "")
    for sec in secrets:
        sec = (sec or "").strip()
        if len(sec) >= 8:
            out = out.replace(sec, f"{sec[:4]}...{sec[-2:]}")
    # belt and braces: catch key-shaped tokens we were never handed
    out = re.sub(r"\b(sk-[A-Za-z0-9_-]{8,}|Bearer\s+[A-Za-z0-9._-]{12,})",
                 "[REDACTED]", out)
    return out


def price_for(model: str) -> tuple[float, float]:
    # A ":free" suffix (OpenRouter) or a local endpoint costs nothing.
    if model.endswith(":free") or model.startswith("local/"):
        return (0.0, 0.0)
    for k, v in PRICES.items():
        if model.startswith(k) or k.startswith(model):
            return v
    return (3.00, 15.00)


@dataclass
class LLMReply:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    usd: float = 0.0
    used_llm: bool = True
    error: str = ""

    def json(self, default: Any = None) -> Any:
        """Best-effort JSON extraction from the reply."""
        t = self.text.strip()
        m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
        if m:
            t = m.group(1).strip()
        try:
            return json.loads(t)
        except json.JSONDecodeError:
            pass
        for opener, closer in (("{", "}"), ("[", "]")):
            s, e = t.find(opener), t.rfind(closer)
            if s >= 0 and e > s:
                try:
                    return json.loads(t[s:e + 1])
                except json.JSONDecodeError:
                    continue
        return default


class LLM:
    def __init__(self, api_key: str = "", memory=None, max_daily_usd: float = 10.0,
                 enabled: bool = True, provider: str = "anthropic",
                 base_url: str = "", model_prefix: str = ""):
        self.provider = (provider or "anthropic").strip().lower()
        proto, default_url, env_var = PROVIDERS.get(
            self.provider, PROVIDERS["anthropic"])
        self.protocol = proto
        self.base_url = (base_url or default_url).strip()
        # explicit key wins, then the provider's own env var, then a generic one
        self.api_key = (api_key or os.environ.get(env_var, "")
                        or os.environ.get("LLM_API_KEY", "")).strip()
        self.model_prefix = model_prefix.strip()
        self.memory = memory
        self.max_daily_usd = max_daily_usd
        # A local endpoint (ollama/LM Studio) legitimately needs no key.
        local = any(h in self.base_url for h in ("localhost", "127.0.0.1"))
        self.enabled = (enabled and bool(self.base_url)
                        and (bool(self.api_key) or local))

    def resolve_model(self, model: str) -> str:
        """Map a firm-level model name onto the configured provider.

        Agents ask for e.g. 'claude-sonnet-4-5'. On OpenRouter that must become
        'anthropic/claude-sonnet-4-5'; on Groq it should be an override from
        config. `llm.model_prefix` handles the common case, and
        `agents.<name>.model` can always name a provider model directly.
        """
        if not model:
            return model
        if self.model_prefix and "/" not in model:
            return f"{self.model_prefix.rstrip('/')}/{model}"
        return model

    @property
    def available(self) -> bool:
        if not self.enabled:
            return False
        if self.memory and self.memory.cost_today() >= self.max_daily_usd:
            return False
        return True

    def ask(self, agent: str, model: str, system: str, prompt: str,
            max_tokens: int = 1200, temperature: float = 0.3) -> LLMReply:
        if not self.available:
            reason = "no API key" if not self.enabled else "daily LLM budget reached"
            return LLMReply(text="", model=model, used_llm=False, error=reason)
        model = self.resolve_model(model)
        try:
            if self.protocol == "anthropic":
                headers = {"x-api-key": self.api_key,
                           "anthropic-version": "2023-06-01",
                           "content-type": "application/json"}
                payload = {"model": model, "max_tokens": max_tokens,
                           "temperature": temperature, "system": system,
                           "messages": [{"role": "user", "content": prompt}]}
            else:
                headers = {"Authorization": f"Bearer {self.api_key}",
                           "content-type": "application/json"}
                # OpenRouter asks callers to identify themselves
                if "openrouter" in self.base_url:
                    headers["HTTP-Referer"] = "https://github.com/agentic-trading-firm"
                    headers["X-Title"] = "Agentic Trading Firm"
                payload = {"model": model, "max_tokens": max_tokens,
                           "temperature": temperature,
                           "messages": [{"role": "system", "content": system},
                                        {"role": "user", "content": prompt}]}

            r = httpx.post(self.base_url, headers=headers, json=payload, timeout=120.0)
            if r.status_code >= 400:
                return LLMReply(text="", model=model, used_llm=False,
                                error=redact(f"HTTP {r.status_code}: {r.text[:200]}", self.api_key))
            data = r.json()

            if self.protocol == "anthropic":
                text = "".join(b.get("text", "") for b in data.get("content", [])
                               if b.get("type") == "text")
                usage = data.get("usage", {}) or {}
                itok = int(usage.get("input_tokens", 0))
                otok = int(usage.get("output_tokens", 0))
            else:
                choices = data.get("choices") or []
                if not choices:
                    return LLMReply(text="", model=model, used_llm=False,
                                    error=redact(f"no choices in reply: {str(data)[:200]}", self.api_key))
                text = (choices[0].get("message") or {}).get("content") or ""
                usage = data.get("usage", {}) or {}
                itok = int(usage.get("prompt_tokens", 0))
                otok = int(usage.get("completion_tokens", 0))

            pin, pout = price_for(model)
            usd = itok / 1e6 * pin + otok / 1e6 * pout
            if self.memory:
                self.memory.add_cost(agent, model, itok, otok, usd)
            return LLMReply(text=text, model=model, input_tokens=itok,
                            output_tokens=otok, usd=usd)
        except Exception as e:
            return LLMReply(text="", model=model, used_llm=False,
                            error=redact(str(e)[:200], self.api_key))
