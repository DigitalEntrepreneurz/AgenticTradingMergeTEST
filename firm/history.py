"""One M1 download, every timeframe.

Downloading each timeframe separately is wasted work: M5, M15, H1, H4 and D1
are all strictly derivable from M1 by aggregation. A higher-timeframe candle is
just a group of M1 candles - first open, max high, min low, last close, summed
volume. Nothing in that operation loses information relative to what the broker
would have sent you for that timeframe, because the broker builds its own bars
the same way.

What M1 does NOT contain:

* **Intra-minute path.** Within one M1 candle you know the high and the low but
  not which came first. For a strategy whose stop and target can both be
  touched inside a single minute, the outcome is genuinely ambiguous. The
  backtester resolves that pessimistically (see `PESSIMISTIC_NOTE`), which is
  the honest choice - assume the stop hit first.
* **Spread history.** M1 OHLC is usually bid-only; the spread you pay is
  modelled, not recorded.
* **Weekend/rollover gaps** are real and preserved, but a resampled D1 bar
  boundary depends on the broker's server timezone, so a D1 built from M1 in
  UTC can differ by a few hours from the broker's own D1.

So: yes, download M1 once for the full period and derive the rest. Just do not
believe a sub-minute-precision result.
"""

from __future__ import annotations

import csv
import json
import time
from pathlib import Path
from typing import Any, Iterable

from .brokers.base import TF_MINUTES, Bar

DATA_DIR = Path(__file__).resolve().parent / "data" / "history"

PESSIMISTIC_NOTE = (
    "Bars resampled from M1: when a candle's high and low would both trigger, "
    "the stop is assumed to fill first."
)


# ---------------------------------------------------------------- resampling
def resample(bars: list[Bar], timeframe: str) -> list[Bar]:
    """Aggregate M1 bars into `timeframe`. Input must be sorted, oldest first.

    Buckets are aligned to the epoch so that every run produces identical
    boundaries - a bar's bucket is `floor(t / seconds) * seconds`, which for
    H1 means 00:00, 01:00 ... in UTC.
    """
    mins = TF_MINUTES.get(timeframe.upper())
    if not mins:
        raise ValueError(f"unknown timeframe {timeframe!r}")
    if mins == 1:
        return list(bars)
    step = mins * 60
    out: list[Bar] = []
    cur_key: float | None = None
    o = h = l = c = 0.0
    vol = 0.0
    for b in bars:
        key = (int(b.time) // step) * step
        if cur_key is None:
            cur_key, o, h, l, c, vol = key, b.open, b.high, b.low, b.close, b.volume
            continue
        if key != cur_key:
            out.append(Bar(time=float(cur_key), open=o, high=h, low=l,
                           close=c, volume=vol))
            cur_key, o, h, l, c, vol = key, b.open, b.high, b.low, b.close, b.volume
        else:
            h = max(h, b.high)
            l = min(l, b.low)
            c = b.close
            vol += b.volume
    if cur_key is not None:
        out.append(Bar(time=float(cur_key), open=o, high=h, low=l, close=c, volume=vol))
    return out


def coverage(bars: list[Bar], timeframe: str = "M1") -> dict:
    """Describe a series: span, count, and gaps big enough to matter."""
    if not bars:
        return {"bars": 0, "from": None, "to": None, "days": 0,
                "gaps": [], "expected": 0, "completeness": 0.0}
    step = TF_MINUTES.get(timeframe.upper(), 1) * 60
    first, last = bars[0].time, bars[-1].time
    gaps = []
    for a, b in zip(bars, bars[1:]):
        d = b.time - a.time
        if d > step * 5:                      # ignore ordinary weekend closes
            gaps.append({"from": a.time, "to": b.time, "hours": round(d / 3600, 1)})
    span = max(1.0, last - first)
    expected = int(span / step) + 1
    # Markets close at weekends: ~5/7 of wall-clock time is tradeable.
    tradeable = expected * 5 / 7
    return {
        "bars": len(bars), "from": first, "to": last,
        "days": round(span / 86400, 1),
        "gaps": sorted(gaps, key=lambda g: -g["hours"])[:20],
        "gap_count": len(gaps),
        "expected": expected,
        "completeness": round(min(1.0, len(bars) / max(1.0, tradeable)) * 100, 1),
    }


# ---------------------------------------------------------------- storage
def _path(symbol: str) -> Path:
    return DATA_DIR / f"{symbol.upper()}_M1.csv"


def save_m1(symbol: str, bars: list[Bar]) -> Path:
    """Persist M1 to CSV. Merges with anything already stored, de-duplicated."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    existing = {int(b.time): b for b in load_m1(symbol)}
    for b in bars:
        existing[int(b.time)] = b
    merged = [existing[k] for k in sorted(existing)]
    p = _path(symbol)
    with p.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["time", "open", "high", "low", "close", "volume"])
        for b in merged:
            w.writerow([int(b.time), b.open, b.high, b.low, b.close, b.volume])
    return p


def load_m1(symbol: str) -> list[Bar]:
    p = _path(symbol)
    if not p.exists():
        return []
    out: list[Bar] = []
    with p.open(newline="") as fh:
        for row in csv.DictReader(fh):
            try:
                out.append(Bar(time=float(row["time"]), open=float(row["open"]),
                               high=float(row["high"]), low=float(row["low"]),
                               close=float(row["close"]),
                               volume=float(row.get("volume") or 0)))
            except (TypeError, ValueError):
                continue
    out.sort(key=lambda b: b.time)
    return out


def import_csv(symbol: str, path: str | Path, tz_offset_hours: float = 0.0) -> dict:
    """Import a broker/HistData M1 CSV.

    Tolerates the common layouts: an epoch column, or separate date+time, or a
    single 'YYYY.MM.DD HH:MM' field. Anything unparseable is skipped and
    counted rather than silently dropped.
    """
    src = Path(path)
    rows_in = skipped = 0
    bars: list[Bar] = []
    with src.open(newline="") as fh:
        sample = fh.read(4096)
        fh.seek(0)
        delim = ";" if sample.count(";") > sample.count(",") else ","
        has_header = any(c.isalpha() for c in sample.split("\n")[0].replace("T", ""))
        reader = csv.reader(fh, delimiter=delim)
        if has_header:
            next(reader, None)
        for row in reader:
            rows_in += 1
            try:
                bars.append(_parse_row(row, tz_offset_hours))
            except (ValueError, IndexError, TypeError):
                skipped += 1
    bars.sort(key=lambda b: b.time)
    if bars:
        save_m1(symbol, bars)
    cov = coverage(bars, "M1")
    return {"symbol": symbol.upper(), "rows_read": rows_in, "skipped": skipped,
            "imported": len(bars), "file": str(src), **cov}


def _parse_row(row: list[str], tz_offset_hours: float) -> Bar:
    off = tz_offset_hours * 3600
    first = row[0].strip()
    # date and time in separate columns
    if len(row) >= 6 and (":" in row[1] or "-" in row[1]) and not _is_number(row[1]):
        t = _to_epoch(f"{first} {row[1].strip()}") - off
        o, h, l, c = (float(row[i]) for i in (2, 3, 4, 5))
        v = float(row[6]) if len(row) > 6 and _is_number(row[6]) else 0.0
        return Bar(time=t, open=o, high=h, low=l, close=c, volume=v)
    # single timestamp column, epoch or formatted
    t = float(first) if _is_number(first) else _to_epoch(first)
    t -= off
    o, h, l, c = (float(row[i]) for i in (1, 2, 3, 4))
    v = float(row[5]) if len(row) > 5 and _is_number(row[5]) else 0.0
    return Bar(time=t, open=o, high=h, low=l, close=c, volume=v)


def _is_number(s: str) -> bool:
    try:
        float(s)
        return True
    except (TypeError, ValueError):
        return False


def _to_epoch(s: str) -> float:
    s = s.strip().replace("T", " ")
    for fmt in ("%Y.%m.%d %H:%M:%S", "%Y.%m.%d %H:%M", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M", "%Y%m%d %H%M%S", "%Y%m%d %H%M",
                "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M"):
        try:
            return time.mktime(time.strptime(s, fmt))
        except ValueError:
            continue
    raise ValueError(f"unparseable timestamp {s!r}")


# ---------------------------------------------------------------- download
def download_m1(broker: Any, symbol: str, days: int = 3650,
                chunk: int = 20000, progress=None) -> dict:
    """Pull M1 history from a broker in chunks and store it.

    Brokers cap a single `bars()` call, so this walks backwards. Stops early
    when the broker stops returning new candles.
    """
    got: dict[int, Bar] = {b.time and int(b.time): b for b in load_m1(symbol)}
    target = days * 24 * 60
    fetched = 0
    while fetched < target:
        n = min(chunk, target - fetched)
        try:
            batch = broker.bars(symbol, "M1", n)
        except Exception as e:
            return {"symbol": symbol, "error": str(e), "stored": len(got)}
        if not batch:
            break
        before = len(got)
        for b in batch:
            got[int(b.time)] = b
        if len(got) == before:
            break                       # broker has no more history
        fetched += n
        if progress:
            progress(len(got), target)
    bars = [got[k] for k in sorted(got)]
    if bars:
        save_m1(symbol, bars)
    return {"symbol": symbol.upper(), "stored": len(bars), **coverage(bars, "M1")}


# ---------------------------------------------------------------- serving
class HistoryBroker:
    """Wraps a broker so `bars()` is served from stored M1 by resampling.

    Falls through to the live broker for symbols with no stored history, so it
    is a drop-in for Lab and the backtester.
    """

    def __init__(self, inner: Any, symbols: Iterable[str] | None = None):
        self.inner = inner
        self._cache: dict[str, list[Bar]] = {}
        for s in (symbols or []):
            self._cache[s.upper()] = load_m1(s)

    def _m1(self, symbol: str) -> list[Bar]:
        s = symbol.upper()
        if s not in self._cache:
            self._cache[s] = load_m1(s)
        return self._cache[s]

    def bars(self, symbol: str, timeframe: str, count: int) -> list[Bar]:
        m1 = self._m1(symbol)
        if not m1:
            return self.inner.bars(symbol, timeframe, count)
        series = resample(m1, timeframe)
        return series[-count:] if count else series

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


def stored_symbols() -> list[dict]:
    """What history is on disk, for the dashboard and CLI."""
    if not DATA_DIR.exists():
        return []
    out = []
    for p in sorted(DATA_DIR.glob("*_M1.csv")):
        sym = p.stem.replace("_M1", "")
        bars = load_m1(sym)
        cov = coverage(bars, "M1")
        out.append({"symbol": sym, "file": str(p),
                    "size_mb": round(p.stat().st_size / 1e6, 2), **cov})
    return out
