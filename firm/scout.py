"""Auto-scan suite: discover trending forex strategies, test them, rank them.

The scout maintains a catalogue of strategy archetypes that circulate in retail
forex education (YouTube, forums, prop-firm blogs). Each one is expressed as a
declarative composite spec - the same data format the video ingester produces -
so the existing backtest / optimize / score / export pipeline runs unchanged.

Discovery has two tiers, and the cheap one always works:

  1. CATALOGUE  - deterministic, $0, no network. Curated archetypes with the
                  parameters their proponents actually quote.
  2. LLM        - optional. Asks the model to name currently-discussed setups
                  and encode them as specs. Validated against RULE_TYPES and
                  discarded if malformed, so a bad reply can never break a scan.

Nothing here is an endorsement. A catalogue entry is a *hypothesis*; the whole
point of the suite is that most of them fail validation and get marked IGNORE.
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable

from .strategies.composite import RULE_TYPES

# ----------------------------------------------------------------------
# Curated archetypes. `popularity` is a rough proxy for how often the setup is
# taught, used only to order the scan queue - never as evidence of edge.
# ----------------------------------------------------------------------
CATALOGUE: list[dict[str, Any]] = [
    {
        "name": "3 EMA Ribbon Pullback",
        "tags": ["trend", "ema", "pullback"],
        "popularity": 96,
        "summary": "Classic 9/21/50 EMA stack; enter on a pullback to the mid "
                   "EMA while momentum stays constructive.",
        "entry": [
            {"type": "ema_stack", "fast": 9, "mid": 21, "slow": 50},
            {"type": "pullback", "to": "ema_mid", "mid": 21, "max_atr": 1.0},
            {"type": "rsi_zone", "period": 14, "min": 45, "max": 75},
        ],
        "filters": [{"type": "session", "from": 7, "to": 17}],
        "exit": {"stop": "atr", "atr_mult": 1.5, "rr": 2.0},
        "agreement": 0.6,
    },
    {
        "name": "London Breakout",
        "tags": ["breakout", "session"],
        "popularity": 92,
        "summary": "Break of the recent range during the London session, "
                   "filtered for adequate volatility.",
        "entry": [
            {"type": "breakout", "period": 20},
            {"type": "candle"},
        ],
        "filters": [
            {"type": "session", "from": 7, "to": 12},
            {"type": "atr_filter", "min_pct": 0.04, "max_pct": 1.5},
        ],
        "exit": {"stop": "atr", "atr_mult": 2.0, "rr": 2.5},
        "agreement": 0.5,
    },
    {
        "name": "MACD + 200 EMA Trend Filter",
        "tags": ["momentum", "macd", "trend"],
        "popularity": 88,
        "summary": "Only take MACD crosses in the direction of the 200 EMA bias.",
        "entry": [
            {"type": "macd_cross"},
            {"type": "ema_stack", "fast": 21, "mid": 50, "slow": 200},
        ],
        "filters": [{"type": "atr_filter", "min_pct": 0.03, "max_pct": 2.0}],
        "exit": {"stop": "atr", "atr_mult": 2.0, "rr": 2.0},
        "agreement": 1.0,
    },
    {
        "name": "Bollinger Band Mean Reversion",
        "tags": ["reversion", "bollinger", "range"],
        "popularity": 85,
        "summary": "Fade closes outside the bands when RSI confirms an extreme.",
        "entry": [
            {"type": "bb_touch", "mode": "reversion"},
            {"type": "rsi_extreme", "period": 14, "oversold": 30, "overbought": 70},
        ],
        "exit": {"stop": "atr", "atr_mult": 1.5, "rr": 1.5},
        "agreement": 1.0,
    },
    {
        "name": "RSI Divergence Reversal",
        "tags": ["reversion", "rsi"],
        "popularity": 80,
        "summary": "RSI at an extreme with a price-action trigger candle. "
                   "Proxy for the divergence setups taught on YouTube.",
        "entry": [
            {"type": "rsi_extreme", "period": 14, "oversold": 25, "overbought": 75},
            {"type": "candle"},
        ],
        "filters": [{"type": "session", "from": 6, "to": 20}],
        "exit": {"stop": "atr", "atr_mult": 1.8, "rr": 2.0},
        "agreement": 1.0,
    },
    {
        "name": "Golden Cross Swing",
        "tags": ["trend", "sma"],
        "popularity": 76,
        "summary": "The 50/200 SMA cross, the most quoted signal in retail "
                   "trading. Included precisely to test the folklore.",
        "entry": [{"type": "sma_cross", "fast": 50, "slow": 200}],
        "exit": {"stop": "atr", "atr_mult": 2.5, "rr": 3.0},
        "agreement": 0.5,
    },
    {
        "name": "Turtle Channel Breakout",
        "tags": ["breakout", "donchian", "trend"],
        "popularity": 74,
        "summary": "Donchian channel break in the direction of the longer EMA "
                   "trend - a modernised Turtle entry.",
        "entry": [
            {"type": "breakout", "period": 20},
            {"type": "ema_cross", "fast": 21, "slow": 50},
        ],
        "exit": {"stop": "atr", "atr_mult": 2.0, "rr": 3.0},
        "agreement": 0.5,
    },
    {
        "name": "Engulfing Pullback Continuation",
        "tags": ["price-action", "pullback"],
        "popularity": 71,
        "summary": "Trend pullback to the 21 EMA completed by an engulfing or "
                   "pin-bar trigger. No indicators beyond the MA.",
        "entry": [
            {"type": "ema_stack", "fast": 21, "mid": 50, "slow": 100},
            {"type": "pullback", "to_period": 21, "max_atr": 0.8},
            {"type": "candle"},
        ],
        "filters": [{"type": "session", "from": 7, "to": 18}],
        "exit": {"stop": "atr", "atr_mult": 1.5, "rr": 2.5},
        "agreement": 0.66,
    },
    {
        "name": "New York Reversal Fade",
        "tags": ["reversion", "session"],
        "popularity": 65,
        "summary": "Fade stretched moves into the New York session close.",
        "entry": [
            {"type": "bb_touch", "mode": "reversion"},
            {"type": "rsi_zone", "period": 14, "min": 30, "max": 70},
        ],
        "filters": [{"type": "session", "from": 13, "to": 21}],
        "exit": {"stop": "atr", "atr_mult": 1.6, "rr": 1.5},
        "agreement": 0.5,
    },
    {
        "name": "Volatility Squeeze Expansion",
        "tags": ["breakout", "volatility"],
        "popularity": 62,
        "summary": "Trade the expansion out of a low-volatility squeeze, "
                   "confirmed by a channel break.",
        "entry": [
            {"type": "breakout", "period": 15},
            {"type": "macd_cross"},
        ],
        "filters": [{"type": "atr_filter", "min_pct": 0.02, "max_pct": 0.9}],
        "exit": {"stop": "atr", "atr_mult": 2.0, "rr": 3.0},
        "agreement": 0.5,
    },
]

DISCOVER_SYSTEM = """You track what retail forex traders are actually being
taught right now on YouTube, prop-firm blogs and trading forums.

Encode each setup as a declarative spec using ONLY these rule types:
{types}

Reply with STRICT JSON, no prose:
{{"strategies":[{{"name":"","summary":"","tags":[],"popularity":0,
 "entry":[{{"type":""}}],"filters":[{{"type":""}}],
 "exit":{{"stop":"atr","atr_mult":2.0,"rr":2.0}},"agreement":0.6}}]}}

Rules:
- Only real, commonly-taught setups. Do not invent exotic strategies.
- 2-4 entry rules each. Filters are optional.
- popularity 0-100 = how widely the setup is taught, NOT how well it works.
- Never claim a strategy is profitable. The firm decides that by backtest."""


def valid_spec(spec: Any) -> bool:
    """A spec is testable only if it has recognised entry rules."""
    if not isinstance(spec, dict):
        return False
    entry = spec.get("entry")
    if not isinstance(entry, list) or not entry:
        return False
    if not all(isinstance(r, dict) and r.get("type") in RULE_TYPES for r in entry):
        return False
    filters = spec.get("filters", [])
    if filters and not all(
            isinstance(r, dict) and r.get("type") in RULE_TYPES for r in filters):
        return False
    return bool(str(spec.get("name", "")).strip())


def catalogue(tags: list[str] | None = None, limit: int = 0) -> list[dict]:
    """Curated archetypes, most-taught first, optionally filtered by tag."""
    out = sorted(CATALOGUE, key=lambda s: -s.get("popularity", 0))
    if tags:
        want = {t.lower() for t in tags}
        out = [s for s in out if want & {t.lower() for t in s.get("tags", [])}]
    out = [dict(s, source="catalogue") for s in out]
    return out[:limit] if limit else out


def discover_llm(llm, count: int = 6, exclude: list[str] | None = None) -> list[dict]:
    """Ask the model for currently-discussed setups. Never raises."""
    if not llm or not getattr(llm, "available", False):
        return []
    try:
        skip = ", ".join(exclude or []) or "none"
        reply = llm.ask(
            "scout", "claude-sonnet-4-5",
            DISCOVER_SYSTEM.format(types=", ".join(sorted(RULE_TYPES))),
            f"Name {count} forex strategies currently being taught that are NOT "
            f"in this list: {skip}. Encode each as a spec.", 2000, 0.6)
        data = reply.json()
        if not isinstance(data, dict):
            return []
        found = [dict(s, source="llm") for s in data.get("strategies", [])
                 if valid_spec(s)]
        return found[:count]
    except Exception:
        return []


def discover(llm=None, tags: list[str] | None = None, limit: int = 8,
             use_llm: bool = True) -> list[dict]:
    """Full discovery pass: catalogue first, LLM-found extras appended."""
    found = catalogue(tags, limit)
    if use_llm and len(found) < limit:
        extra = discover_llm(llm, limit - len(found),
                             exclude=[s["name"] for s in found])
        seen = {s["name"].lower() for s in found}
        found += [s for s in extra if s["name"].lower() not in seen]
    return found[:limit]


# ----------------------------------------------------------------------
# Scanning
# ----------------------------------------------------------------------
def scan(lab, specs: list[dict], symbols: list[str], timeframe: str = "H1",
         max_combos: int = 24, bars: int = 1600,
         progress: Callable[[dict], None] | None = None,
         memory=None) -> list[dict]:
    """Backtest+optimize every spec on every symbol, ranked by robustness.

    Returns plain dicts so the result can go straight to JSON. Failures are
    captured per-item: one broken spec must never abort the scan.
    """
    results: list[dict] = []
    total = len(specs) * len(symbols)
    done = 0
    for spec in specs:
        for sym in symbols:
            label = f"{spec.get('name', 'unnamed')} · {sym} {timeframe}"
            try:
                res = lab.optimize_spec(spec, sym, timeframe,
                                        max_combos=max_combos, bars_count=bars)
                d = res.dict()
                d.update(name=spec.get("name", "unnamed"),
                         summary=spec.get("summary", ""),
                         tags=spec.get("tags", []),
                         popularity=spec.get("popularity", 0),
                         origin=spec.get("source", "catalogue"),
                         spec=spec, composite=True)
                d["verdict"] = verdict_of(d)
            except Exception as e:
                d = {"name": spec.get("name", "unnamed"), "symbol": sym,
                     "timeframe": timeframe, "strategy": spec.get("name", "unnamed"),
                     "score": 0.0, "passed": False, "metrics": {},
                     "walk_forward": {}, "params": {}, "spec": spec,
                     "composite": True, "origin": spec.get("source", "catalogue"),
                     "tags": spec.get("tags", []), "verdict": "ERROR",
                     "reason": f"{type(e).__name__}: {e}"}
            results.append(d)
            done += 1
            if memory is not None:
                _remember(memory, d)
            if progress:
                progress({"done": done, "total": total, "current": label,
                          "result": d})
    results.sort(key=lambda r: r.get("score", 0), reverse=True)
    return results


def verdict_of(d: dict) -> str:
    """Three-way call so the board can triage at a glance."""
    if d.get("passed"):
        return "ADOPT"
    m = d.get("metrics") or {}
    # Positive expectancy but failed a gate = worth another look, not a reject.
    if m.get("trades", 0) >= 8 and float(m.get("expectancy_r", 0) or 0) > 0:
        return "WATCH"
    return "IGNORE"


def _remember(memory, d: dict) -> None:
    """Persist so the firm never rescans the same thing blindly."""
    try:
        key = f"{d['name']}:{d['symbol']}:{d['timeframe']}"
        memory.remember("scout", "scan", key,
                        f"{d['verdict']} score {d.get('score', 0)} "
                        f"{json.dumps(d.get('metrics', {}))[:400]}",
                        {"verdict": d["verdict"], "score": d.get("score", 0)})
        if d.get("verdict") in ("ADOPT", "WATCH"):
            memory.upsert_strategy(
                name=f"{d['name']}@{d['symbol']}",
                spec={"composite": True, "spec": d.get("spec", {}),
                      "params": d.get("params", {}), "symbol": d["symbol"],
                      "timeframe": d["timeframe"]},
                source="scout",
                status="proposed",
                score=float(d.get("score", 0) or 0),
                metrics=d.get("metrics", {}),
                notes=f"Auto-scan {d['verdict']}. {d.get('reason', '')}"[:500])
    except Exception:
        pass


def summarise(results: list[dict]) -> dict:
    """Headline numbers for the scan report."""
    adopt = [r for r in results if r.get("verdict") == "ADOPT"]
    watch = [r for r in results if r.get("verdict") == "WATCH"]
    return {
        "scanned": len(results),
        "adopt": len(adopt),
        "watch": len(watch),
        "ignore": sum(1 for r in results if r.get("verdict") == "IGNORE"),
        "errors": sum(1 for r in results if r.get("verdict") == "ERROR"),
        "best": results[0] if results else None,
        "generated_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
