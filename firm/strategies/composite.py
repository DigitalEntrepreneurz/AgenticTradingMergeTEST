"""Composable rule-based strategies.

A strategy extracted from a video (or written by an agent) is expressed as a
declarative spec rather than code, so it can be backtested, optimized, scored
and exported to MQL without anyone writing Python.

    {
      "name": "3 EMA Pullback",
      "entry": [
        {"type": "ema_stack", "fast": 9, "mid": 21, "slow": 50},
        {"type": "rsi_zone",  "period": 14, "min": 40, "max": 70},
        {"type": "pullback",  "to": "ema_mid", "max_atr": 1.0}
      ],
      "exit":  {"stop": "atr", "atr_mult": 1.5, "rr": 2.0},
      "filters": [{"type": "session", "from": 7, "to": 20}]
    }

Every rule is optional; unknown rules are ignored so a partial extraction still
produces something testable.
"""
from __future__ import annotations

import time
from typing import Any

from ..brokers.base import Bar
from ..indicators import (atr, bollinger, closes, donchian, ema, macd, rsi, sma)
from .library import Signal

# ---------------------------------------------------------------- rule set
RULE_TYPES = {
    "ema_stack":   "Fast/mid/slow EMAs aligned in the trade direction",
    "ema_cross":   "Fast EMA crosses the slow EMA",
    "sma_cross":   "Fast SMA crosses the slow SMA",
    "rsi_zone":    "RSI inside a band (momentum confirmation)",
    "rsi_extreme": "RSI beyond an overbought/oversold level (reversal)",
    "macd_cross":  "MACD line crosses its signal line",
    "bb_touch":    "Price closes outside a Bollinger band",
    "breakout":    "Break of the N-bar high/low",
    "pullback":    "Price has retraced close to a moving average",
    "candle":      "Engulfing / pin-bar style price action trigger",
    "session":     "Only trade inside these UTC hours",
    "atr_filter":  "Only trade when volatility is within a range",
}


def _ind(bars: list[Bar], spec: dict) -> dict[str, Any]:
    """Compute once, everything the rules might need.

    Indicators are keyed by their actual period so that a rule asking for
    RSI-21 or a 15-bar breakout gets exactly that. (Before, every RSI rule
    silently shared one spec-level RSI-14 and every breakout shared one
    Donchian-20, so rule-level periods were ignored and the backtest did not
    test the strategy that was written.) Spec-level defaults are still
    computed under the bare keys for backwards compatibility.
    """
    c = closes(bars)
    p = spec.get("params", {})
    out: dict[str, Any] = {"close": c, "bars": bars}
    rules = [r for r in (list(spec.get("entry") or []) + list(spec.get("filters") or []))
             if isinstance(r, dict)]

    # moving averages
    periods = set()
    for r in rules:
        for k in ("fast", "mid", "slow", "period", "to_period"):
            if isinstance(r.get(k), (int, float)) and r.get("type") not in (
                    "rsi_zone", "rsi_extreme", "bb_touch", "breakout", "macd_cross"):
                periods.add(int(r[k]))
        # ema_cross/sma_cross use fast+slow, always MAs
        if r.get("type") in ("ema_cross", "sma_cross", "ema_stack", "pullback"):
            for k in ("fast", "mid", "slow", "period", "to_period"):
                if isinstance(r.get(k), (int, float)):
                    periods.add(int(r[k]))
    for n in periods | {9, 21, 50, 200}:
        if 1 < n < len(c):
            out[f"ema{n}"] = ema(c, n)
            out[f"sma{n}"] = sma(c, n)

    out["atr"] = atr(bars, int(p.get("atr_n", 14)))

    # per-rule RSI periods
    out["rsi"] = rsi(c, int(p.get("rsi_n", 14)))
    for r in rules:
        if r.get("type") in ("rsi_zone", "rsi_extreme"):
            n = int(r.get("period", p.get("rsi_n", 14)))
            if 1 < n < len(c):
                out.setdefault(f"rsi{n}", rsi(c, n))

    # per-rule MACD triplets
    out["macd"] = macd(c, int(p.get("macd_fast", 12)), int(p.get("macd_slow", 26)),
                       int(p.get("macd_signal", 9)))
    for r in rules:
        if r.get("type") == "macd_cross":
            f = int(r.get("fast", p.get("macd_fast", 12)))
            s = int(r.get("slow", p.get("macd_slow", 26)))
            g = int(r.get("signal", p.get("macd_signal", 9)))
            out.setdefault(f"macd{f}_{s}_{g}", macd(c, f, s, g))

    # per-rule Bollinger settings
    out["bb"] = bollinger(c, int(p.get("bb_n", 20)), float(p.get("bb_mult", 2.0)))
    for r in rules:
        if r.get("type") == "bb_touch":
            n = int(r.get("period", p.get("bb_n", 20)))
            m = float(r.get("mult", p.get("bb_mult", 2.0)))
            if 1 < n < len(c):
                out.setdefault(f"bb{n}_{m}", bollinger(c, n, m))

    # per-rule breakout channels
    out["dc"] = donchian(bars, int(p.get("channel", 20)))
    for r in rules:
        if r.get("type") == "breakout":
            # ingest emits "channel", hand-written specs use "period" - accept both
            n = int(r.get("period", r.get("channel", p.get("channel", 20))))
            if 1 < n < len(bars):
                out.setdefault(f"dc{n}", donchian(bars, n))
    return out


def _rule_vote(rule: dict, I: dict, i: int) -> int:
    """+1 bullish, -1 bearish, 0 neutral/blocked. None-safe."""
    t = rule.get("type")
    c = I["close"]

    def g(name, idx=i):
        s = I.get(name)
        return s[idx] if s and idx < len(s) and s[idx] is not None else None

    if t == "ema_stack":
        f, m, s = (g(f"ema{int(rule.get('fast', 9))}"),
                   g(f"ema{int(rule.get('mid', 21))}"),
                   g(f"ema{int(rule.get('slow', 50))}"))
        if None in (f, m, s):
            return 0
        if f > m > s:
            return 1
        if f < m < s:
            return -1
        return 0

    if t in ("ema_cross", "sma_cross"):
        pre = "ema" if t == "ema_cross" else "sma"
        fn, sn = int(rule.get("fast", 9)), int(rule.get("slow", 21))
        f0, s0 = g(f"{pre}{fn}"), g(f"{pre}{sn}")
        f1, s1 = g(f"{pre}{fn}", i - 1), g(f"{pre}{sn}", i - 1)
        if None in (f0, s0, f1, s1):
            return 0
        if f1 <= s1 and f0 > s0:
            return 1
        if f1 >= s1 and f0 < s0:
            return -1
        return 0

    if t == "rsi_zone":
        v = g(f"rsi{int(rule['period'])}") if isinstance(rule.get("period"), (int, float)) \
            else g("rsi")
        if v is None:
            v = g("rsi")
        if v is None:
            return 0
        lo, hi = float(rule.get("min", 40)), float(rule.get("max", 70))
        if lo <= v <= hi:
            return 1 if v >= 50 else -1
        return 0

    if t == "rsi_extreme":
        v = g(f"rsi{int(rule['period'])}") if isinstance(rule.get("period"), (int, float)) \
            else g("rsi")
        if v is None:
            v = g("rsi")
        if v is None:
            return 0
        if v < float(rule.get("oversold", 30)):
            return 1
        if v > float(rule.get("overbought", 70)):
            return -1
        return 0

    if t == "macd_cross":
        _key = (f"macd{int(rule.get('fast', 12))}_{int(rule.get('slow', 26))}"
                f"_{int(rule.get('signal', 9))}")
        line, sig, _ = I.get(_key) or I["macd"]
        if i < 1 or None in (line[i], sig[i], line[i - 1], sig[i - 1]):
            return 0
        if line[i - 1] <= sig[i - 1] and line[i] > sig[i]:
            return 1
        if line[i - 1] >= sig[i - 1] and line[i] < sig[i]:
            return -1
        return 0

    if t == "bb_touch":
        _key = (f"bb{int(rule['period'])}_{float(rule.get('mult', 2.0))}"
                if isinstance(rule.get("period"), (int, float)) else None)
        up, mid, lo = (I.get(_key) or I["bb"]) if _key else I["bb"]
        if None in (up[i], lo[i]):
            return 0
        if c[i] < lo[i]:
            return 1 if rule.get("mode", "reversion") == "reversion" else -1
        if c[i] > up[i]:
            return -1 if rule.get("mode", "reversion") == "reversion" else 1
        return 0

    if t == "breakout":
        _n = rule.get("period", rule.get("channel"))
        _key = f"dc{int(_n)}" if isinstance(_n, (int, float)) else None
        hi, lo = (I.get(_key) or I["dc"]) if _key else I["dc"]
        j = i - 1
        if j < 0 or hi[j] is None or lo[j] is None:
            return 0
        if c[i] > hi[j]:
            return 1
        if c[i] < lo[j]:
            return -1
        return 0

    if t == "pullback":
        ref = rule.get("to", "ema_mid")
        n = {"ema_fast": int(rule.get("fast", 9)), "ema_mid": int(rule.get("mid", 21)),
             "ema_slow": int(rule.get("slow", 50))}.get(ref, int(rule.get("to_period", 21)))
        ma, a = g(f"ema{n}"), g("atr")
        if None in (ma, a) or a <= 0:
            return 0
        return 1 if abs(c[i] - ma) / a <= float(rule.get("max_atr", 1.0)) else 0

    if t == "candle":
        if i < 1:
            return 0
        b, pb = I["bars"][i], I["bars"][i - 1]
        body, rng = abs(b.close - b.open), max(b.high - b.low, 1e-12)
        pbody = abs(pb.close - pb.open)
        bull_eng = b.close > b.open and pb.close < pb.open and body > pbody
        bear_eng = b.close < b.open and pb.close > pb.open and body > pbody
        lower_wick = min(b.open, b.close) - b.low
        upper_wick = b.high - max(b.open, b.close)
        pin_bull = lower_wick > body * 2 and body / rng < 0.4
        pin_bear = upper_wick > body * 2 and body / rng < 0.4
        if bull_eng or pin_bull:
            return 1
        if bear_eng or pin_bear:
            return -1
        return 0

    if t == "session":
        h = int(time.strftime("%H", time.gmtime(I["bars"][i].time)))
        f_, t_ = int(rule.get("from", 0)), int(rule.get("to", 24))
        return 0 if (f_ <= h < t_) else -99          # -99 = hard block

    if t == "atr_filter":
        a = g("atr")
        if a is None or c[i] <= 0:
            return 0
        pct = a / c[i] * 100
        lo, hi = float(rule.get("min_pct", 0.0)), float(rule.get("max_pct", 99.0))
        return 0 if (lo <= pct <= hi) else -99

    return 0


def evaluate_spec(spec: dict, bars: list[Bar]) -> list:
    """Signals for every bar index from a declarative spec."""
    n = len(bars)
    out: list = [None] * n
    entry = [r for r in spec.get("entry", []) if isinstance(r, dict)]
    filters = [r for r in spec.get("filters", []) if isinstance(r, dict)]
    if not entry or n < 160:
        return out

    I = _ind(bars, spec)
    ex = spec.get("exit", {}) or {}
    atr_mult = float(ex.get("atr_mult", 2.0))
    rr = float(ex.get("rr", 2.0))
    need = max(int(spec.get("params", {}).get("warmup", 150)), 60)
    # majority of entry rules must agree; any filter can veto
    threshold = float(spec.get("agreement", 0.6))

    for i in range(need, n):
        blocked = False
        for f in filters:
            if _rule_vote(f, I, i) == -99:
                blocked = True
                break
        if blocked:
            continue
        votes = []
        for r in entry:
            v = _rule_vote(r, I, i)
            if v == -99:
                votes = []
                break
            votes.append(v)
        if not votes:
            continue
        bulls = sum(1 for v in votes if v > 0)
        bears = sum(1 for v in votes if v < 0)
        total = len(votes)
        a = I["atr"][i]
        if a is None or a <= 0:
            continue
        price = I["close"][i]

        if bulls / total >= threshold and bulls > bears:
            stop = price - a * atr_mult
            conf = min(0.95, 0.4 + 0.5 * bulls / total)
            out[i] = Signal("buy", price, stop, price + (price - stop) * rr, conf,
                            f"{bulls}/{total} bullish rules agreed", meta={"atr": a})
        elif bears / total >= threshold and bears > bulls:
            stop = price + a * atr_mult
            conf = min(0.95, 0.4 + 0.5 * bears / total)
            out[i] = Signal("sell", price, stop, price - (stop - price) * rr, conf,
                            f"{bears}/{total} bearish rules agreed", meta={"atr": a})
    return out


def spec_search_space(spec: dict) -> dict[str, list]:
    """Parameter grid for optimizing an ingested strategy."""
    return {"atr_mult": [1.0, 1.5, 2.0, 2.5, 3.0],
            "rr": [1.0, 1.5, 2.0, 2.5, 3.0, 4.0],
            "agreement": [0.5, 0.6, 0.75, 1.0]}


def apply_params(spec: dict, params: dict) -> dict:
    """Return a copy of the spec with optimizer params applied."""
    s = {k: (v.copy() if isinstance(v, (dict, list)) else v) for k, v in spec.items()}
    ex = dict(s.get("exit", {}) or {})
    if "atr_mult" in params:
        ex["atr_mult"] = params["atr_mult"]
    if "rr" in params:
        ex["rr"] = params["rr"]
    s["exit"] = ex
    if "agreement" in params:
        s["agreement"] = params["agreement"]
    return s
