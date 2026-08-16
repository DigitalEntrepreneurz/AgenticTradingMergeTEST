"""Event-driven bar backtester with spread, stop/target priority and metrics."""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from statistics import fmean, pstdev

from .brokers.base import Bar, SymbolSpec
from .strategies.library import run as run_strategy


@dataclass
class BTTrade:
    side: str
    entry_i: int
    entry: float
    stop: float
    take: float
    exit_i: int = -1
    exit: float = 0.0
    r: float = 0.0
    reason: str = ""


@dataclass
class BTResult:
    strategy: str
    symbol: str
    timeframe: str
    trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    total_r: float = 0.0
    expectancy_r: float = 0.0
    profit_factor: float = 0.0
    max_drawdown_r: float = 0.0
    sharpe: float = 0.0
    avg_bars_held: float = 0.0
    equity_curve: list[float] = field(default_factory=list)
    detail: list[dict] = field(default_factory=list)

    def summary(self) -> dict:
        d = asdict(self)
        d.pop("equity_curve", None)
        d.pop("detail", None)
        return d

    def verdict(self, min_trades: int = 12, min_pf: float = 1.25,
                min_expectancy: float = 0.05) -> tuple[bool, str]:
        if self.trades < min_trades:
            return False, f"only {self.trades} trades (need >= {min_trades})"
        if self.profit_factor < min_pf:
            return False, f"profit factor {self.profit_factor:.2f} < {min_pf}"
        if self.expectancy_r < min_expectancy:
            return False, f"expectancy {self.expectancy_r:.3f}R < {min_expectancy}R"
        if self.max_drawdown_r > max(6.0, abs(self.total_r) * 1.5):
            return False, f"drawdown {self.max_drawdown_r:.1f}R too deep vs return"
        return True, (f"PF {self.profit_factor:.2f}, expectancy {self.expectancy_r:.3f}R "
                      f"over {self.trades} trades, maxDD {self.max_drawdown_r:.1f}R")


def backtest(strategy: str, bars: list[Bar], symbol: str, timeframe: str,
             params: dict | None = None, spec: SymbolSpec | None = None,
             warmup: int = 120, spread_pts: float = 10.0,
             max_hold_bars: int = 200, force_scalar: bool = False) -> BTResult:
    """Walk bars forward; only one position at a time (per symbol/strategy)."""
    res = BTResult(strategy=strategy, symbol=symbol, timeframe=timeframe)
    if len(bars) < warmup + 30:
        return res

    # Fast path: compute every indicator once over the full series instead of
    # rebuilding them on each bar. Falls back to the per-bar call automatically.
    precomputed: list = []
    if not force_scalar:
        try:
            from .strategies.vectorized import evaluate, has_vector
            if has_vector(strategy):
                precomputed = evaluate(strategy, bars, params)
        except Exception:
            precomputed = []

    point = spec.point if spec else 0.00001
    spread = spread_pts * point
    open_t: BTTrade | None = None
    r_series: list[float] = []
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    hold: list[int] = []

    for i in range(warmup, len(bars)):
        bar = bars[i]

        # --- manage the open trade on this bar (stop wins ties) ---
        if open_t:
            risk = abs(open_t.entry - open_t.stop) or point
            exit_px = None
            reason = ""
            if open_t.side == "buy":
                if bar.low <= open_t.stop:
                    exit_px, reason = open_t.stop, "stop"
                elif bar.high >= open_t.take:
                    exit_px, reason = open_t.take, "target"
            else:
                if bar.high >= open_t.stop:
                    exit_px, reason = open_t.stop, "stop"
                elif bar.low <= open_t.take:
                    exit_px, reason = open_t.take, "target"
            if exit_px is None and i - open_t.entry_i >= max_hold_bars:
                exit_px, reason = bar.close, "timeout"

            if exit_px is not None:
                gross = ((exit_px - open_t.entry) if open_t.side == "buy"
                         else (open_t.entry - exit_px))
                r = (gross - spread) / risk
                open_t.exit_i, open_t.exit, open_t.r, open_t.reason = i, exit_px, r, reason
                res.detail.append(asdict(open_t))
                r_series.append(r)
                hold.append(i - open_t.entry_i)
                equity += r
                res.equity_curve.append(round(equity, 4))
                peak = max(peak, equity)
                max_dd = max(max_dd, peak - equity)
                open_t = None

        # --- look for a new entry on closed data ---
        if open_t is None:
            sig = (precomputed[i] if precomputed
                   else run_strategy(strategy, bars[:i + 1], params))
            if sig and sig.stop and abs(sig.entry - sig.stop) > point:
                entry = sig.entry + (spread if sig.side == "buy" else -spread) * 0.5
                open_t = BTTrade(side=sig.side, entry_i=i, entry=entry,
                                 stop=sig.stop, take=sig.take)

    # --- metrics ---
    res.trades = len(r_series)
    if not res.trades:
        return res
    wins = [r for r in r_series if r > 0]
    losses = [r for r in r_series if r <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    res.wins, res.losses = len(wins), len(losses)
    res.win_rate = round(len(wins) / res.trades * 100, 2)
    res.total_r = round(sum(r_series), 3)
    res.expectancy_r = round(fmean(r_series), 4)
    res.profit_factor = round(gross_win / gross_loss, 3) if gross_loss > 0 else (
        999.0 if gross_win > 0 else 0.0)
    res.max_drawdown_r = round(max_dd, 3)
    res.avg_bars_held = round(fmean(hold), 1) if hold else 0.0
    if len(r_series) > 2:
        sd = pstdev(r_series)
        res.sharpe = round((fmean(r_series) / sd) * math.sqrt(len(r_series)), 3) if sd else 0.0
    return res


def backtest_precomputed(signals: list, bars: list[Bar], symbol: str, timeframe: str,
                         spec: SymbolSpec | None = None, spread_pts: float = 10.0,
                         warmup: int = 120, max_hold_bars: int = 200,
                         name: str = "composite") -> BTResult:
    """Backtest from a pre-computed signal-per-bar list (ingested specs)."""
    res = BTResult(strategy=name, symbol=symbol, timeframe=timeframe)
    if len(bars) < warmup + 30 or not signals:
        return res

    point = spec.point if spec else 0.00001
    spread = spread_pts * point
    open_t: BTTrade | None = None
    r_series: list[float] = []
    equity = peak = max_dd = 0.0
    hold: list[int] = []

    for i in range(warmup, len(bars)):
        bar = bars[i]
        if open_t:
            risk = abs(open_t.entry - open_t.stop) or point
            exit_px = None
            reason = ""
            if open_t.side == "buy":
                if bar.low <= open_t.stop:
                    exit_px, reason = open_t.stop, "stop"
                elif bar.high >= open_t.take:
                    exit_px, reason = open_t.take, "target"
            else:
                if bar.high >= open_t.stop:
                    exit_px, reason = open_t.stop, "stop"
                elif bar.low <= open_t.take:
                    exit_px, reason = open_t.take, "target"
            if exit_px is None and i - open_t.entry_i >= max_hold_bars:
                exit_px, reason = bar.close, "timeout"
            if exit_px is not None:
                gross = ((exit_px - open_t.entry) if open_t.side == "buy"
                         else (open_t.entry - exit_px))
                r = (gross - spread) / risk
                open_t.exit_i, open_t.exit, open_t.r, open_t.reason = i, exit_px, r, reason
                res.detail.append(asdict(open_t))
                r_series.append(r)
                hold.append(i - open_t.entry_i)
                equity += r
                res.equity_curve.append(round(equity, 4))
                peak = max(peak, equity)
                max_dd = max(max_dd, peak - equity)
                open_t = None

        if open_t is None and i < len(signals):
            sig = signals[i]
            if sig and sig.stop and abs(sig.entry - sig.stop) > point:
                entry = sig.entry + (spread if sig.side == "buy" else -spread) * 0.5
                open_t = BTTrade(side=sig.side, entry_i=i, entry=entry,
                                 stop=sig.stop, take=sig.take)

    res.trades = len(r_series)
    if not res.trades:
        return res
    wins = [r for r in r_series if r > 0]
    losses = [r for r in r_series if r <= 0]
    gross_win, gross_loss = sum(wins), abs(sum(losses))
    res.wins, res.losses = len(wins), len(losses)
    res.win_rate = round(len(wins) / res.trades * 100, 2)
    res.total_r = round(sum(r_series), 3)
    res.expectancy_r = round(fmean(r_series), 4)
    res.profit_factor = round(gross_win / gross_loss, 3) if gross_loss > 0 else (
        999.0 if gross_win > 0 else 0.0)
    res.max_drawdown_r = round(max_dd, 3)
    res.avg_bars_held = round(fmean(hold), 1) if hold else 0.0
    if len(r_series) > 2:
        sd = pstdev(r_series)
        res.sharpe = round((fmean(r_series) / sd) * math.sqrt(len(r_series)), 3) if sd else 0.0
    return res


def optimize(strategy: str, bars: list[Bar], symbol: str, timeframe: str,
             grid: dict[str, list], spec: SymbolSpec | None = None,
             spread_pts: float = 10.0, max_combos: int = 24
             ) -> list[tuple[dict, BTResult]]:
    """Small grid search. Returns (params, result) sorted by expectancy * sqrt(trades)."""
    keys = list(grid)
    combos: list[dict] = [{}]
    for k in keys:
        combos = [dict(c, **{k: v}) for c in combos for v in grid[k]]
    combos = combos[:max_combos]

    scored: list[tuple[dict, BTResult]] = []
    for params in combos:
        r = backtest(strategy, bars, symbol, timeframe, params, spec, spread_pts=spread_pts)
        scored.append((params, r))
    scored.sort(key=lambda t: t[1].expectancy_r * math.sqrt(max(t[1].trades, 1)), reverse=True)
    return scored


def walk_forward(strategy: str, bars: list[Bar], symbol: str, timeframe: str,
                 grid: dict[str, list], folds: int = 3, spec: SymbolSpec | None = None,
                 spread_pts: float = 10.0) -> dict:
    """Optimise in-sample, verify out-of-sample. Guards against curve fitting."""
    n = len(bars)
    if n < 600:
        return {"folds": 0, "note": "not enough history for walk-forward"}
    seg = n // (folds + 1)
    oos: list[BTResult] = []
    chosen: list[dict] = []
    for f in range(folds):
        tr = bars[: seg * (f + 1)]
        te = bars[seg * (f + 1): seg * (f + 2)]
        if len(te) < 150:
            continue
        best = optimize(strategy, tr, symbol, timeframe, grid, spec, spread_pts)
        if not best:
            continue
        params = best[0][0]
        chosen.append(params)
        oos.append(backtest(strategy, te, symbol, timeframe, params, spec,
                            warmup=120, spread_pts=spread_pts))
    if not oos:
        return {"folds": 0, "note": "no valid folds"}
    tot = sum(r.total_r for r in oos)
    trades = sum(r.trades for r in oos)
    return {
        "folds": len(oos),
        "oos_total_r": round(tot, 3),
        "oos_trades": trades,
        "oos_expectancy_r": round(tot / trades, 4) if trades else 0.0,
        "params_per_fold": chosen,
        "robust": trades >= 10 and tot > 0,
    }
