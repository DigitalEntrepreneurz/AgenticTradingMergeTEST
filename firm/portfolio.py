"""Portfolio-level correlation risk.

Counting open positions is not the same as measuring exposure. Three "separate"
trades - long EURUSD, long GBPUSD, short USDCHF - can be one leveraged bet
against the dollar. The per-trade risk gate cannot see this, because it only
ever looks at one signal at a time.

This module measures what is actually at stake:

  * `correlation_matrix` - Pearson correlation of log returns between symbols,
    computed from the broker's own bars so it reflects the instruments you
    really trade, not a textbook table
  * `cluster` - greedy grouping of symbols whose pairwise correlation exceeds a
    threshold, so a portfolio can be described as N independent bets rather
    than N positions
  * `portfolio_heat` - total risk if every open stop is hit at once, both naive
    (sum of individual risk) and correlation-adjusted
  * `effective_bets` - the diversification ratio; 4 positions in one cluster is
    closer to 1 bet than 4

The correlation-adjusted figure is the honest one. For a portfolio of positions
with risk amounts r_i and correlation matrix C, treating each stop-out as a unit
move in the same direction:

    adjusted = sqrt( sum_i sum_j  s_i s_j r_i r_j C_ij )

where s_i is +1 for long and -1 for short, so a short in a positively
correlated pair correctly *reduces* concentration rather than adding to it.
When everything is perfectly correlated and aligned this collapses to the naive
sum; when positions genuinely offset, it shrinks.

Nothing here places or blocks a trade on its own - `RiskAgent.vet` consumes it.
"""
from __future__ import annotations

import math
from typing import Any, Iterable

# Pairwise |r| at or above this counts as "the same bet".
CLUSTER_THRESHOLD = 0.7
# Bars of history used for the correlation estimate.
LOOKBACK = 300
# Minimum overlapping observations before a correlation is trusted at all.
MIN_OBS = 60


def log_returns(closes: list[float]) -> list[float]:
    """Log returns. Zero/negative prices are skipped rather than crashing."""
    out: list[float] = []
    for a, b in zip(closes, closes[1:]):
        if a > 0 and b > 0:
            out.append(math.log(b / a))
        else:
            out.append(0.0)
    return out


def pearson(xs: list[float], ys: list[float]) -> float:
    """Pearson correlation, 0.0 when undefined (constant series, no overlap)."""
    n = min(len(xs), len(ys))
    if n < 2:
        return 0.0
    xs, ys = xs[-n:], ys[-n:]
    mx = sum(xs) / n
    my = sum(ys) / n
    sxy = sxx = syy = 0.0
    for x, y in zip(xs, ys):
        dx, dy = x - mx, y - my
        sxy += dx * dy
        sxx += dx * dx
        syy += dy * dy
    if sxx <= 0 or syy <= 0:
        return 0.0
    r = sxy / math.sqrt(sxx * syy)
    return max(-1.0, min(1.0, r))


def returns_by_symbol(broker: Any, symbols: Iterable[str], timeframe: str = "H1",
                      lookback: int = LOOKBACK) -> dict[str, list[float]]:
    """Log-return series per symbol, straight from the broker's bars."""
    out: dict[str, list[float]] = {}
    for sym in dict.fromkeys(symbols):
        try:
            bars = broker.bars(sym, timeframe, lookback)
        except Exception:
            continue
        if not bars or len(bars) < MIN_OBS:
            continue
        out[sym] = log_returns([float(b.close) for b in bars])
    return out


def correlation_matrix(rets: dict[str, list[float]]) -> dict[str, dict[str, float]]:
    """Full symmetric correlation matrix with 1.0 on the diagonal."""
    syms = sorted(rets)
    m: dict[str, dict[str, float]] = {a: {} for a in syms}
    for i, a in enumerate(syms):
        m[a][a] = 1.0
        for b in syms[i + 1:]:
            n = min(len(rets[a]), len(rets[b]))
            r = pearson(rets[a], rets[b]) if n >= MIN_OBS else 0.0
            m[a][b] = round(r, 4)
            m[b][a] = round(r, 4)
    return m


def cluster(matrix: dict[str, dict[str, float]],
            threshold: float = CLUSTER_THRESHOLD) -> list[list[str]]:
    """Group symbols into correlated clusters (union-find on |r| >= threshold)."""
    syms = sorted(matrix)
    parent = {s: s for s in syms}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i, a in enumerate(syms):
        for b in syms[i + 1:]:
            if abs(matrix.get(a, {}).get(b, 0.0)) >= threshold:
                union(a, b)

    groups: dict[str, list[str]] = {}
    for s in syms:
        groups.setdefault(find(s), []).append(s)
    return sorted((sorted(g) for g in groups.values()), key=lambda g: (-len(g), g[0]))


def _side_sign(side: str) -> int:
    return 1 if str(side).lower() == "buy" else -1


def portfolio_heat(positions: list[dict],
                   matrix: dict[str, dict[str, float]]) -> dict:
    """Total risk if every stop is hit, naive vs correlation-adjusted.

    `positions` need `symbol`, `side` and `risk_usd`.
    """
    rows = [p for p in positions if float(p.get("risk_usd") or 0) > 0]
    naive = sum(float(p["risk_usd"]) for p in rows)
    if not rows:
        return {"naive_usd": 0.0, "adjusted_usd": 0.0, "positions": 0,
                "diversification": 1.0, "effective_bets": 0.0}

    total = 0.0
    for p in rows:
        for q in rows:
            ri = float(p["risk_usd"]) * _side_sign(p.get("side", "buy"))
            rj = float(q["risk_usd"]) * _side_sign(q.get("side", "buy"))
            c = 1.0 if p is q else matrix.get(p["symbol"], {}).get(q["symbol"], 0.0)
            total += ri * rj * c
    adjusted = math.sqrt(max(0.0, total))

    # diversification ratio: 1.0 = no benefit (one big bet), lower = genuinely spread
    ratio = (adjusted / naive) if naive > 0 else 1.0
    # Effective independent bets: N when uncorrelated, ~1 when all the same.
    # A fully hedged book has no net exposure at all, so "bets" is 0, not N -
    # reporting N there would overstate the risk being carried.
    if adjusted <= 1e-9:
        eff = 0.0
    else:
        eff = min((naive / adjusted) ** 2, float(len(rows)))
    return {
        "naive_usd": round(naive, 2),
        "adjusted_usd": round(adjusted, 2),
        "positions": len(rows),
        "diversification": round(ratio, 4),
        "effective_bets": round(eff, 2),
    }


def concentration(positions: list[dict], matrix: dict[str, dict[str, float]],
                  threshold: float = CLUSTER_THRESHOLD) -> list[dict]:
    """Risk grouped by correlated cluster, largest exposure first."""
    if not positions:
        return []
    syms = sorted({p["symbol"] for p in positions})
    sub = {a: {b: matrix.get(a, {}).get(b, 0.0) for b in syms} for a in syms}
    for a in syms:
        sub[a][a] = 1.0
    out = []
    for group in cluster(sub, threshold):
        members = [p for p in positions if p["symbol"] in group]
        if not members:
            continue
        # net directional risk inside the cluster: opposing sides offset
        net = sum(float(p.get("risk_usd") or 0) * _side_sign(p.get("side", "buy"))
                  for p in members)
        gross = sum(float(p.get("risk_usd") or 0) for p in members)
        out.append({
            "symbols": group,
            "positions": len(members),
            "gross_usd": round(gross, 2),
            "net_usd": round(net, 2),
            "directional": abs(net) > gross * 0.6,
        })
    return sorted(out, key=lambda g: -abs(g["net_usd"]))


def assess(broker: Any, positions: list[dict], equity: float,
           candidate: dict | None = None, timeframe: str = "H1",
           threshold: float = CLUSTER_THRESHOLD,
           max_cluster_pct: float = 2.0) -> dict:
    """Full portfolio view, optionally including a proposed new position.

    `candidate` is a dict like {"symbol","side","risk_usd"}. When supplied the
    result reports what the portfolio would look like *after* the trade, which
    is what the risk gate needs to decide.
    """
    live = list(positions)
    if candidate:
        live = live + [candidate]

    symbols = [p["symbol"] for p in live]
    rets = returns_by_symbol(broker, symbols, timeframe)
    matrix = correlation_matrix(rets)
    heat = portfolio_heat(live, matrix)
    groups = concentration(live, matrix, threshold)

    worst = groups[0] if groups else None
    cluster_pct = (abs(worst["net_usd"]) / equity * 100) if worst and equity > 0 else 0.0
    breach = cluster_pct > max_cluster_pct and (worst or {}).get("positions", 0) > 1

    return {
        "matrix": matrix,
        "symbols": sorted(matrix),
        "clusters": [g for g in cluster(matrix, threshold)],
        "heat": heat,
        "concentration": groups,
        "worst_cluster": worst,
        "worst_cluster_pct": round(cluster_pct, 3),
        "max_cluster_pct": max_cluster_pct,
        "breach": bool(breach),
        "threshold": threshold,
        "equity": round(float(equity), 2),
        "note": ("Correlation is measured on the broker's own bars over "
                 f"{LOOKBACK} {timeframe} candles. Correlations move - a pair "
                 "that is independent today can converge in a crisis."),
    }
