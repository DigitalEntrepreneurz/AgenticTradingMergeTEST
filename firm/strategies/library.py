"""Strategy library.

A strategy is a pure function: (bars, params) -> Signal | None, evaluated on the
last CLOSED bar. Deterministic and unit-testable, so the backtest agent and the
execution agent run identical logic (no train/live skew).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ..brokers.base import Bar
from ..indicators import atr, bollinger, closes, donchian, ema, macd, rsi, slope


@dataclass
class Signal:
    side: str                       # buy | sell
    entry: float
    stop: float
    take: float
    confidence: float = 0.5
    rationale: str = ""
    strategy: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


StrategyFn = Callable[[list[Bar], dict], "Signal | None"]

_REGISTRY: dict[str, dict[str, Any]] = {}


def register(name: str, description: str, default_params: dict) -> Callable:
    def deco(fn: StrategyFn) -> StrategyFn:
        _REGISTRY[name] = {"fn": fn, "description": description,
                           "default_params": default_params}
        return fn
    return deco


def get(name: str) -> dict[str, Any] | None:
    return _REGISTRY.get(name)


def all_strategies() -> dict[str, dict[str, Any]]:
    return dict(_REGISTRY)


def run(name: str, bars: list[Bar], params: dict | None = None) -> Signal | None:
    entry = _REGISTRY.get(name)
    if not entry:
        return None
    p = dict(entry["default_params"])
    p.update(params or {})
    sig = entry["fn"](bars, p)
    if sig:
        sig.strategy = name
    return sig


# ----------------------------------------------------------------------
# 1. Trend following: EMA stack + pullback, ATR stop
# ----------------------------------------------------------------------
@register("ema_trend_pullback",
          "Trade with the higher-timeframe trend: EMA fast/slow stacked, enter on "
          "a pullback to the fast EMA, ATR-based stop and R multiple target.",
          {"fast": 21, "slow": 55, "atr_n": 14, "atr_mult": 2.0, "rr": 2.0,
           "pullback_atr": 0.8, "min_slope": 0.0004})
def _ema_trend_pullback(bars: list[Bar], p: dict) -> Signal | None:
    need = max(p["slow"], p["atr_n"]) + 10
    if len(bars) < need:
        return None
    c = closes(bars)
    ef, es = ema(c, p["fast"]), ema(c, p["slow"])
    a = atr(bars, p["atr_n"])
    i = len(bars) - 1
    if None in (ef[i], es[i], a[i]) or a[i] <= 0:
        return None

    price, fast, slow, av = c[i], ef[i], es[i], a[i]
    sl_slope = slope(es, 8)
    dist = abs(price - fast) / av

    up = fast > slow and sl_slope > p["min_slope"]
    down = fast < slow and sl_slope < -p["min_slope"]
    if not (up or down) or dist > p["pullback_atr"]:
        return None

    conf = max(0.35, min(0.92, 0.55 + abs(sl_slope) * 60 + (p["pullback_atr"] - dist) * 0.2))
    if up:
        stop = price - av * p["atr_mult"]
        return Signal("buy", price, stop, price + (price - stop) * p["rr"], conf,
                      f"Uptrend (EMA{p['fast']}>{p['slow']}, slope {sl_slope:+.4f}); "
                      f"price {dist:.2f} ATR from fast EMA - pullback entry.",
                      meta={"atr": av})
    stop = price + av * p["atr_mult"]
    return Signal("sell", price, stop, price - (stop - price) * p["rr"], conf,
                  f"Downtrend (EMA{p['fast']}<{p['slow']}, slope {sl_slope:+.4f}); "
                  f"price {dist:.2f} ATR from fast EMA - pullback entry.",
                  meta={"atr": av})


# ----------------------------------------------------------------------
# 2. Donchian breakout
# ----------------------------------------------------------------------
@register("donchian_breakout",
          "Breakout of the N-bar high/low with an ATR trailing-style stop. "
          "Classic turtle-flavoured momentum.",
          {"channel": 20, "atr_n": 14, "atr_mult": 2.0, "rr": 2.5, "buffer_atr": 0.05})
def _donchian_breakout(bars: list[Bar], p: dict) -> Signal | None:
    need = max(p["channel"], p["atr_n"]) + 5
    if len(bars) < need:
        return None
    hi, lo = donchian(bars[:-1], p["channel"])     # exclude forming bar
    a = atr(bars, p["atr_n"])
    i, j = len(bars) - 1, len(bars) - 2
    if hi[j] is None or lo[j] is None or a[i] is None or a[i] <= 0:
        return None
    price, av, buf = bars[i].close, a[i], a[i] * p["buffer_atr"]

    if price > hi[j] + buf:
        stop = price - av * p["atr_mult"]
        return Signal("buy", price, stop, price + (price - stop) * p["rr"], 0.6,
                      f"Broke {p['channel']}-bar high {hi[j]:.5f} by {(price-hi[j])/av:.2f} ATR.",
                      meta={"atr": av})
    if price < lo[j] - buf:
        stop = price + av * p["atr_mult"]
        return Signal("sell", price, stop, price - (stop - price) * p["rr"], 0.6,
                      f"Broke {p['channel']}-bar low {lo[j]:.5f} by {(lo[j]-price)/av:.2f} ATR.",
                      meta={"atr": av})
    return None


# ----------------------------------------------------------------------
# 3. Mean reversion: Bollinger + RSI, trend filter
# ----------------------------------------------------------------------
@register("bollinger_reversion",
          "Fade stretched moves: close outside the Bollinger band with RSI at an "
          "extreme, only when the longer EMA is flat (no strong trend).",
          {"bb_n": 20, "bb_mult": 2.2, "rsi_n": 14, "rsi_lo": 28, "rsi_hi": 72,
           "atr_n": 14, "atr_mult": 1.6, "rr": 1.5, "flat_slope": 0.0025})
def _bollinger_reversion(bars: list[Bar], p: dict) -> Signal | None:
    need = max(p["bb_n"], p["rsi_n"], p["atr_n"]) + 10
    if len(bars) < need:
        return None
    c = closes(bars)
    up, mid, lo = bollinger(c, p["bb_n"], p["bb_mult"])
    r = rsi(c, p["rsi_n"])
    a = atr(bars, p["atr_n"])
    trend = ema(c, 100 if len(c) > 110 else 50)
    i = len(bars) - 1
    if None in (up[i], lo[i], mid[i], r[i], a[i]) or a[i] <= 0:
        return None
    if abs(slope(trend, 10)) > p["flat_slope"]:
        return None                                  # too trendy to fade

    price, av = c[i], a[i]
    if price < lo[i] and r[i] < p["rsi_lo"]:
        stop = price - av * p["atr_mult"]
        return Signal("buy", price, stop, mid[i], 0.5,
                      f"Close below lower band with RSI {r[i]:.0f} - reversion to mean "
                      f"{mid[i]:.5f} in a flat regime.", meta={"atr": av})
    if price > up[i] and r[i] > p["rsi_hi"]:
        stop = price + av * p["atr_mult"]
        return Signal("sell", price, stop, mid[i], 0.5,
                      f"Close above upper band with RSI {r[i]:.0f} - reversion to mean "
                      f"{mid[i]:.5f} in a flat regime.", meta={"atr": av})
    return None


# ----------------------------------------------------------------------
# 4. MACD momentum confirmation
# ----------------------------------------------------------------------
@register("macd_momentum",
          "MACD line crosses its signal in the direction of the 100-EMA bias, "
          "with a widening histogram.",
          {"fast": 12, "slow": 26, "signal": 9, "bias": 100, "atr_n": 14,
           "atr_mult": 2.0, "rr": 2.0})
def _macd_momentum(bars: list[Bar], p: dict) -> Signal | None:
    if len(bars) < p["bias"] + 10:
        return None
    c = closes(bars)
    line, sig, hist = macd(c, p["fast"], p["slow"], p["signal"])
    bias = ema(c, p["bias"])
    a = atr(bars, p["atr_n"])
    i, j = len(c) - 1, len(c) - 2
    if None in (line[i], sig[i], line[j], sig[j], bias[i], a[i], hist[i], hist[j]):
        return None
    if a[i] <= 0:
        return None
    price, av = c[i], a[i]

    crossed_up = line[j] <= sig[j] and line[i] > sig[i]
    crossed_dn = line[j] >= sig[j] and line[i] < sig[i]

    if crossed_up and price > bias[i] and hist[i] > hist[j]:
        stop = price - av * p["atr_mult"]
        return Signal("buy", price, stop, price + (price - stop) * p["rr"], 0.58,
                      "MACD crossed up above the 100-EMA with expanding histogram.",
                      meta={"atr": av})
    if crossed_dn and price < bias[i] and hist[i] < hist[j]:
        stop = price + av * p["atr_mult"]
        return Signal("sell", price, stop, price - (stop - price) * p["rr"], 0.58,
                      "MACD crossed down below the 100-EMA with expanding histogram.",
                      meta={"atr": av})
    return None
