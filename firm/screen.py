"""Rank bulk-test results against a survival brief, not a profit brief.

"Takes trades daily, consistent, does not blow up" is three separate
constraints, and the third is the one that kills accounts. A sweep sorted by
return puts the most over-fitted, most leveraged configuration on top - it is
the one that got luckiest. This module sorts by the opposite instinct.

Every gate below is a *disqualifier*, not a weighting. A strategy that fails
one is out, however good the rest look, because you cannot average your way out
of an account-ending drawdown.
"""

from __future__ import annotations

from typing import Any

# Trading days in a year, used to turn a backtest span into a daily rate.
TRADING_DAYS = 252

DEFAULTS = {
    "min_trades_per_day": 0.5,      # roughly one setup every other day
    "max_trades_per_day": 20.0,     # above this it is noise-trading / cost-bound
    "min_trades": 60,               # sample size before any claim means anything
    "min_profit_factor": 1.25,
    "min_expectancy_r": 0.05,
    "max_drawdown_r": 12.0,         # hard survival gate, in R
    "max_dd_vs_return": 0.5,        # maxDD must be < half the total return
    "min_win_rate": 0.0,
    "require_walk_forward": True,   # must hold up out of sample
    "max_consecutive_losses": 12,
}


def bars_per_day(timeframe: str) -> float:
    from .brokers.base import TF_MINUTES
    mins = TF_MINUTES.get(timeframe.upper(), 60)
    return (24 * 60) / mins * (5 / 7)      # forex: ~5 sessions per 7 days


def trades_per_day(metrics: dict, timeframe: str, bars_tested: int) -> float:
    """How often this configuration actually fires, per calendar trading day."""
    trades = float(metrics.get("trades") or 0)
    if trades <= 0 or bars_tested <= 0:
        return 0.0
    days = max(1.0, bars_tested / bars_per_day(timeframe))
    return trades / days


def consistency(equity_curve: list[float]) -> dict:
    """Is the return spread through the curve, or one lucky spike?

    A curve whose best single step is most of the total return is a lottery
    ticket, not an edge.
    """
    if not equity_curve or len(equity_curve) < 3:
        return {"largest_step_share": 1.0, "up_fraction": 0.0, "smooth": False}
    steps = [b - a for a, b in zip(equity_curve, equity_curve[1:])]
    total = equity_curve[-1] - equity_curve[0]
    gains = [s for s in steps if s > 0]
    biggest = max(gains) if gains else 0.0
    share = (biggest / total) if total > 0 else 1.0
    up = sum(1 for s in steps if s > 0) / max(1, len(steps))
    return {
        "largest_step_share": round(share, 3),
        "up_fraction": round(up, 3),
        "smooth": share < 0.25 and up > 0.35,
    }


def max_consecutive_losses(detail: list[dict]) -> int:
    worst = run = 0
    for t in detail or []:
        if float(t.get("r") or 0) < 0:
            run += 1
            worst = max(worst, run)
        else:
            run = 0
    return worst


def screen_one(result: Any, rules: dict | None = None,
               bars_tested: int = 2500) -> dict:
    """Grade a single LabResult (or its dict) against the survival brief."""
    r = rules or DEFAULTS
    d = result.dict() if hasattr(result, "dict") else dict(result)
    m = d.get("metrics") or {}
    tf = d.get("timeframe", "H1")

    tpd = trades_per_day(m, tf, bars_tested)
    cons = consistency(d.get("equity_curve") or [])
    wf = d.get("walk_forward") or {}
    dd = float(m.get("max_drawdown_r") or 0.0)
    total_r = float(m.get("total_r") or 0.0)
    trades = int(m.get("trades") or 0)

    fails: list[str] = []
    if trades < r["min_trades"]:
        fails.append(f"only {trades} trades (need {r['min_trades']})")
    if tpd < r["min_trades_per_day"]:
        fails.append(f"fires {tpd:.2f}x/day, below {r['min_trades_per_day']}")
    if tpd > r["max_trades_per_day"]:
        fails.append(f"fires {tpd:.1f}x/day - overtrading, costs will eat it")
    if float(m.get("profit_factor") or 0) < r["min_profit_factor"]:
        fails.append(f"PF {m.get('profit_factor', 0):.2f} < {r['min_profit_factor']}")
    if float(m.get("expectancy_r") or 0) < r["min_expectancy_r"]:
        fails.append(f"expectancy {m.get('expectancy_r', 0):.3f}R too thin")
    if dd > r["max_drawdown_r"]:
        fails.append(f"maxDD {dd:.1f}R exceeds the {r['max_drawdown_r']}R survival cap")
    if total_r > 0 and dd > total_r * r["max_dd_vs_return"]:
        fails.append(f"maxDD {dd:.1f}R is more than "
                     f"{r['max_dd_vs_return']:.0%} of the {total_r:.1f}R return")
    if r["require_walk_forward"] and wf.get("folds") and not wf.get("robust"):
        fails.append("failed walk-forward: does not hold out of sample")
    if not cons["smooth"]:
        fails.append(f"lumpy equity: {cons['largest_step_share']:.0%} of the return "
                     "came from one trade")
    mcl = max_consecutive_losses(d.get("detail") or [])
    if mcl and mcl > r["max_consecutive_losses"]:
        fails.append(f"{mcl} losses in a row - unholdable in practice")

    return {
        "strategy": d.get("strategy"), "symbol": d.get("symbol"), "timeframe": tf,
        "params": d.get("params") or {},
        "trades": trades, "trades_per_day": round(tpd, 2),
        "profit_factor": round(float(m.get("profit_factor") or 0), 3),
        "expectancy_r": round(float(m.get("expectancy_r") or 0), 4),
        "total_r": round(total_r, 2), "max_drawdown_r": round(dd, 2),
        "win_rate": round(float(m.get("win_rate") or 0), 3),
        "walk_forward_robust": bool(wf.get("robust")) if wf.get("folds") else None,
        "consistency": cons, "max_consecutive_losses": mcl,
        "score": float(d.get("score") or 0.0),
        "verdict": "DEPLOY" if not fails else "REJECT",
        "failures": fails,
        "survival_score": survival_score(m, tpd, cons, dd, total_r),
    }


def survival_score(metrics: dict, tpd: float, cons: dict,
                   dd: float, total_r: float) -> float:
    """Rank survivors by robustness, explicitly penalising drawdown depth.

    Deliberately NOT total return: the highest-returning configuration in a
    sweep is usually the most over-fitted one.
    """
    pf = float(metrics.get("profit_factor") or 0)
    exp = float(metrics.get("expectancy_r") or 0)
    n = float(metrics.get("trades") or 0)
    if n <= 0 or pf <= 0:
        return 0.0
    edge = min(pf, 3.0) / 3.0 * 0.30
    consistency_part = cons.get("up_fraction", 0) * 0.20
    sample = min(n / 200.0, 1.0) * 0.15
    smooth = (1.0 - min(cons.get("largest_step_share", 1.0), 1.0)) * 0.15
    pain = (1.0 - min(dd / 20.0, 1.0)) * 0.20
    rate = 1.0 if 0.5 <= tpd <= 8 else 0.4
    return round((edge + consistency_part + sample + smooth + pain) * rate, 4)


def screen(results: list[Any], rules: dict | None = None,
           bars_tested: int = 2500) -> dict:
    """Screen a whole sweep. Survivors first, ranked by survival score."""
    graded = [screen_one(r, rules, bars_tested) for r in results]
    keep = sorted([g for g in graded if g["verdict"] == "DEPLOY"],
                  key=lambda g: -g["survival_score"])
    drop = sorted([g for g in graded if g["verdict"] == "REJECT"],
                  key=lambda g: -g["score"])
    reasons: dict[str, int] = {}
    for g in drop:
        for f in g["failures"]:
            head = f.split(" (")[0].split(" -")[0][:48]
            reasons[head] = reasons.get(head, 0) + 1
    return {
        "tested": len(graded), "deploy": keep, "rejected": drop,
        "deploy_count": len(keep), "reject_count": len(drop),
        "common_failures": sorted(reasons.items(), key=lambda kv: -kv[1])[:10],
        "rules": rules or DEFAULTS,
    }
