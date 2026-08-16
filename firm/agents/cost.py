"""Cost optimizer agent - keeps the firm's LLM spend under control.

The lesson from the video: running everything at once burned $40 in a minute.
This agent watches spend per agent, recommends cheaper models for mechanical
work and throttles schedules when the budget runs hot.
"""
from __future__ import annotations

import time

from .base import Agent

CHEAP = "claude-haiku-4-5"
MID = "claude-sonnet-4-5"

# Work that never needs a frontier model
MECHANICAL = {"backtest", "execution", "cost_optimizer"}


class CostAgent(Agent):
    name = "cost_optimizer"
    title = "Cost Optimizer"
    charter = ("Track LLM spend per agent, downgrade models for mechanical work, "
               "throttle schedules and hard-stop the firm before the budget blows.")

    def handle(self, issue: dict) -> str:
        total = self.mem.cost_today()
        cap = float(self.cfg.get("llm.max_daily_usd", 10.0))
        by_agent = self.mem.cost_by_agent()

        lines = [f"LLM spend (last 24h): ${total:.4f} of ${cap:.2f} cap "
                 f"({total/cap*100 if cap else 0:.1f}%)"]
        if by_agent:
            for row in by_agent:
                budget = float(self.cfg.get(
                    f"agents.{row['agent']}.budget_usd_per_day", 1.0))
                flag = " OVER BUDGET" if row["usd"] > budget else ""
                lines.append(f"  {row['agent']}: ${row['usd']:.4f} in {row['calls']} "
                             f"calls (budget ${budget:.2f}){flag}")
        else:
            lines.append("  no LLM calls recorded - firm is running on the "
                         "deterministic engine (cost: $0.00)")

        recs: list[str] = []
        for agent in MECHANICAL:
            model = self.cfg.get(f"agents.{agent}.model", MID)
            if model != CHEAP:
                recs.append(f"switch {agent} to {CHEAP} (mechanical work, "
                            f"~3x cheaper than {model})")

        # throttle if we are burning too fast
        if cap and total > cap * 0.75:
            cur = int(self.cfg.get("schedule.research_every_minutes", 60))
            recs.append(f"raise research_every_minutes {cur} -> {cur*2} "
                        f"(75% of daily cap consumed)")
            self.mem.put("throttle", True)
        elif cap and total < cap * 0.25 and self.mem.get("throttle"):
            self.mem.put("throttle", False)
            recs.append("spend is back under 25% of cap - throttle lifted")

        if cap and total >= cap:
            self.mem.put("llm_disabled_until", time.time() + 3600)
            recs.append("DAILY CAP REACHED - LLM calls suspended for 1h; agents fall "
                        "back to the deterministic rule engine (trading continues)")

        # concurrency guidance - the $40-in-a-minute lesson
        recs.append("keep agents sequential (one department per tick) rather than "
                    "fanning out all tasks simultaneously")

        report = "\n".join(lines) + "\nRecommendations:\n" + "\n".join(
            f"  - {r}" for r in recs)
        self.remember("cost_report", f"cost:{int(time.time())}", report)
        return report

    def tick(self) -> None:
        cap = float(self.cfg.get("llm.max_daily_usd", 10.0))
        if cap and self.mem.cost_today() >= cap:
            self.llm.enabled = False
