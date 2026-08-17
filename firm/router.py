"""Model routing across many small quota pools.

A single-provider key is one big bucket. A FreeLLMAPI-style key is *not*: it is
dozens of independent per-model budgets that happen to sum to a big number.
"1.1B remaining" is the sum of ~33 separate pools, the largest of which is
99.5M. An agent pinned to one model does not get 1.1B tokens - it gets that
model's pool, and when the pool empties the agent stops working while the
dashboard still reports a billion tokens free.

Measured, not estimated: an unattended firm calls a model only for research
(hourly) and scout (12-hourly) - about **1.4M tokens/month**. Risk, execution,
backtest and cost are deterministic code and call nothing. So routing is not
about surviving a huge draw; it is about not stranding an agent on a 2M pool
when 30 other pools are full, and about degrading honestly when one empties.

So routing here is quota-aware failover, in preference order, per role:

* each model declares its monthly pool;
* usage is measured from the `costs` table (calendar month, as providers reset);
* a model at or near its pool is skipped;
* the next candidate for that role is used instead;
* when every candidate is exhausted the caller is told plainly, and the firm
  falls back to its deterministic engine rather than pretending.

Nothing here decides trades. It decides which model answers, and it fails
closed to heuristics.
"""

from __future__ import annotations

from typing import Any

# Monthly token pool per model, from the provider's dashboard. 0 means
# "unmetered or unknown" - treated as always available but never preferred.
POOLS: dict[str, float] = {
    # large pools, in millions of tokens
    "mistral-large-3": 99.5,
    "magistral-medium": 99.5,
    "codestral": 99.5,
    "mistral-medium-3.5": 99.5,
    "devstral": 99.5,
    "mistral-small-4": 99.5,
    "ministral-3-8b": 99.5,
    # mid
    "llama-3.3-70b-fp8-fast": 44.8,
    "gpt-oss-120b-cf": 44.8,
    "glm-4.7-flash-cf": 44.8,
    "llama-4-scout-cf": 44.8,
    "qwen3-30b-a3b-fp8-cf": 44.8,
    "glm-4.7-cerebras": 29.9,
    "gemma-4-31b-it": 29.9,
    "gemma-4-26b-it": 29.9,
    "gemma-4-26b-a4b-it-cf": 19.9,
    # small
    "granite-4.0-h-micro-cf": 10.0,
    "nemotron-3-120b-cf": 10.0,
    "gpt-oss-120b-groq": 6.0,
    "gpt-oss-20b-groq": 6.0,
    "compound-groq": 6.0,
    "compound-mini-groq": 6.0,
    "gpt-oss-safeguard-20b-groq": 6.0,
    "deepseek-r1-distill-qwen-32b-cf": 5.0,
    # tiny - fine for cheap, infrequent roles only
    "gemini-2.5-flash": 3.0,
    "gemini-2.5-flash-lite": 3.0,
    "gemini-3.5-flash": 3.0,
    "nemotron-super-120b-kilo": 3.0,
    "codestral-llm7": 2.0,
    # unmetered / stealth
    "deepseek-v4-flash-free": 0.0,
    "big-pickle": 0.0,
    "mimo-v2.5-free": 0.0,
    "stepfun-step-3.7-flash": 0.0,
}

# Preference order per role. Hungry, high-frequency roles get the big pools;
# roles that need reasoning quality get the strongest models available.
ROUTES: dict[str, list[str]] = {
    # 5-minute cadence, ~48M/month: must sit on the largest pools
    "execution": ["ministral-3-8b", "mistral-small-4", "mistral-medium-3.5",
                  "llama-3.3-70b-fp8-fast", "gpt-oss-120b-cf"],
    "risk":      ["mistral-medium-3.5", "mistral-large-3", "magistral-medium",
                  "llama-3.3-70b-fp8-fast", "qwen3-30b-a3b-fp8-cf"],
    # reasoning-heavy, low frequency: prefer capability
    "ceo":       ["mistral-large-3", "magistral-medium", "glm-4.7-cerebras",
                  "llama-3.3-70b-fp8-fast", "gpt-oss-120b-cf"],
    "research":  ["magistral-medium", "mistral-large-3", "glm-4.7-cerebras",
                  "llama-4-scout-cf", "gpt-oss-120b-cf"],
    "scout":     ["glm-4.7-flash-cf", "llama-4-scout-cf", "qwen3-30b-a3b-fp8-cf",
                  "gemma-4-31b-it", "mistral-small-4"],
    # code/spec shaped work
    "backtest":  ["devstral", "codestral", "qwen3-30b-a3b-fp8-cf",
                  "deepseek-r1-distill-qwen-32b-cf", "gpt-oss-120b-cf"],
    "cost_optimizer": ["gemma-4-26b-it", "granite-4.0-h-micro-cf",
                       "gpt-oss-20b-groq", "compound-mini-groq"],
}

DEFAULT_ROUTE = ["mistral-small-4", "llama-3.3-70b-fp8-fast", "gpt-oss-120b-cf",
                 "gemma-4-26b-it"]

# Stop using a pool at this fraction, leaving headroom for the month's tail.
SAFETY = 0.95

# Model names that mean "let the provider route". When an agent is configured
# with one of these, the gateway is doing the job this module does - picking
# the best model and failing over when one is rate-limited. Two routers
# fighting is strictly worse than either alone: ours would pin a single model
# and silently discard the gateway's live reliability/latency scoring, while
# still being blind to per-minute rate limits that only the provider can see.
# So we stand down and pass the name straight through.
PASSTHROUGH = {"auto", "fusion", "default", "best", "router", "smart"}


def is_passthrough(model: str) -> bool:
    return str(model or "").strip().lower() in PASSTHROUGH


def pool_for(model: str) -> float:
    """Monthly pool in tokens (not millions). 0 = unmetered/unknown."""
    return POOLS.get(model, 0.0) * 1_000_000


def usage_by_model(memory: Any) -> dict[str, int]:
    """Tokens consumed per model in the current calendar month."""
    import calendar
    import time as _t
    now = _t.gmtime()
    start = calendar.timegm((now.tm_year, now.tm_mon, 1, 0, 0, 0, 0, 0, 0))
    rows = memory.q(
        "SELECT model, COALESCE(SUM(input_tokens+output_tokens),0) t FROM costs"
        " WHERE created_at>=? GROUP BY model", (start,))
    return {r["model"]: int(r["t"]) for r in rows}


def remaining(memory: Any, model: str) -> float:
    """Tokens left in this model's pool. inf when unmetered."""
    cap = pool_for(model)
    if cap <= 0:
        return float("inf")
    return max(0.0, cap * SAFETY - usage_by_model(memory).get(model, 0))


def candidates(role: str) -> list[str]:
    return ROUTES.get(role) or DEFAULT_ROUTE


def pick(memory: Any, role: str, need: int = 4000,
         exclude: set[str] | None = None) -> tuple[str, str]:
    """Choose a model for `role`. Returns (model, reason).

    An empty model means every candidate is exhausted; the caller must fall
    back to deterministic behaviour rather than guess.
    """
    exclude = exclude or set()
    used = usage_by_model(memory)
    tried = []
    for m in candidates(role):
        if m in exclude:
            continue
        cap = pool_for(m)
        if cap <= 0:
            return m, f"{m} (unmetered)"
        left = cap * SAFETY - used.get(m, 0)
        tried.append((m, left))
        if left >= need:
            return m, (f"{m} ({left/1e6:.1f}M of {cap/1e6:.1f}M left)")
    if tried:
        worst = ", ".join(f"{m} {max(l,0)/1e6:.1f}M" for m, l in tried)
        return "", f"all {role} models exhausted this month ({worst})"
    return "", f"no models configured for {role}"


def status(memory: Any) -> dict:
    """Per-pool report for the dashboard and preflight."""
    used = usage_by_model(memory)
    rows = []
    for m, mm in sorted(POOLS.items(), key=lambda kv: -kv[1]):
        cap = mm * 1_000_000
        u = used.get(m, 0)
        rows.append({
            "model": m,
            "pool_tokens": cap,
            "used_tokens": u,
            "remaining_tokens": (float("inf") if cap <= 0
                                 else max(0.0, cap * SAFETY - u)),
            "pct_used": (0.0 if cap <= 0 else round(u / cap * 100, 2)),
            "metered": cap > 0,
        })
    total_cap = sum(v for v in POOLS.values()) * 1_000_000
    return {
        "models": rows,
        "total_pool_tokens": total_cap,
        "total_used_tokens": sum(used.values()),
        "roles": {r: candidates(r) for r in ROUTES},
        "safety_fraction": SAFETY,
        "note": ("Each model has its own monthly pool; the headline total is "
                 "the sum of independent pools, not one shared budget."),
    }
