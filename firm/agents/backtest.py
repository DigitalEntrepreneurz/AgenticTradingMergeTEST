"""Backtest agent - validates every proposal before it can ever trade.

Runs an event-driven backtest plus walk-forward validation. Only strategies
that clear the bar get status 'approved'; the execution agent will trade
nothing else.
"""
from __future__ import annotations

import json
import time

from ..backtester import backtest, optimize, walk_forward
from .base import Agent

GRIDS = {
    "ema_trend_pullback": {"atr_mult": [1.5, 2.0, 2.5], "rr": [1.5, 2.0, 2.5],
                           "fast": [13, 21]},
    "donchian_breakout":  {"channel": [15, 20, 30], "atr_mult": [2.0, 2.5],
                           "rr": [2.0, 2.5, 3.0]},
    "bollinger_reversion": {"bb_mult": [2.0, 2.2, 2.5], "rr": [1.2, 1.5, 2.0]},
    "macd_momentum":      {"atr_mult": [1.5, 2.0, 2.5], "rr": [1.5, 2.0, 2.5]},
}


class BacktestAgent(Agent):
    name = "backtest"
    title = "Head of Backtesting"
    charter = ("Backtest and walk-forward validate every proposed strategy. "
               "Approve only what survives out-of-sample. Reject curve fits.")

    def _validate_spec(self, cand: dict, wrapper: dict, sym: str, tf: str) -> str:
        """Validate a composite rule spec (auto-scan / video ingest) via the Lab.

        Same gate as a built-in: it must clear verdict() AND walk-forward.
        """
        from ..lab import Lab
        spec = wrapper.get("spec") or {}
        try:
            lab = Lab(self.ctx.primary_broker())
            res = lab.optimize_spec(spec, sym, tf, max_combos=12, bars_count=1200)
        except Exception as e:
            self.mem.upsert_strategy(
                name=cand["name"], spec=wrapper, source=cand["source"],
                status="rejected", notes=f"spec backtest failed: {e}"[:400])
            return f"- {cand['name']}: FAIL | could not be tested ({type(e).__name__})"

        metrics = dict(res.metrics or {})
        metrics["walk_forward"] = res.walk_forward
        metrics["best_params"] = res.params
        passed = bool(res.passed)

        self.mem.x("INSERT INTO backtests(created_at,strategy,symbol,timeframe,metrics,"
                   "passed,notes) VALUES(?,?,?,?,?,?,?)",
                   (time.time(), cand["name"], sym, tf, json.dumps(metrics),
                    1 if passed else 0, res.reason))

        new_wrapper = dict(wrapper); new_wrapper["params"] = res.params
        self.mem.upsert_strategy(
            name=cand["name"], spec=new_wrapper, source=cand["source"],
            status="approved" if passed else "rejected",
            score=round(res.score, 3), metrics=metrics,
            notes=(f"{'APPROVED' if passed else 'REJECTED'}: {res.reason}. "
                   f"WF {res.walk_forward.get('oos_total_r', 'n/a')}R over "
                   f"{res.walk_forward.get('oos_trades', 0)} OOS trades."))
        self.remember("backtest", f"{cand['name']}:{int(time.time())}",
                      json.dumps(metrics), {"passed": passed})

        m = res.metrics or {}
        return (f"- {cand['name']} (rule spec): {'PASS' if passed else 'FAIL'} | "
                f"trades {m.get('trades', 0)}, PF {m.get('profit_factor', 0)}, "
                f"exp {m.get('expectancy_r', 0)}R, score {res.score} | {res.reason}")

    def handle(self, issue: dict) -> str:
        br = self.ctx.primary_broker()
        if not br:
            return "No broker connected - cannot fetch history for backtesting."

        candidates = self.mem.strategies("proposed")
        if not candidates:
            return "No proposed strategies awaiting validation. Nothing to do."

        lines: list[str] = []
        approved = rejected = 0

        for cand in candidates[:6]:                     # cost control per cycle
            spec = cand["spec"]
            sym = spec.get("symbol")
            tf = spec.get("timeframe", self.cfg.timeframe)

            # Auto-scanned / ingested strategies are composite rule specs, not
            # library names. They validate through the Lab, which knows how to
            # optimize and walk-forward a spec.
            if spec.get("composite") and spec.get("spec"):
                if not sym:
                    continue
                line = self._validate_spec(cand, spec, sym, tf)
                lines.append(line)
                approved += int(line.startswith("- ") and "PASS" in line)
                rejected += int("FAIL" in line)
                continue

            strat = spec.get("strategy")
            if not strat or not sym:
                continue

            bars = br.bars(sym, tf, 1200)
            if len(bars) < 300:
                lines.append(f"- {cand['name']}: only {len(bars)} bars, skipped.")
                continue

            sspec = br.symbol_spec(sym)
            spread_pts = 10.0
            try:
                t = br.tick(sym)
                spread_pts = max(1.0, (t.ask - t.bid) / sspec.point)
            except Exception:
                pass

            # 1) baseline
            base = backtest(strat, bars, sym, tf, spec.get("params", {}), sspec,
                            spread_pts=spread_pts)
            # 2) grid search
            grid = GRIDS.get(strat, {})
            best_params = dict(spec.get("params", {}))
            best = base
            if grid:
                scored = optimize(strat, bars, sym, tf, grid, sspec,
                                  spread_pts=spread_pts, max_combos=18)
                if scored and scored[0][1].expectancy_r > base.expectancy_r:
                    best_params, best = dict(scored[0][0]), scored[0][1]
            # 3) walk-forward
            wf = walk_forward(strat, bars, sym, tf, grid or {"rr": [2.0]}, folds=3,
                              spec=sspec, spread_pts=spread_pts)

            ok, reason = best.verdict()
            robust = bool(wf.get("robust", False)) or wf.get("folds", 0) == 0
            passed = ok and robust

            metrics = best.summary()
            metrics["walk_forward"] = wf
            metrics["spread_pts"] = round(spread_pts, 1)
            metrics["best_params"] = best_params

            self.mem.x("INSERT INTO backtests(created_at,strategy,symbol,timeframe,metrics,"
                       "passed,notes) VALUES(?,?,?,?,?,?,?)",
                       (time.time(), cand["name"], sym, tf, json.dumps(metrics),
                        1 if passed else 0, reason))

            score = best.expectancy_r * min(best.trades, 60)
            new_spec = dict(spec); new_spec["params"] = best_params
            self.mem.upsert_strategy(
                name=cand["name"], spec=new_spec, source=cand["source"],
                status="approved" if passed else "rejected",
                score=round(score, 3), metrics=metrics,
                notes=(f"{'APPROVED' if passed else 'REJECTED'}: {reason}. "
                       f"WF {wf.get('oos_total_r', 'n/a')}R over "
                       f"{wf.get('oos_trades', 0)} OOS trades."))

            approved += int(passed)
            rejected += int(not passed)
            lines.append(
                f"- {cand['name']}: {'PASS' if passed else 'FAIL'} | trades {best.trades}, "
                f"win {best.win_rate}%, PF {best.profit_factor}, exp {best.expectancy_r}R, "
                f"maxDD {best.max_drawdown_r}R, WF {wf.get('oos_total_r', 'n/a')}R "
                f"({wf.get('oos_trades', 0)} OOS) | {reason}")

            self.remember("backtest", f"{cand['name']}:{int(time.time())}",
                          json.dumps(metrics), {"passed": passed})

        summary = (f"Backtest cycle: {approved} approved, {rejected} rejected "
                   f"of {min(len(candidates), 6)} candidates.\n" + "\n".join(lines) +
                   "\nApproved strategies are now eligible for execution; "
                   "rejected ones are recorded so research will not re-propose them.")
        return summary
