"""Dependency-light technical indicators (pure python over lists of floats)."""
from __future__ import annotations

from statistics import fmean, pstdev

from .brokers.base import Bar


def closes(bars: list[Bar]) -> list[float]:
    return [b.close for b in bars]


def sma(vals: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(vals)
    if n <= 0 or len(vals) < n:
        return out
    run = sum(vals[:n])
    out[n - 1] = run / n
    for i in range(n, len(vals)):
        run += vals[i] - vals[i - n]
        out[i] = run / n
    return out


def ema(vals: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(vals)
    if n <= 0 or len(vals) < n:
        return out
    k = 2 / (n + 1)
    prev = fmean(vals[:n])
    out[n - 1] = prev
    for i in range(n, len(vals)):
        prev = vals[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(vals: list[float], n: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(vals)
    if len(vals) <= n:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = vals[i] - vals[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    ag, al = gains / n, losses / n
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n + 1, len(vals)):
        d = vals[i] - vals[i - 1]
        ag = (ag * (n - 1) + max(d, 0.0)) / n
        al = (al * (n - 1) + max(-d, 0.0)) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def atr(bars: list[Bar], n: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(bars)
    if len(bars) <= n:
        return out
    trs: list[float] = [bars[0].high - bars[0].low]
    for i in range(1, len(bars)):
        h, l, pc = bars[i].high, bars[i].low, bars[i - 1].close
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    prev = fmean(trs[1:n + 1])
    out[n] = prev
    for i in range(n + 1, len(bars)):
        prev = (prev * (n - 1) + trs[i]) / n
        out[i] = prev
    return out


def macd(vals: list[float], fast: int = 12, slow: int = 26, signal: int = 9):
    ef, es = ema(vals, fast), ema(vals, slow)
    line = [None if (ef[i] is None or es[i] is None) else ef[i] - es[i]
            for i in range(len(vals))]
    clean = [v for v in line if v is not None]
    sig_clean = ema(clean, signal)
    sig: list[float | None] = [None] * len(vals)
    offset = len(vals) - len(clean)
    for i, v in enumerate(sig_clean):
        sig[offset + i] = v
    hist = [None if (line[i] is None or sig[i] is None) else line[i] - sig[i]
            for i in range(len(vals))]
    return line, sig, hist


def bollinger(vals: list[float], n: int = 20, mult: float = 2.0):
    mid = sma(vals, n)
    up: list[float | None] = [None] * len(vals)
    lo: list[float | None] = [None] * len(vals)
    for i in range(n - 1, len(vals)):
        sd = pstdev(vals[i - n + 1:i + 1])
        up[i] = mid[i] + mult * sd
        lo[i] = mid[i] - mult * sd
    return up, mid, lo


def donchian(bars: list[Bar], n: int = 20):
    hi: list[float | None] = [None] * len(bars)
    lo: list[float | None] = [None] * len(bars)
    for i in range(n - 1, len(bars)):
        window = bars[i - n + 1:i + 1]
        hi[i] = max(b.high for b in window)
        lo[i] = min(b.low for b in window)
    return hi, lo


def slope(vals: list[float | None], lookback: int = 5) -> float:
    clean = [v for v in vals if v is not None]
    if len(clean) < lookback + 1:
        return 0.0
    a, b = clean[-lookback - 1], clean[-1]
    return 0.0 if a == 0 else (b - a) / abs(a)
