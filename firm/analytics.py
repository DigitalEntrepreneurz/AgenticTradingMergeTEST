"""Performance analytics: everything the dashboard visualises.

Pure functions over trade rows so the same maths serves the live dashboard,
the strategy lab and any exported report.
"""
from __future__ import annotations

import math
import time
from collections import defaultdict
from dataclasses import dataclass, field
from statistics import fmean, pstdev
from typing import Any, Iterable

SEC_DAY = 86400


# ----------------------------------------------------------------------
# core metric block
# ----------------------------------------------------------------------
@dataclass
class Metrics:
    trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    net_profit: float = 0.0
    profit_factor: float = 0.0
    expectancy: float = 0.0
    expectancy_r: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    payoff_ratio: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0
    recovery_factor: float = 0.0
    sharpe: float = 0.0
    sortino: float = 0.0
    calmar: float = 0.0
    max_win_streak: int = 0
    max_loss_streak: int = 0
    avg_hold_hours: float = 0.0
    kelly_pct: float = 0.0
    total_r: float = 0.0
    sqn: float = 0.0

    def dict(self) -> dict:
        return {k: (round(v, 4) if isinstance(v, float) else v)
                for k, v in self.__dict__.items()}


def _pnls(trades: Iterable[dict]) -> list[float]:
    return [float(t.get("pnl") or 0.0) for t in trades]


def compute_metrics(trades: list[dict], starting_equity: float = 10_000.0) -> Metrics:
    """Metrics from CLOSED trades (each needs pnl; entry/stop enable R stats)."""
    m = Metrics()
    closed = [t for t in trades if t.get("status") == "closed" and t.get("pnl") is not None]
    if not closed:
        return m
    closed = sorted(closed, key=lambda t: t.get("closed_at") or t.get("created_at") or 0)
    p = _pnls(closed)

    wins = [x for x in p if x > 0]
    losses = [x for x in p if x < 0]
    m.trades = len(p)
    m.wins, m.losses = len(wins), len(losses)
    m.win_rate = len(wins) / len(p) * 100
    m.gross_profit = sum(wins)
    m.gross_loss = abs(sum(losses))
    m.net_profit = sum(p)
    m.profit_factor = (m.gross_profit / m.gross_loss) if m.gross_loss > 0 else (
        999.0 if m.gross_profit > 0 else 0.0)
    m.expectancy = fmean(p)
    m.avg_win = fmean(wins) if wins else 0.0
    m.avg_loss = fmean(losses) if losses else 0.0
    m.payoff_ratio = abs(m.avg_win / m.avg_loss) if m.avg_loss else 0.0
    m.largest_win = max(p) if p else 0.0
    m.largest_loss = min(p) if p else 0.0

    # R multiples where we know the initial risk
    rs: list[float] = []
    for t in closed:
        risk_money = risk_usd(t)
        if risk_money > 0:
            rs.append(float(t["pnl"]) / risk_money)
    if rs:
        m.total_r = sum(rs)
        m.expectancy_r = fmean(rs)
        if len(rs) > 1 and pstdev(rs) > 0:
            m.sqn = fmean(rs) / pstdev(rs) * math.sqrt(len(rs))

    # equity path & drawdown
    eq = starting_equity
    peak = starting_equity
    max_dd = 0.0
    max_dd_pct = 0.0
    for x in p:
        eq += x
        peak = max(peak, eq)
        dd = peak - eq
        if dd > max_dd:
            max_dd = dd
            max_dd_pct = dd / peak * 100 if peak else 0.0
    m.max_drawdown = max_dd
    m.max_drawdown_pct = max_dd_pct
    m.recovery_factor = (m.net_profit / max_dd) if max_dd > 0 else 0.0

    # risk-adjusted
    if len(p) > 1:
        sd = pstdev(p)
        m.sharpe = (fmean(p) / sd * math.sqrt(len(p))) if sd > 0 else 0.0
        downside = [x for x in p if x < 0]
        dsd = pstdev(downside) if len(downside) > 1 else 0.0
        m.sortino = (fmean(p) / dsd * math.sqrt(len(p))) if dsd > 0 else 0.0

    span_days = 0.0
    if closed:
        t0 = closed[0].get("created_at") or 0
        t1 = closed[-1].get("closed_at") or closed[-1].get("created_at") or 0
        span_days = max((t1 - t0) / SEC_DAY, 0.0)
    if span_days > 0 and max_dd_pct > 0:
        ret_pct = m.net_profit / starting_equity * 100
        annual = ret_pct * (365 / span_days) if span_days else 0.0
        m.calmar = annual / max_dd_pct

    # streaks
    cw = cl = 0
    for x in p:
        if x > 0:
            cw += 1; cl = 0
        else:
            cl += 1; cw = 0
        m.max_win_streak = max(m.max_win_streak, cw)
        m.max_loss_streak = max(m.max_loss_streak, cl)

    holds = [((t.get("closed_at") or 0) - (t.get("created_at") or 0)) / 3600
             for t in closed if t.get("closed_at") and t.get("created_at")]
    m.avg_hold_hours = fmean(holds) if holds else 0.0

    # Kelly fraction
    if m.payoff_ratio > 0:
        w = m.win_rate / 100
        m.kelly_pct = max(0.0, (w - (1 - w) / m.payoff_ratio) * 100)
    return m


# ----------------------------------------------------------------------
# series for charts
# ----------------------------------------------------------------------
def equity_series(trades: list[dict], starting_equity: float = 10_000.0) -> dict:
    """Equity curve, underwater drawdown curve and peak line."""
    closed = sorted([t for t in trades if t.get("status") == "closed"
                     and t.get("pnl") is not None],
                    key=lambda t: t.get("closed_at") or 0)
    pts = [{"i": 0, "t": (closed[0].get("created_at") if closed else time.time()),
            "equity": starting_equity, "peak": starting_equity, "dd": 0.0, "dd_pct": 0.0,
            "pnl": 0.0}]
    eq = peak = starting_equity
    for i, t in enumerate(closed, 1):
        eq += float(t["pnl"])
        peak = max(peak, eq)
        pts.append({"i": i, "t": t.get("closed_at") or 0, "equity": round(eq, 2),
                    "peak": round(peak, 2), "dd": round(eq - peak, 2),
                    "dd_pct": round((eq - peak) / peak * 100 if peak else 0, 3),
                    "pnl": round(float(t["pnl"]), 2),
                    "symbol": t.get("symbol"), "side": t.get("side"),
                    "strategy": (t.get("meta_strategy") or "")})
    return {"points": pts, "start": starting_equity, "end": round(eq, 2),
            "return_pct": round((eq - starting_equity) / starting_equity * 100, 3)
            if starting_equity else 0.0}


def r_distribution(trades: list[dict], bins: int = 13) -> dict:
    """Histogram of per-trade P&L, bucketed symmetrically around zero."""
    p = [float(t["pnl"]) for t in trades
         if t.get("status") == "closed" and t.get("pnl") is not None]
    if not p:
        return {"bins": [], "max": 0}
    lim = max(abs(min(p)), abs(max(p))) or 1.0
    step = (lim * 2) / bins
    counts = [0] * bins
    for x in p:
        idx = min(bins - 1, max(0, int((x + lim) / step)))
        counts[idx] += 1
    out = []
    for i, c in enumerate(counts):
        lo = -lim + i * step
        out.append({"lo": round(lo, 2), "hi": round(lo + step, 2), "count": c,
                    "positive": lo + step / 2 > 0})
    return {"bins": out, "max": max(counts) if counts else 0}


def breakdown(trades: list[dict], key: str) -> list[dict]:
    """Per-symbol / per-strategy / per-side performance table."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        if t.get("status") != "closed" or t.get("pnl") is None:
            continue
        groups[str(t.get(key) or "unknown")].append(t)
    rows = []
    for name, ts in groups.items():
        m = compute_metrics(ts)
        rows.append({"name": name, "trades": m.trades, "win_rate": round(m.win_rate, 1),
                     "net": round(m.net_profit, 2),
                     "pf": round(m.profit_factor, 2),
                     "expectancy": round(m.expectancy, 2),
                     "max_dd": round(m.max_drawdown, 2)})
    rows.sort(key=lambda r: r["net"], reverse=True)
    return rows


def daily_pnl(trades: list[dict], days: int = 30) -> list[dict]:
    """Calendar of realised P&L per day."""
    buckets: dict[str, float] = defaultdict(float)
    counts: dict[str, int] = defaultdict(int)
    for t in trades:
        if t.get("status") != "closed" or t.get("pnl") is None:
            continue
        ts = t.get("closed_at") or t.get("created_at") or 0
        d = time.strftime("%Y-%m-%d", time.localtime(ts))
        buckets[d] += float(t["pnl"])
        counts[d] += 1
    out = []
    now = time.time()
    for i in range(days - 1, -1, -1):
        d = time.strftime("%Y-%m-%d", time.localtime(now - i * SEC_DAY))
        out.append({"date": d, "pnl": round(buckets.get(d, 0.0), 2),
                    "trades": counts.get(d, 0)})
    return out


def hourly_profile(trades: list[dict]) -> list[dict]:
    """Which hours of the day actually make money."""
    agg: dict[int, list[float]] = defaultdict(list)
    for t in trades:
        if t.get("status") != "closed" or t.get("pnl") is None:
            continue
        h = int(time.strftime("%H", time.gmtime(t.get("created_at") or 0)))
        agg[h].append(float(t["pnl"]))
    return [{"hour": h, "pnl": round(sum(agg.get(h, [])), 2),
             "trades": len(agg.get(h, []))} for h in range(24)]


def rolling_metric(trades: list[dict], window: int = 20) -> list[dict]:
    """Rolling win rate and profit factor - shows if an edge is decaying."""
    closed = sorted([t for t in trades if t.get("status") == "closed"
                     and t.get("pnl") is not None],
                    key=lambda t: t.get("closed_at") or 0)
    out = []
    for i in range(len(closed)):
        w = closed[max(0, i - window + 1): i + 1]
        p = _pnls(w)
        wins = [x for x in p if x > 0]
        gl = abs(sum(x for x in p if x < 0))
        out.append({"i": i + 1,
                    "win_rate": round(len(wins) / len(p) * 100, 1) if p else 0,
                    "pf": round(sum(wins) / gl, 2) if gl > 0 else
                    (9.99 if wins else 0)})
    return out


def monte_carlo(trades: list[dict], runs: int = 400, starting_equity: float = 10_000.0,
                seed: int = 42, bootstrap: bool = True) -> dict:
    """How much of this curve is skill and how much is luck?

    Uses BOOTSTRAP resampling (draw trades with replacement) rather than a plain
    shuffle. Shuffling only reorders the same trades, so every run ends at the
    identical final equity and the percentiles collapse to a single number -
    useless. Resampling produces a genuine distribution of alternate histories.
    """
    import random
    p = [float(t["pnl"]) for t in trades
         if t.get("status") == "closed" and t.get("pnl") is not None]
    if len(p) < 5:
        return {"runs": 0, "note": "need at least 5 closed trades"}
    rng = random.Random(seed)
    n = len(p)
    finals, dds = [], []
    curves = []
    for r in range(runs):
        if bootstrap:
            order = [p[rng.randrange(n)] for _ in range(n)]
        else:
            order = p[:]
            rng.shuffle(order)
        eq = peak = starting_equity
        mdd = 0.0
        curve = [starting_equity]
        for x in order:
            eq += x
            peak = max(peak, eq)
            mdd = max(mdd, (peak - eq) / peak * 100 if peak else 0)
            curve.append(round(eq, 2))
        finals.append(eq)
        dds.append(mdd)
        if r < 40:
            curves.append(curve)
    finals.sort(); dds.sort()

    def pct(arr, q):
        return arr[min(len(arr) - 1, max(0, int(len(arr) * q)))]

    return {
        "runs": runs,
        "curves": curves,
        "final_p5": round(pct(finals, 0.05), 2),
        "final_p50": round(pct(finals, 0.50), 2),
        "final_p95": round(pct(finals, 0.95), 2),
        "dd_p50": round(pct(dds, 0.50), 2),
        "dd_p95": round(pct(dds, 0.95), 2),
        "prob_profit": round(sum(1 for f in finals if f > starting_equity)
                             / len(finals) * 100, 1),
        "risk_of_ruin": round(sum(1 for d in dds if d >= 50) / len(dds) * 100, 2),
        "method": "bootstrap" if bootstrap else "shuffle",
    }



def risk_usd(t: dict) -> float:
    """Initial dollar risk of a trade: recorded if we have it, else inferred."""
    entry, stop = t.get("entry"), t.get("stop")
    if not entry or not stop or abs(float(entry) - float(stop)) <= 0:
        return 0.0
    recorded = abs(float(t.get("meta_risk_usd") or 0.0))
    if recorded:
        return recorded
    lots = float(t.get("lots") or 0)
    return abs(float(entry) - float(stop)) * lots * 1e5 if lots else 0.0


def r_multiple(t: dict) -> float | None:
    """Realised R for a closed trade, or None when risk is unknowable."""
    if t.get("status") != "closed":
        return None
    risk = risk_usd(t)
    if risk <= 0:
        return None
    return float(t.get("pnl") or 0.0) / risk


def blotter(trades: list[dict], limit: int = 500) -> list[dict]:
    """Per-trade rows for the dashboard trade log, newest first."""
    rows: list[dict] = []
    for t in trades:
        entry = float(t.get("entry") or 0.0)
        exitp = t.get("exit_price")
        closed_at, created_at = t.get("closed_at"), t.get("created_at")
        held = ((float(closed_at) - float(created_at)) / 3600.0
                if closed_at and created_at else None)
        r = r_multiple(t)
        rows.append({
            "id": t.get("id"),
            "ticket": t.get("ticket"),
            "opened": created_at,
            "closed": closed_at,
            "symbol": t.get("symbol"),
            "side": t.get("side"),
            "lots": float(t.get("lots") or 0.0),
            "entry": entry,
            "stop": float(t.get("stop") or 0.0),
            "take": float(t.get("take") or 0.0),
            "exit": float(exitp) if exitp is not None else None,
            "pnl": float(t.get("pnl") or 0.0) if t.get("status") == "closed" else None,
            "r": round(r, 3) if r is not None else None,
            "risk_usd": round(risk_usd(t), 2),
            "hold_hours": round(held, 2) if held is not None else None,
            "status": t.get("status"),
            "mode": t.get("mode"),
            "account": t.get("account"),
            "platform": t.get("platform"),
            "strategy": t.get("meta_strategy") or "unknown",
            "demo": bool(t.get("meta_demo")),
        })
    rows.sort(key=lambda r: (r["opened"] or 0), reverse=True)
    return rows[:limit]


def full_report(trades: list[dict], starting_equity: float = 10_000.0) -> dict:
    """Everything the dashboard needs in one payload."""
    m = compute_metrics(trades, starting_equity)
    return {
        "metrics": m.dict(),
        "equity": equity_series(trades, starting_equity),
        "distribution": r_distribution(trades),
        "by_symbol": breakdown(trades, "symbol"),
        "by_strategy": breakdown(trades, "meta_strategy"),
        "by_side": breakdown(trades, "side"),
        "daily": daily_pnl(trades),
        "hourly": hourly_profile(trades),
        "rolling": rolling_metric(trades),
        "blotter": blotter(trades),
    }
