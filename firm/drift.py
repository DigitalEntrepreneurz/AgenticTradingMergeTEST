"""Live-vs-backtest drift detection.

A strategy that passed walk-forward validation can still stop working. Regimes
change, spreads widen, a broker fills differently. This module answers one
question per strategy:

    Is live performance still consistent with what the backtest promised?

The comparison is deliberately conservative. Small live samples are noisy, and
crying "drift" after four bad trades is worse than useless - it trains you to
ignore the alarm. So:

  * the baseline is the backtest expectancy in R, which is unit-free and
    therefore comparable across symbols and position sizes
  * the live sample is every closed trade whose initial risk is known
  * significance is a Welch t-test (unequal variance), because the live sample
    is small and its variance has nothing to do with the backtest's
  * below MIN_TRADES we return INSUFFICIENT and say so, rather than guessing
  * a CUSUM tracks *cumulative* shortfall, which catches slow bleed that a
    t-test on the whole sample can miss

Statuses, worst first:

    BROKEN   live expectancy is significantly worse than backtest (p < .05)
             and the shortfall is material
    DRIFT    live is worse by more than DRIFT_R, but not yet significant
    WATCH    live is below baseline but within noise
    OK       live is at or above baseline
    INSUFFICIENT  fewer than MIN_TRADES closed trades with known risk
"""
from __future__ import annotations

import json
import math
from typing import Any

from .analytics import r_multiple

# A strategy needs at least this many closed live trades before we will judge it.
MIN_TRADES = 8
# Shortfall in R that counts as material rather than cosmetic.
DRIFT_R = 0.15
# Significance level for the Welch test.
ALPHA = 0.05


# ---------------------------------------------------------------- statistics
def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _var(xs: list[float]) -> float:
    """Sample variance (n-1). Zero for fewer than two points."""
    n = len(xs)
    if n < 2:
        return 0.0
    m = _mean(xs)
    return sum((x - m) ** 2 for x in xs) / (n - 1)


def _norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _t_sf(t: float, df: float) -> float:
    """One-sided survival function for Student's t.

    Uses the regularised incomplete beta function. Falls back to the normal
    approximation for large df, where the two agree to well past our needs.
    """
    if df <= 0:
        return 0.5
    if df > 300:
        return 1.0 - _norm_cdf(t)
    x = df / (df + t * t)
    p = 0.5 * _betainc(df / 2.0, 0.5, x)
    return p if t > 0 else 1.0 - p


def _betainc(a: float, b: float, x: float) -> float:
    """Regularised incomplete beta I_x(a,b) via continued fraction.

    The Lentz continued fraction only converges for x < (a+1)/(a+b+2); beyond
    that we evaluate the mirrored form and subtract. Without this switch the
    function silently returns garbage for large x.
    """
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    if x > (a + 1.0) / (a + b + 2.0):
        return 1.0 - _betainc(b, a, 1.0 - x)
    lbeta = math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)
    front = math.exp(math.log(x) * a + math.log(1 - x) * b - lbeta) / a
    # Lentz's algorithm
    f, c, d = 1.0, 1.0, 0.0
    for i in range(200):
        m = i // 2
        if i == 0:
            num = 1.0
        elif i % 2 == 0:
            num = (m * (b - m) * x) / ((a + 2 * m - 1) * (a + 2 * m))
        else:
            num = -((a + m) * (a + b + m) * x) / ((a + 2 * m) * (a + 2 * m + 1))
        d = 1.0 + num * d
        if abs(d) < 1e-30:
            d = 1e-30
        d = 1.0 / d
        c = 1.0 + num / c
        if abs(c) < 1e-30:
            c = 1e-30
        f *= c * d
        if abs(1.0 - c * d) < 1e-10:
            break
    return front * (f - 1.0)


def welch(live: list[float], base_mean: float, base_var: float,
          base_n: int) -> dict:
    """One-sided Welch t-test: is `live` worse than the baseline?

    The backtest is treated as a sample too, so its own dispersion counts
    against false alarms.
    """
    n = len(live)
    if n < 2:
        return {"t": 0.0, "p": 1.0, "df": 0.0}
    m1, v1 = _mean(live), _var(live)
    v2 = max(base_var, 1e-12)
    n2 = max(base_n, 2)
    se2 = v1 / n + v2 / n2
    if se2 <= 0:
        return {"t": 0.0, "p": 1.0, "df": 0.0}
    se = math.sqrt(se2)
    t = (m1 - base_mean) / se
    num = se2 ** 2
    den = (v1 / n) ** 2 / (n - 1) + (v2 / n2) ** 2 / (n2 - 1)
    df = num / den if den > 0 else float(n - 1)
    # one-sided: probability of a result this bad or worse
    p = _t_sf(-t, df)
    return {"t": round(t, 3), "p": round(p, 4), "df": round(df, 1)}


def cusum(live: list[float], target: float, k: float = 0.5) -> dict:
    """Downside CUSUM: cumulative shortfall against `target`.

    `k` is the slack in R that we forgive before accumulating. The peak of the
    negative sum is what matters; a strategy that bleeds slowly will drive it
    down long before the overall mean looks damning.
    """
    s = 0.0
    worst = 0.0
    at = 0
    series = []
    for i, r in enumerate(live):
        s = min(0.0, s + (r - target + k))
        series.append(round(s, 3))
        if s < worst:
            worst, at = s, i + 1
    return {"worst": round(worst, 3), "at_trade": at, "series": series}


# ---------------------------------------------------------------- baselines
def baselines(memory: Any) -> dict[str, dict]:
    """Backtest expectancy per `strategy@symbol`, newest record wins."""
    out: dict[str, dict] = {}
    for row in memory.q("SELECT * FROM backtests ORDER BY id ASC"):
        try:
            m = json.loads(row.get("metrics") or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        strat = m.get("strategy") or row.get("strategy") or ""
        # backtests are keyed "name@SYMBOL" sometimes, plain name others
        strat = str(strat).split("@")[0]
        sym = row.get("symbol") or m.get("symbol") or ""
        if not strat or not sym:
            continue
        exp_r = m.get("expectancy_r")
        if exp_r is None:
            continue
        n = int(m.get("trades") or 0)
        total_r = float(m.get("total_r") or 0.0)
        # Reconstruct a plausible variance from the R distribution when the
        # backtest did not store one: R multiples of a fixed-R system cluster
        # near -1 and +rr, so a unit-ish variance is a fair, conservative prior.
        var = m.get("r_variance")
        if var is None:
            var = 1.0
        out[f"{strat}@{sym}"] = {
            "strategy": strat, "symbol": sym,
            "expectancy_r": float(exp_r), "trades": n,
            "total_r": total_r, "variance": float(var),
            "profit_factor": m.get("profit_factor"),
            "win_rate": m.get("win_rate"),
            "recorded_at": row.get("created_at"),
        }
    return out


def live_samples(memory: Any, include_demo: bool = False) -> dict[str, list[dict]]:
    """Closed live trades grouped by `strategy@symbol`, oldest first."""
    groups: dict[str, list[dict]] = {}
    for t in memory.all_trades():
        if t.get("status") != "closed":
            continue
        if not include_demo and t.get("meta_demo"):
            continue
        r = r_multiple(t)
        if r is None:
            continue
        strat = str(t.get("meta_strategy") or "unknown").split("@")[0]
        sym = t.get("symbol") or ""
        if not sym:
            continue
        groups.setdefault(f"{strat}@{sym}", []).append({
            "r": r, "pnl": float(t.get("pnl") or 0.0),
            "opened": t.get("created_at"), "closed": t.get("closed_at"),
            "ticket": t.get("ticket"), "demo": bool(t.get("meta_demo")),
        })
    for k in groups:
        groups[k].sort(key=lambda x: x["opened"] or 0)
    return groups


# ---------------------------------------------------------------- verdict
def classify(live_r: list[float], base: dict) -> dict:
    """Grade one strategy's live sample against its backtest baseline."""
    n = len(live_r)
    base_exp = float(base.get("expectancy_r") or 0.0)
    if n < MIN_TRADES:
        return {
            "status": "INSUFFICIENT",
            "reason": f"only {n} closed live trades; need {MIN_TRADES} "
                      "before a verdict means anything",
            "live_expectancy_r": round(_mean(live_r), 4) if live_r else None,
            "baseline_expectancy_r": round(base_exp, 4),
            "delta_r": round(_mean(live_r) - base_exp, 4) if live_r else None,
            "trades": n, "p_value": None, "significant": False,
            "cusum": cusum(live_r, base_exp) if live_r else
                     {"worst": 0.0, "at_trade": 0, "series": []},
        }

    live_exp = _mean(live_r)
    delta = live_exp - base_exp
    w = welch(live_r, base_exp, float(base.get("variance") or 1.0),
              int(base.get("trades") or 30))
    cu = cusum(live_r, base_exp)
    sig = w["p"] < ALPHA

    if delta >= 0:
        status = "OK"
        reason = (f"live expectancy {live_exp:+.3f}R is at or above the "
                  f"backtest's {base_exp:+.3f}R")
    elif sig and abs(delta) >= DRIFT_R:
        status = "BROKEN"
        reason = (f"live {live_exp:+.3f}R vs backtest {base_exp:+.3f}R "
                  f"({delta:+.3f}R), p={w['p']:.3f} over {n} trades - the gap "
                  "is larger than noise explains")
    elif abs(delta) >= DRIFT_R:
        status = "DRIFT"
        reason = (f"live {live_exp:+.3f}R is {abs(delta):.3f}R below backtest, "
                  f"but p={w['p']:.3f} - suggestive, not yet conclusive")
    else:
        status = "WATCH"
        reason = (f"live {live_exp:+.3f}R trails backtest by {abs(delta):.3f}R, "
                  "inside normal variation")

    # a deep CUSUM trough escalates a quiet WATCH
    if status == "WATCH" and cu["worst"] <= -2.0:
        status = "DRIFT"
        reason += f"; cumulative shortfall reached {cu['worst']}R"

    return {
        "status": status, "reason": reason,
        "live_expectancy_r": round(live_exp, 4),
        "baseline_expectancy_r": round(base_exp, 4),
        "delta_r": round(delta, 4),
        "trades": n,
        "live_total_r": round(sum(live_r), 3),
        "p_value": w["p"], "t": w["t"], "df": w["df"],
        "significant": sig,
        "cusum": cu,
    }


def report(memory: Any, include_demo: bool = False) -> dict:
    """Full drift report across every strategy with both a baseline and fills."""
    base = baselines(memory)
    live = live_samples(memory, include_demo=include_demo)

    rows = []
    for key, b in sorted(base.items()):
        sample = live.get(key, [])
        rs = [s["r"] for s in sample]
        v = classify(rs, b)
        v.update({
            "key": key, "strategy": b["strategy"], "symbol": b["symbol"],
            "baseline_trades": b["trades"],
            "baseline_profit_factor": b.get("profit_factor"),
            "r_series": [round(x, 3) for x in rs][-60:],
        })
        rows.append(v)

    # live strategies with no backtest baseline are their own kind of risk
    unmatched = []
    for key, sample in sorted(live.items()):
        if key in base:
            continue
        rs = [s["r"] for s in sample]
        unmatched.append({
            "key": key, "strategy": key.split("@")[0], "symbol": key.split("@")[-1],
            "trades": len(rs),
            "live_expectancy_r": round(_mean(rs), 4) if rs else None,
            "live_total_r": round(sum(rs), 3),
            "status": "NO_BASELINE",
            "reason": "trading live with no recorded backtest to compare against",
        })

    order = {"BROKEN": 0, "DRIFT": 1, "WATCH": 2, "OK": 3, "INSUFFICIENT": 4}
    rows.sort(key=lambda r: (order.get(r["status"], 9), r.get("delta_r") or 0))

    tally: dict[str, int] = {}
    for r in rows:
        tally[r["status"]] = tally.get(r["status"], 0) + 1
    for u in unmatched:
        tally["NO_BASELINE"] = tally.get("NO_BASELINE", 0) + 1

    return {
        "rows": rows,
        "unmatched": unmatched,
        "tally": tally,
        "checked": len(rows),
        "min_trades": MIN_TRADES,
        "drift_r": DRIFT_R,
        "alpha": ALPHA,
        "include_demo": include_demo,
        "note": ("Baselines are in-sample backtest expectancy. A live sample "
                 "below it is not automatically broken - check the trade count "
                 "before acting."),
    }
