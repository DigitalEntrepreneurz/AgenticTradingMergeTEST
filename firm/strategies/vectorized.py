"""Vectorized strategy evaluation.

The naive backtest loop calls a strategy with bars[:i+1] for every i, so every
indicator is rebuilt from scratch on every bar - O(n^2) and dominated by ATR.

Here each strategy is expressed once over the FULL series: indicators are
computed a single time, then a cheap per-bar rule reads index i. Same maths as
`library.py` (guaranteed by tests), 30-100x faster.
"""
from __future__ import annotations

from typing import Any, Callable

from ..brokers.base import Bar
from ..indicators import atr, bollinger, closes, donchian, ema, macd, rsi
from .library import Signal, all_strategies

# name -> fn(bars, params) -> list[Signal | None] aligned to bars
_VEC: dict[str, Callable[[list[Bar], dict], list]] = {}


def register_vec(name: str):
    def deco(fn):
        _VEC[name] = fn
        return fn
    return deco


def has_vector(name: str) -> bool:
    return name in _VEC


def evaluate(name: str, bars: list[Bar], params: dict | None = None) -> list:
    """Signals for every bar index, or [] if this strategy has no fast path."""
    fn = _VEC.get(name)
    if not fn:
        return []
    lib = all_strategies().get(name, {})
    p = dict(lib.get("default_params", {}))
    p.update(params or {})
    out = fn(bars, p)
    for s in out:
        if s:
            s.strategy = name
    return out


def _slope_at(series: list, i: int, lookback: int) -> float:
    """Slope over the last `lookback` valid points ending at i (matches indicators.slope)."""
    if series[i] is None:
        return 0.0
    seen = []
    j = i
    while j >= 0 and len(seen) < lookback + 1:
        if series[j] is not None:
            seen.append(series[j])
        j -= 1
    if len(seen) < lookback + 1:
        return 0.0
    a, b = seen[lookback], seen[0]
    return 0.0 if a == 0 else (b - a) / abs(a)


# ----------------------------------------------------------------------
@register_vec("ema_trend_pullback")
def _v_ema_trend(bars: list[Bar], p: dict) -> list:
    n = len(bars)
    out: list = [None] * n
    need = max(p["slow"], p["atr_n"]) + 10
    if n < need:
        return out
    c = closes(bars)
    ef, es = ema(c, p["fast"]), ema(c, p["slow"])
    a = atr(bars, p["atr_n"])
    for i in range(need - 1, n):
        if ef[i] is None or es[i] is None or a[i] is None or a[i] <= 0:
            continue
        price, fast, slow, av = c[i], ef[i], es[i], a[i]
        sl = _slope_at(es, i, 8)
        dist = abs(price - fast) / av
        up = fast > slow and sl > p["min_slope"]
        down = fast < slow and sl < -p["min_slope"]
        if not (up or down) or dist > p["pullback_atr"]:
            continue
        conf = max(0.35, min(0.92, 0.55 + abs(sl) * 60 + (p["pullback_atr"] - dist) * 0.2))
        if up:
            stop = price - av * p["atr_mult"]
            out[i] = Signal("buy", price, stop, price + (price - stop) * p["rr"], conf,
                            f"Uptrend (EMA{p['fast']}>{p['slow']}, slope {sl:+.4f}); "
                            f"price {dist:.2f} ATR from fast EMA - pullback entry.",
                            meta={"atr": av})
        else:
            stop = price + av * p["atr_mult"]
            out[i] = Signal("sell", price, stop, price - (stop - price) * p["rr"], conf,
                            f"Downtrend (EMA{p['fast']}<{p['slow']}, slope {sl:+.4f}); "
                            f"price {dist:.2f} ATR from fast EMA - pullback entry.",
                            meta={"atr": av})
    return out


@register_vec("donchian_breakout")
def _v_donchian(bars: list[Bar], p: dict) -> list:
    n = len(bars)
    out: list = [None] * n
    need = max(p["channel"], p["atr_n"]) + 5
    if n < need:
        return out
    a = atr(bars, p["atr_n"])
    # donchian over bars[:-1] at each i == channel of bars[:i] evaluated at i-1
    hi, lo = donchian(bars, p["channel"])
    for i in range(need - 1, n):
        j = i - 1
        if j < 0 or hi[j] is None or lo[j] is None or a[i] is None or a[i] <= 0:
            continue
        price, av = bars[i].close, a[i]
        buf = av * p["buffer_atr"]
        if price > hi[j] + buf:
            stop = price - av * p["atr_mult"]
            out[i] = Signal("buy", price, stop, price + (price - stop) * p["rr"], 0.6,
                            f"Broke {p['channel']}-bar high {hi[j]:.5f} by "
                            f"{(price-hi[j])/av:.2f} ATR.", meta={"atr": av})
        elif price < lo[j] - buf:
            stop = price + av * p["atr_mult"]
            out[i] = Signal("sell", price, stop, price - (stop - price) * p["rr"], 0.6,
                            f"Broke {p['channel']}-bar low {lo[j]:.5f} by "
                            f"{(lo[j]-price)/av:.2f} ATR.", meta={"atr": av})
    return out


@register_vec("bollinger_reversion")
def _v_bollinger(bars: list[Bar], p: dict) -> list:
    n = len(bars)
    out: list = [None] * n
    need = max(p["bb_n"], p["rsi_n"], p["atr_n"]) + 10
    if n < need:
        return out
    c = closes(bars)
    up, mid, lo = bollinger(c, p["bb_n"], p["bb_mult"])
    r = rsi(c, p["rsi_n"])
    a = atr(bars, p["atr_n"])
    # library.py picks the trend EMA length from the slice length, so the period
    # switches from 50 to 100 at slice length 111. Precompute both once.
    trend50, trend100 = ema(c, 50), ema(c, 100)
    for i in range(need - 1, n):
        if None in (up[i], lo[i], mid[i], r[i], a[i]) or a[i] <= 0:
            continue
        trend = trend100 if (i + 1) > 110 else trend50
        if abs(_slope_at(trend, i, 10)) > p["flat_slope"]:
            continue
        price, av = c[i], a[i]
        if price < lo[i] and r[i] < p["rsi_lo"]:
            out[i] = Signal("buy", price, price - av * p["atr_mult"], mid[i], 0.5,
                            f"Close below lower band with RSI {r[i]:.0f} - reversion to "
                            f"mean {mid[i]:.5f} in a flat regime.", meta={"atr": av})
        elif price > up[i] and r[i] > p["rsi_hi"]:
            out[i] = Signal("sell", price, price + av * p["atr_mult"], mid[i], 0.5,
                            f"Close above upper band with RSI {r[i]:.0f} - reversion to "
                            f"mean {mid[i]:.5f} in a flat regime.", meta={"atr": av})
    return out


@register_vec("macd_momentum")
def _v_macd(bars: list[Bar], p: dict) -> list:
    n = len(bars)
    out: list = [None] * n
    if n < p["bias"] + 10:
        return out
    c = closes(bars)
    a = atr(bars, p["atr_n"])
    bias = ema(c, p["bias"])
    # MACD's signal line is an EMA of the MACD line; its seeding is stable, so
    # the full-series computation is bit-identical to per-slice (verified in tests).
    line, sig, hist = macd(c, p["fast"], p["slow"], p["signal"])
    for i in range(p["bias"] + 9, n):
        j = i - 1
        k = i
        if None in (line[k], sig[k], line[j], sig[j], bias[i], a[i], hist[k], hist[j]):
            continue
        if a[i] <= 0:
            continue
        price, av = c[i], a[i]
        up = line[j] <= sig[j] and line[k] > sig[k]
        dn = line[j] >= sig[j] and line[k] < sig[k]
        if up and price > bias[i] and hist[k] > hist[j]:
            stop = price - av * p["atr_mult"]
            out[i] = Signal("buy", price, stop, price + (price - stop) * p["rr"], 0.58,
                            "MACD crossed up above the 100-EMA with expanding histogram.",
                            meta={"atr": av})
        elif dn and price < bias[i] and hist[k] < hist[j]:
            stop = price + av * p["atr_mult"]
            out[i] = Signal("sell", price, stop, price - (stop - price) * p["rr"], 0.58,
                            "MACD crossed down below the 100-EMA with expanding histogram.",
                            meta={"atr": av})
    return out
