"""Strategy Lab - fast multi-strategy testing, optimization and export.

Runs the whole strategy library across symbols and timeframes in parallel,
grid- or random-searches parameters, validates with walk-forward, ranks by a
robustness score, and exports winners as MQL4/MQL5 Expert Advisors or as a
JSON strategy pack for a web product.

    from firm.lab import Lab
    lab = Lab(broker)
    res = lab.sweep(symbols=["EURUSD"], timeframes=["H1"])
    lab.export_ea(res[0], "MyEA")
"""
from __future__ import annotations

import itertools
import json
import math
import random
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from .backtester import BTResult, backtest, backtest_precomputed, walk_forward
from .brokers.base import Bar, SymbolSpec
from .strategies.library import all_strategies

ROOT = Path(__file__).resolve().parent.parent
EXPORT_DIR = ROOT / "exports"

# Parameter search spaces per strategy
SEARCH_SPACE: dict[str, dict[str, list]] = {
    "ema_trend_pullback": {
        "fast": [8, 13, 21, 34], "slow": [34, 55, 89],
        "atr_mult": [1.5, 2.0, 2.5, 3.0], "rr": [1.5, 2.0, 2.5, 3.0],
        "pullback_atr": [0.5, 0.8, 1.2],
    },
    "donchian_breakout": {
        "channel": [10, 15, 20, 30, 40], "atr_mult": [1.5, 2.0, 2.5, 3.0],
        "rr": [2.0, 2.5, 3.0, 4.0], "buffer_atr": [0.0, 0.05, 0.15],
    },
    "bollinger_reversion": {
        "bb_n": [14, 20, 30], "bb_mult": [1.8, 2.0, 2.2, 2.5],
        "rsi_lo": [20, 25, 30], "rsi_hi": [70, 75, 80],
        "rr": [1.0, 1.5, 2.0], "atr_mult": [1.2, 1.6, 2.0],
    },
    "macd_momentum": {
        "fast": [8, 12], "slow": [21, 26, 34], "signal": [7, 9],
        "atr_mult": [1.5, 2.0, 2.5], "rr": [1.5, 2.0, 2.5, 3.0],
    },
}


@dataclass
class LabResult:
    strategy: str
    symbol: str
    timeframe: str
    params: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)
    walk_forward: dict = field(default_factory=dict)
    score: float = 0.0
    passed: bool = False
    reason: str = ""
    equity_curve: list[float] = field(default_factory=list)
    tested: int = 0
    elapsed: float = 0.0

    def dict(self) -> dict:
        return asdict(self)


def robustness_score(r: BTResult, wf: dict | None = None) -> float:
    """One number balancing edge, sample size, consistency and drawdown.

    Rewards: expectancy, trade count (sqrt), profit factor, out-of-sample survival.
    Punishes: deep drawdown relative to return, tiny samples.
    """
    if r.trades < 5:
        return 0.0
    edge = r.expectancy_r
    if edge <= 0:
        return round(edge * math.sqrt(r.trades), 4)
    sample = math.sqrt(min(r.trades, 200) / 200)
    pf_term = min(r.profit_factor, 4.0) / 4.0
    dd_term = 1.0 / (1.0 + max(r.max_drawdown_r, 0.0) / max(abs(r.total_r), 1.0))
    consistency = min(max(r.sharpe, 0.0), 4.0) / 4.0
    base = edge * (0.9 + sample) * (0.6 + 0.4 * pf_term) * (0.5 + 0.5 * dd_term)
    base *= (0.75 + 0.25 * consistency)
    if wf and wf.get("folds"):
        oos = wf.get("oos_expectancy_r", 0.0)
        if wf.get("robust"):
            base *= 1.35
        elif oos <= 0:
            base *= 0.35          # in-sample only: likely curve fit
    return round(base, 4)


def _combos(space: dict[str, list], max_n: int, seed: int = 7) -> list[dict]:
    keys = list(space)
    total = 1
    for k in keys:
        total *= len(space[k])
    if total <= max_n:
        return [dict(zip(keys, vals)) for vals in itertools.product(*(space[k] for k in keys))]
    rng = random.Random(seed)
    seen, out = set(), []
    while len(out) < max_n and len(seen) < total:
        cand = {k: rng.choice(space[k]) for k in keys}
        sig = json.dumps(cand, sort_keys=True)
        if sig in seen:
            continue
        seen.add(sig)
        out.append(cand)
    return out


def _bars_payload(bars: list[Bar]) -> list[tuple]:
    return [(b.time, b.open, b.high, b.low, b.close, b.volume) for b in bars]


def _bars_restore(payload: list[tuple]) -> list[Bar]:
    return [Bar(time=t, open=o, high=h, low=l, close=c, volume=v)
            for t, o, h, l, c, v in payload]


def _worker(job: dict) -> dict:
    """Runs in a separate process: test one strategy/params combo."""
    bars = _bars_restore(job["bars"])
    spec = SymbolSpec(**job["spec"]) if job.get("spec") else None
    if job.get("composite"):
        r = backtest_spec(job["composite"], job["params"], bars, job["symbol"],
                          job["timeframe"], spec, job["spread_pts"])
    else:
        r = backtest(job["strategy"], bars, job["symbol"], job["timeframe"],
                     job["params"], spec, spread_pts=job["spread_pts"])
    return {"params": job["params"], "summary": r.summary(),
            "score_raw": robustness_score(r), "curve": r.equity_curve[-120:]}


def backtest_spec(spec: dict, params: dict, bars: list[Bar], symbol: str,
                  timeframe: str, sym_spec: SymbolSpec | None,
                  spread_pts: float) -> BTResult:
    """Backtest a declarative (ingested) strategy spec."""
    from .strategies.composite import apply_params, evaluate_spec
    applied = apply_params(spec, params or {})
    signals = evaluate_spec(applied, bars)
    return backtest_precomputed(signals, bars, symbol, timeframe, sym_spec,
                                spread_pts, name=spec.get("name", "composite"))


class Lab:
    """Fast, parallel strategy research."""

    def __init__(self, broker=None, workers: int = 0, cache_dir: Path | None = None):
        self.broker = broker
        self.workers = workers or 0
        self.cache: dict[str, list[Bar]] = {}
        self.cache_dir = cache_dir

    # ---------------- data ----------------
    def bars(self, symbol: str, timeframe: str, count: int = 2500) -> list[Bar]:
        key = f"{symbol}:{timeframe}:{count}"
        if key in self.cache:
            return self.cache[key]
        if not self.broker:
            raise RuntimeError("Lab needs a broker to fetch history")
        b = self.broker.bars(symbol, timeframe, count)
        self.cache[key] = b
        return b

    def _spread(self, symbol: str, spec: SymbolSpec) -> float:
        try:
            t = self.broker.tick(symbol)
            return max(1.0, (t.ask - t.bid) / spec.point)
        except Exception:
            return 10.0

    # ---------------- single test ----------------
    def test(self, strategy: str, symbol: str, timeframe: str,
             params: dict | None = None, bars_count: int = 2500) -> LabResult:
        t0 = time.time()
        bars = self.bars(symbol, timeframe, bars_count)
        spec = self.broker.symbol_spec(symbol)
        sp = self._spread(symbol, spec)
        r = backtest(strategy, bars, symbol, timeframe, params, spec, spread_pts=sp)
        wf = walk_forward(strategy, bars, symbol, timeframe,
                          SEARCH_SPACE.get(strategy, {"rr": [2.0]}), folds=3,
                          spec=spec, spread_pts=sp)
        ok, reason = r.verdict()
        return LabResult(strategy=strategy, symbol=symbol, timeframe=timeframe,
                         params=params or {}, metrics=r.summary(), walk_forward=wf,
                         score=robustness_score(r, wf), passed=ok and
                         (wf.get("robust", True) if wf.get("folds") else True),
                         reason=reason, equity_curve=r.equity_curve[-200:],
                         tested=1, elapsed=round(time.time() - t0, 2))

    # ---------------- optimization ----------------
    def optimize(self, strategy: str, symbol: str, timeframe: str,
                 max_combos: int = 120, bars_count: int = 2500,
                 progress: Callable[[int, int], None] | None = None) -> LabResult:
        """Search the parameter space, then walk-forward validate the winner."""
        t0 = time.time()
        bars = self.bars(symbol, timeframe, bars_count)
        spec = self.broker.symbol_spec(symbol)
        sp = self._spread(symbol, spec)
        space = SEARCH_SPACE.get(strategy, {})
        combos = _combos(space, max_combos) if space else [{}]

        payload = _bars_payload(bars)
        spec_d = asdict(spec)
        jobs = [{"strategy": strategy, "symbol": symbol, "timeframe": timeframe,
                 "params": c, "bars": payload, "spec": spec_d, "spread_pts": sp}
                for c in combos]

        results: list[dict] = []
        if self.workers and len(jobs) > 8:
            try:
                with ProcessPoolExecutor(max_workers=self.workers) as ex:
                    futs = [ex.submit(_worker, j) for j in jobs]
                    for n, f in enumerate(as_completed(futs), 1):
                        results.append(f.result())
                        if progress:
                            progress(n, len(jobs))
            except Exception:
                results = []
        if not results:
            for n, j in enumerate(jobs, 1):
                results.append(_worker(j))
                if progress:
                    progress(n, len(jobs))

        results = [r for r in results if r["summary"]["trades"] > 0]
        if not results:
            return LabResult(strategy=strategy, symbol=symbol, timeframe=timeframe,
                             reason="no parameter set produced a trade",
                             tested=len(jobs), elapsed=round(time.time() - t0, 2))

        results.sort(key=lambda r: r["score_raw"], reverse=True)
        best = results[0]

        wf = walk_forward(strategy, bars, symbol, timeframe, space or {"rr": [2.0]},
                          folds=3, spec=spec, spread_pts=sp)
        r_final = backtest(strategy, bars, symbol, timeframe, best["params"], spec,
                           spread_pts=sp)
        ok, reason = r_final.verdict()
        robust = wf.get("robust", True) if wf.get("folds") else True
        return LabResult(
            strategy=strategy, symbol=symbol, timeframe=timeframe,
            params=best["params"], metrics=r_final.summary(), walk_forward=wf,
            score=robustness_score(r_final, wf), passed=bool(ok and robust),
            reason=reason if ok else reason,
            equity_curve=r_final.equity_curve[-200:], tested=len(jobs),
            elapsed=round(time.time() - t0, 2))

    # ---------------- sweep ----------------
    def sweep(self, symbols: list[str], timeframes: list[str],
              strategies: list[str] | None = None, max_combos: int = 60,
              bars_count: int = 2500, optimize_each: bool = True,
              progress: Callable[[dict], None] | None = None) -> list[LabResult]:
        """Every strategy x symbol x timeframe, ranked by robustness score."""
        strategies = strategies or list(all_strategies())
        out: list[LabResult] = []
        total = len(strategies) * len(symbols) * len(timeframes)
        done = 0
        for tf in timeframes:
            for sym in symbols:
                for st in strategies:
                    try:
                        res = (self.optimize(st, sym, tf, max_combos, bars_count)
                               if optimize_each else
                               self.test(st, sym, tf, bars_count=bars_count))
                    except Exception as e:
                        res = LabResult(strategy=st, symbol=sym, timeframe=tf,
                                        reason=f"error: {type(e).__name__}: {e}")
                    out.append(res)
                    done += 1
                    if progress:
                        progress({"done": done, "total": total, "current":
                                  f"{st} {sym} {tf}", "result": res.dict()})
        out.sort(key=lambda r: r.score, reverse=True)
        return out

    # ---------------- ingested specs ----------------
    def optimize_spec(self, spec: dict, symbol: str, timeframe: str,
                      max_combos: int = 60, bars_count: int = 2500,
                      progress: Callable[[int, int], None] | None = None) -> LabResult:
        """Optimize a declarative strategy spec (e.g. extracted from a video)."""
        from .strategies.composite import apply_params, evaluate_spec, spec_search_space
        t0 = time.time()
        bars = self.bars(symbol, timeframe, bars_count)
        sym_spec = self.broker.symbol_spec(symbol)
        sp = self._spread(symbol, sym_spec)
        combos = _combos(spec_search_space(spec), max_combos)

        scored: list[tuple[dict, Any]] = []
        for n, params in enumerate(combos, 1):
            r = backtest_spec(spec, params, bars, symbol, timeframe, sym_spec, sp)
            scored.append((params, r))
            if progress:
                progress(n, len(combos))
        scored = [(p, r) for p, r in scored if r.trades > 0]
        if not scored:
            return LabResult(strategy=spec.get("name", "ingested"), symbol=symbol,
                             timeframe=timeframe,
                             reason="no parameter set produced a trade",
                             tested=len(combos), elapsed=round(time.time() - t0, 2))
        scored.sort(key=lambda t: robustness_score(t[1]), reverse=True)
        best_params, best = scored[0]

        # walk-forward on the spec: optimise in-sample, verify out-of-sample
        n_bars = len(bars)
        folds, oos_r, oos_n = 3, 0.0, 0
        seg = n_bars // (folds + 1)
        fold_params = []
        if seg >= 200:
            for f in range(folds):
                tr, te = bars[: seg * (f + 1)], bars[seg * (f + 1): seg * (f + 2)]
                if len(te) < 150:
                    continue
                cand = [(p, backtest_spec(spec, p, tr, symbol, timeframe, sym_spec, sp))
                        for p in combos[:max(6, len(combos) // 4)]]
                cand = [(p, r) for p, r in cand if r.trades > 0]
                if not cand:
                    continue
                cand.sort(key=lambda t: robustness_score(t[1]), reverse=True)
                fp = cand[0][0]
                fold_params.append(fp)
                rr = backtest_spec(spec, fp, te, symbol, timeframe, sym_spec, sp)
                oos_r += rr.total_r
                oos_n += rr.trades
        wf = {"folds": len(fold_params), "oos_total_r": round(oos_r, 3),
              "oos_trades": oos_n,
              "oos_expectancy_r": round(oos_r / oos_n, 4) if oos_n else 0.0,
              "params_per_fold": fold_params,
              "robust": oos_n >= 10 and oos_r > 0}
        ok, reason = best.verdict()
        return LabResult(strategy=spec.get("name", "ingested"), symbol=symbol,
                         timeframe=timeframe, params=best_params,
                         metrics=best.summary(), walk_forward=wf,
                         score=robustness_score(best, wf),
                         passed=bool(ok and (wf["robust"] if wf["folds"] else True)),
                         reason=reason, equity_curve=best.equity_curve[-200:],
                         tested=len(combos), elapsed=round(time.time() - t0, 2))

    # ---------------- export ----------------
    def export_indicator(self, res: LabResult, name: str | None = None,
                         platform: str = "both",
                         spec: dict | None = None) -> dict[str, str]:
        """Emit a chart indicator that plots this strategy's signals.

        Needs a compilable rule spec - a built-in strategy has no spec to
        translate, so this returns {} rather than emitting something wrong.
        """
        from .mql import can_compile, render_spec_indicator
        if not (spec and can_compile(spec)):
            return {}
        EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        name = (name or f"{res.strategy}_{res.symbol}_{res.timeframe}_Signals")
        name = name.replace("-", "_")
        written = {}
        for mql, key in ((5, "mq5"), (4, "mq4")):
            if platform in ("both", f"mt{mql}"):
                p = EXPORT_DIR / f"{name}.mq{mql}"
                p.write_text(render_spec_indicator(
                    spec, name, mql, params=res.params, metrics=res.metrics,
                    walk_forward=res.walk_forward, score=res.score,
                    symbol=res.symbol, timeframe=res.timeframe))
                written[key] = str(p)
        return written

    def export_ea(self, res: LabResult, name: str | None = None,
                  platform: str = "both", spec: dict | None = None) -> dict[str, str]:
        """Emit a standalone Expert Advisor implementing this exact strategy.

        Pass `spec` for an ingested/auto-scanned composite strategy and the EA
        is compiled from those rules; otherwise a built-in template is used.
        """
        from .mql import can_compile, render_spec_ea
        EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        name = (name or f"{res.strategy}_{res.symbol}_{res.timeframe}").replace("-", "_")
        use_spec = bool(spec and can_compile(spec))

        def src(mql: int) -> str:
            if use_spec:
                return render_spec_ea(spec, name, mql, params=res.params,
                                      metrics=res.metrics,
                                      walk_forward=res.walk_forward,
                                      score=res.score, symbol=res.symbol,
                                      timeframe=res.timeframe)
            return render_ea(res, name, mql)

        written = {}
        for mql, key in ((5, "mq5"), (4, "mq4")):
            if platform in ("both", f"mt{mql}"):
                p = EXPORT_DIR / f"{name}.mq{mql}"
                p.write_text(src(mql))
                written[key] = str(p)
        return written

    def export_pack(self, results: list[LabResult], name: str = "strategy_pack") -> str:
        """JSON pack for a web/SaaS product: params + verified metrics."""
        EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "generated_at": time.time(),
            "generated_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "count": len(results),
            "strategies": [{
                "id": f"{r.strategy}.{r.symbol}.{r.timeframe}",
                "strategy": r.strategy, "symbol": r.symbol, "timeframe": r.timeframe,
                "params": r.params, "score": r.score, "passed": r.passed,
                "metrics": r.metrics, "walk_forward": r.walk_forward,
            } for r in results],
        }
        p = EXPORT_DIR / f"{name}.json"
        p.write_text(json.dumps(payload, indent=2))
        return str(p)


# ----------------------------------------------------------------------
# MQL code generation
# ----------------------------------------------------------------------
def _p(res: LabResult, key: str, default):
    lib = all_strategies().get(res.strategy, {})
    d = dict(lib.get("default_params", {}))
    d.update(res.params or {})
    return d.get(key, default)


def _entry_logic(res: LabResult, v: int) -> str:
    """Emit the strategy's entry condition in MQL."""
    s = res.strategy
    if s == "ema_trend_pullback":
        return f"""
   double fastMA = iMA(_Symbol,PERIOD_CURRENT,{int(_p(res,'fast',21))},0,MODE_EMA,PRICE_CLOSE{',1' if v==4 else ''});
   double slowMA = iMA(_Symbol,PERIOD_CURRENT,{int(_p(res,'slow',55))},0,MODE_EMA,PRICE_CLOSE{',1' if v==4 else ''});
   double slowPrev = {'iMA(_Symbol,PERIOD_CURRENT,'+str(int(_p(res,'slow',55)))+',0,MODE_EMA,PRICE_CLOSE,9)' if v==4 else 'SlowPrev()'};
   double dist = MathAbs(price - fastMA) / atr;
   bool trendUp = fastMA > slowMA && (slowMA - slowPrev)/slowPrev > {float(_p(res,'min_slope',0.0004))};
   bool trendDn = fastMA < slowMA && (slowPrev - slowMA)/slowPrev > {float(_p(res,'min_slope',0.0004))};
   if(dist > {float(_p(res,'pullback_atr',0.8))}) return;
   buy  = trendUp;
   sell = trendDn;"""
    if s == "donchian_breakout":
        n = int(_p(res, "channel", 20))
        return f"""
   double hh = 0, ll = 0;
   {'hh = High[iHighest(NULL,0,MODE_HIGH,'+str(n)+',2)]; ll = Low[iLowest(NULL,0,MODE_LOW,'+str(n)+',2)];' if v==4 else 'ChannelHL('+str(n)+', hh, ll);'}
   double buf = atr * {float(_p(res,'buffer_atr',0.05))};
   buy  = price > hh + buf;
   sell = price < ll - buf;"""
    if s == "bollinger_reversion":
        n = int(_p(res, "bb_n", 20)); m = float(_p(res, "bb_mult", 2.2))
        rl = float(_p(res, "rsi_lo", 28)); rh = float(_p(res, "rsi_hi", 72))
        return f"""
   double upper = iBands(_Symbol,PERIOD_CURRENT,{n},{m},0,PRICE_CLOSE{',MODE_UPPER,1' if v==4 else ''}{'' if v==4 else ''});
   double lower = {'iBands(_Symbol,PERIOD_CURRENT,'+str(n)+','+str(m)+',0,PRICE_CLOSE,MODE_LOWER,1)' if v==4 else 'BandLower()'};
   double rsiV  = iRSI(_Symbol,PERIOD_CURRENT,{int(_p(res,'rsi_n',14))},PRICE_CLOSE{',1' if v==4 else ''});
   buy  = price < lower && rsiV < {rl};
   sell = price > upper && rsiV > {rh};"""
    # macd_momentum
    f_, sl_, sg_ = (int(_p(res, "fast", 12)), int(_p(res, "slow", 26)),
                    int(_p(res, "signal", 9)))
    return f"""
   double macdMain = iMACD(_Symbol,PERIOD_CURRENT,{f_},{sl_},{sg_},PRICE_CLOSE{',MODE_MAIN,1' if v==4 else ''});
   double macdSig  = {'iMACD(_Symbol,PERIOD_CURRENT,'+f"{f_},{sl_},{sg_}"+',PRICE_CLOSE,MODE_SIGNAL,1)' if v==4 else 'MacdSignal()'};
   double macdMain2= {'iMACD(_Symbol,PERIOD_CURRENT,'+f"{f_},{sl_},{sg_}"+',PRICE_CLOSE,MODE_MAIN,2)' if v==4 else 'MacdMainPrev()'};
   double macdSig2 = {'iMACD(_Symbol,PERIOD_CURRENT,'+f"{f_},{sl_},{sg_}"+',PRICE_CLOSE,MODE_SIGNAL,2)' if v==4 else 'MacdSignalPrev()'};
   double bias = iMA(_Symbol,PERIOD_CURRENT,{int(_p(res,'bias',100))},0,MODE_EMA,PRICE_CLOSE{',1' if v==4 else ''});
   buy  = macdMain2 <= macdSig2 && macdMain > macdSig && price > bias;
   sell = macdMain2 >= macdSig2 && macdMain < macdSig && price < bias;"""


def render_ea(res: LabResult, name: str, mql: int = 5) -> str:
    """Generate a complete, compilable Expert Advisor."""
    m = res.metrics or {}
    atr_n = int(_p(res, "atr_n", 14))
    atr_mult = float(_p(res, "atr_mult", 2.0))
    rr = float(_p(res, "rr", 2.0))
    header = f"""//+------------------------------------------------------------------+
//| {name}.mq{mql}
//| Generated by the Agentic Trading Firm - Strategy Lab
//| {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}
//|
//| Strategy   : {res.strategy}
//| Symbol/TF  : {res.symbol} {res.timeframe}
//| Parameters : {json.dumps(res.params)}
//|
//| BACKTEST (in-sample, spread-adjusted)
//|   trades {m.get('trades', 0)} | win {m.get('win_rate', 0)}% | PF {m.get('profit_factor', 0)}
//|   expectancy {m.get('expectancy_r', 0)}R | maxDD {m.get('max_drawdown_r', 0)}R
//|   sharpe {m.get('sharpe', 0)} | robustness score {res.score}
//| WALK-FORWARD: {json.dumps(res.walk_forward)[:150]}
//|
//| Past performance does not predict future results. Test on demo first.
//+------------------------------------------------------------------+
#property copyright "Agentic Trading Firm"
#property version   "1.00"
#property strict

input double RiskPercent   = 0.75;    // % equity risked per trade
input double ATRMultiplier = {atr_mult};   // stop distance
input double RewardRatio   = {rr};   // target = R x stop
input int    ATRPeriod     = {atr_n};
input int    MagicNumber   = {abs(hash(name)) % 900000 + 100000};
input int    Slippage      = 20;
input int    MaxPositions  = 1;
input bool   TradeOnNewBarOnly = true;
"""

    if mql == 5:
        return header + f"""
#include <Trade/Trade.mqh>
CTrade trade;
int atrHandle, fastHandle, slowHandle, rsiHandle, macdHandle, bandsHandle, biasHandle;
datetime lastBar = 0;

int OnInit()
  {{
   trade.SetExpertMagicNumber(MagicNumber);
   trade.SetDeviationInPoints(Slippage);
   atrHandle   = iATR(_Symbol, PERIOD_CURRENT, ATRPeriod);
   fastHandle  = iMA(_Symbol, PERIOD_CURRENT, {int(_p(res,'fast',21))}, 0, MODE_EMA, PRICE_CLOSE);
   slowHandle  = iMA(_Symbol, PERIOD_CURRENT, {int(_p(res,'slow',55))}, 0, MODE_EMA, PRICE_CLOSE);
   biasHandle  = iMA(_Symbol, PERIOD_CURRENT, {int(_p(res,'bias',100))}, 0, MODE_EMA, PRICE_CLOSE);
   rsiHandle   = iRSI(_Symbol, PERIOD_CURRENT, {int(_p(res,'rsi_n',14))}, PRICE_CLOSE);
   macdHandle  = iMACD(_Symbol, PERIOD_CURRENT, {int(_p(res,'fast',12))}, {int(_p(res,'slow',26))}, {int(_p(res,'signal',9))}, PRICE_CLOSE);
   bandsHandle = iBands(_Symbol, PERIOD_CURRENT, {int(_p(res,'bb_n',20))}, 0, {float(_p(res,'bb_mult',2.2))}, PRICE_CLOSE);
   if(atrHandle == INVALID_HANDLE) return INIT_FAILED;
   Print("{name} initialised. Risk ", RiskPercent, "% | ATRx", ATRMultiplier, " | RR ", RewardRatio);
   return INIT_SUCCEEDED;
  }}

double Buf(int handle, int buffer, int shift)
  {{
   double v[];
   if(CopyBuffer(handle, buffer, shift, 1, v) < 1) return 0.0;
   return v[0];
  }}
double SlowPrev()      {{ return Buf(slowHandle, 0, 9); }}
double BandLower()     {{ return Buf(bandsHandle, 2, 1); }}
double MacdSignal()    {{ return Buf(macdHandle, 1, 1); }}
double MacdMainPrev()  {{ return Buf(macdHandle, 0, 2); }}
double MacdSignalPrev(){{ return Buf(macdHandle, 1, 2); }}

void ChannelHL(int n, double &hh, double &ll)
  {{
   double h[], l[];
   if(CopyHigh(_Symbol, PERIOD_CURRENT, 2, n, h) < n) {{ hh = 0; ll = 0; return; }}
   if(CopyLow(_Symbol, PERIOD_CURRENT, 2, n, l) < n)  {{ hh = 0; ll = 0; return; }}
   hh = h[ArrayMaximum(h)];
   ll = l[ArrayMinimum(l)];
  }}

int CountPositions()
  {{
   int c = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {{
      ulong tk = PositionGetTicket(i);
      if(tk == 0) continue;
      if(PositionSelectByTicket(tk) && PositionGetString(POSITION_SYMBOL) == _Symbol
         && PositionGetInteger(POSITION_MAGIC) == MagicNumber) c++;
     }}
   return c;
  }}

double LotsForRisk(double stopDistance)
  {{
   double eq   = AccountInfoDouble(ACCOUNT_EQUITY);
   double risk = eq * RiskPercent / 100.0;
   double tv   = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE);
   double ts   = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
   if(ts <= 0 || tv <= 0 || stopDistance <= 0) return 0.0;
   double lossPerLot = stopDistance / ts * tv;
   if(lossPerLot <= 0) return 0.0;
   double lots = risk / lossPerLot;
   double step = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   double vmin = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double vmax = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   if(step > 0) lots = MathFloor(lots / step) * step;
   if(lots < vmin) return 0.0;
   return MathMin(lots, vmax);
  }}

void OnTick()
  {{
   if(TradeOnNewBarOnly)
     {{
      datetime t = iTime(_Symbol, PERIOD_CURRENT, 0);
      if(t == lastBar) return;
      lastBar = t;
     }}
   if(CountPositions() >= MaxPositions) return;

   double atr = Buf(atrHandle, 0, 1);
   if(atr <= 0) return;
   double price = iClose(_Symbol, PERIOD_CURRENT, 1);
   bool buy = false, sell = false;
{_entry_logic(res, 5)}
   if(!buy && !sell) return;

   double stopDist = atr * ATRMultiplier;
   double lots = LotsForRisk(stopDist);
   if(lots <= 0) return;

   int digits = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);
   double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);

   if(buy)
     {{
      double sl = NormalizeDouble(ask - stopDist, digits);
      double tp = NormalizeDouble(ask + stopDist * RewardRatio, digits);
      trade.Buy(lots, _Symbol, ask, sl, tp, "{name}");
     }}
   else
     {{
      double sl = NormalizeDouble(bid + stopDist, digits);
      double tp = NormalizeDouble(bid - stopDist * RewardRatio, digits);
      trade.Sell(lots, _Symbol, bid, sl, tp, "{name}");
     }}
  }}
"""

    # ---- MQL4 ----
    return header + f"""
datetime lastBar = 0;

int OnInit()
  {{
   Print("{name} initialised. Risk ", RiskPercent, "% | ATRx", ATRMultiplier, " | RR ", RewardRatio);
   return INIT_SUCCEEDED;
  }}

int CountPositions()
  {{
   int c = 0;
   for(int i = OrdersTotal() - 1; i >= 0; i--)
     {{
      if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES)) continue;
      if(OrderSymbol() == Symbol() && OrderMagicNumber() == MagicNumber
         && (OrderType() == OP_BUY || OrderType() == OP_SELL)) c++;
     }}
   return c;
  }}

double LotsForRisk(double stopDistance)
  {{
   double eq   = AccountEquity();
   double risk = eq * RiskPercent / 100.0;
   double tv   = MarketInfo(Symbol(), MODE_TICKVALUE);
   double ts   = MarketInfo(Symbol(), MODE_TICKSIZE);
   if(ts <= 0 || tv <= 0 || stopDistance <= 0) return 0.0;
   double lossPerLot = stopDistance / ts * tv;
   if(lossPerLot <= 0) return 0.0;
   double lots = risk / lossPerLot;
   double step = MarketInfo(Symbol(), MODE_LOTSTEP);
   double vmin = MarketInfo(Symbol(), MODE_MINLOT);
   double vmax = MarketInfo(Symbol(), MODE_MAXLOT);
   if(step > 0) lots = MathFloor(lots / step) * step;
   if(lots < vmin) return 0.0;
   return MathMin(lots, vmax);
  }}

void OnTick()
  {{
   if(TradeOnNewBarOnly)
     {{
      if(Time[0] == lastBar) return;
      lastBar = Time[0];
     }}
   if(CountPositions() >= MaxPositions) return;

   double atr = iATR(NULL, 0, ATRPeriod, 1);
   if(atr <= 0) return;
   double price = Close[1];
   bool buy = false, sell = false;
{_entry_logic(res, 4)}
   if(!buy && !sell) return;

   double stopDist = atr * ATRMultiplier;
   double lots = LotsForRisk(stopDist);
   if(lots <= 0) return;

   int digits = (int)MarketInfo(Symbol(), MODE_DIGITS);
   double stopLvl = MarketInfo(Symbol(), MODE_STOPLEVEL) * Point;
   if(stopDist < stopLvl) stopDist = stopLvl;

   if(buy)
     {{
      double sl = NormalizeDouble(Ask - stopDist, digits);
      double tp = NormalizeDouble(Ask + stopDist * RewardRatio, digits);
      int t1 = OrderSend(Symbol(), OP_BUY, lots, NormalizeDouble(Ask, digits), Slippage,
                         sl, tp, "{name}", MagicNumber, 0, clrDodgerBlue);
      if(t1 < 0) Print("Buy failed err=", GetLastError());
     }}
   else
     {{
      double sl = NormalizeDouble(Bid + stopDist, digits);
      double tp = NormalizeDouble(Bid - stopDist * RewardRatio, digits);
      int t2 = OrderSend(Symbol(), OP_SELL, lots, NormalizeDouble(Bid, digits), Slippage,
                         sl, tp, "{name}", MagicNumber, 0, clrOrangeRed);
      if(t2 < 0) Print("Sell failed err=", GetLastError());
     }}
  }}
"""
