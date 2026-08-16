"""Scout agent - autonomously discovers and validates trending strategies.

The seventh department. It runs the auto-scan suite: pull trending strategy
archetypes, backtest and walk-forward them all, and file the survivors as
proposals for the backtest department to gate.

It proposes; it never approves. Everything it finds still has to clear the
normal validation pipeline before execution will touch it.
"""
from __future__ import annotations

import json
import time

from ..scout import discover, scan, summarise
from .base import Agent

SYSTEM = """You are the Head of Strategy Discovery at a systematic trading firm.
You survey what retail forex traders are being taught, encode setups as testable
specs and let the backtester decide which survive. You are deeply sceptical:
popularity is not edge. Reply with STRICT JSON, no prose."""


class ScoutAgent(Agent):
    name = "scout"
    title = "Head of Strategy Discovery"
    charter = ("Auto-scan trending forex strategies, backtest and walk-forward "
               "each one, and file only the survivors as proposals.")

    # ---------------- scanning ----------------
    def run_scan(self, symbols: list[str] | None = None, timeframe: str = "",
                 limit: int = 6, max_combos: int = 20, bars: int = 1600,
                 use_llm: bool = True, tags: list[str] | None = None,
                 progress=None) -> list[dict]:
        """Discover then validate. Returns ranked result dicts."""
        from ..lab import Lab
        symbols = symbols or self.cfg.symbols[:2]
        timeframe = timeframe or self.cfg.timeframe
        specs = discover(self.llm if (use_llm and self.within_budget()) else None,
                         tags=tags, limit=limit, use_llm=use_llm)
        self.log(f"discovered {len(specs)} candidate strategies")
        lab = Lab(self.ctx.primary_broker())
        return scan(lab, specs, symbols, timeframe, max_combos=max_combos,
                    bars=bars, progress=progress, memory=self.mem)

    # ---------------- issue handling ----------------
    def handle(self, issue: dict) -> str:
        body = f"{issue.get('title', '')} {issue.get('body', '')}".lower()
        # a narrow scan when the board names a theme, otherwise the broad sweep
        tags = [t for t in ("trend", "breakout", "reversion", "momentum",
                            "price-action", "volatility", "session")
                if t in body] or None

        results = self.run_scan(tags=tags)
        s = summarise(results)

        lines = []
        for r in results[:12]:
            m = r.get("metrics", {})
            lines.append(
                f"- [{r['verdict']}] {r['name']} on {r['symbol']} "
                f"{r['timeframe']}: score {r.get('score', 0)}, "
                f"{m.get('trades', 0)} trades, PF {m.get('profit_factor', 0)}, "
                f"expectancy {m.get('expectancy_r', 0)}R")

        best = s.get("best")
        head = (f"Auto-scan complete: {s['scanned']} strategy/symbol combinations "
                f"tested — {s['adopt']} ADOPT, {s['watch']} WATCH, "
                f"{s['ignore']} IGNORE, {s['errors']} errored.")
        if best and best.get("verdict") == "ADOPT":
            head += (f"\nBest: {best['name']} on {best['symbol']} "
                     f"(score {best.get('score', 0)}).")
        else:
            head += "\nNothing cleared the validation gate this pass."

        summary = (head + "\n\n" + "\n".join(lines) +
                   "\n\nSurvivors are filed as *proposals* only. The backtest "
                   "department still has to approve them before execution can "
                   "trade them. Popularity is not edge.")
        self.remember("cycle", f"scout:{int(time.time())}", summary)
        self.mem.put("last_scan", {"summary": s, "at": time.time()})
        return summary

    def tick(self) -> None:
        return None
