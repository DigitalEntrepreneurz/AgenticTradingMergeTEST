"""The firm: wires brokers + memory + agents together and runs the clock.

Agents are worked SEQUENTIALLY, one department per tick, in dependency order.
That is deliberate: fanning every agent out at once is what makes an agentic
system burn money.
"""
from __future__ import annotations

import time
from typing import Any

from .agents import AgentContext, build_firm
from .brokers import build_all
from .config import Config, load_config
from .llm import LLM
from .memory import Memory

ORDER = ["ceo", "scout", "research", "backtest", "risk", "execution",
         "cost_optimizer"]


class Firm:
    def __init__(self, cfg: Config | None = None, memory: Memory | None = None,
                 connect: bool = True):
        self.cfg = cfg or load_config()
        self.memory = memory or Memory()
        self.llm = LLM(api_key=self.cfg.llm_key,
                       provider=self.cfg.llm_provider,
                       base_url=self.cfg.llm_base_url,
                       model_prefix=self.cfg.llm_model_prefix,
                       memory=self.memory,
                       max_daily_usd=float(self.cfg.get("llm.max_daily_usd", 10.0)),
                       free_tier=bool(self.cfg.get("llm.free_tier", False)),
                       max_tokens_per_day=int(
                           self.cfg.get("llm.max_tokens_per_day", 0) or 0),
                       monthly_token_quota=int(
                           self.cfg.get("llm.monthly_token_quota", 0) or 0),
                       enabled=self.cfg.get("llm.provider", "anthropic") != "none")
        self.brokers: dict[str, Any] = build_all(self.cfg) if connect else {}
        self.ctx = AgentContext(cfg=self.cfg, memory=self.memory, llm=self.llm,
                                brokers=self.brokers)
        self.agents = build_firm(self.ctx)
        self._last: dict[str, float] = {}
        self.memory.log("info", "firm",
                        f"{self.cfg.firm_name} online | mode="
                        f"{'LIVE' if self.cfg.live_enabled else 'PAPER'} | "
                        f"accounts={[f'{b.id}:{getattr(b,'platform','SIM')}' for b in self.brokers.values()]}")

    # ---------------- board interface ----------------
    def board_request(self, title: str, body: str) -> int:
        """You talk to the CEO. Returns the issue id."""
        return self.memory.create_issue(title, body, "ceo", author="board")

    def inbox(self, limit: int = 10) -> list[dict]:
        rows = self.memory.q(
            "SELECT * FROM issues WHERE author='board' ORDER BY id DESC LIMIT ?", (limit,))
        for r in rows:
            r["comments"] = self.memory.issue_comments(r["id"])
        return rows

    # ---------------- clock ----------------
    def _due(self, key: str, minutes: float) -> bool:
        if minutes <= 0:
            return False
        if self.memory.get("throttle") and key != "risk":
            minutes *= 2
        last = self._last.get(key, 0.0)
        if time.time() - last >= minutes * 60:
            self._last[key] = time.time()
            return True
        return False

    def schedule(self) -> list[str]:
        """CEO raises standing assignments when their cadence is due."""
        s = self.cfg.get("schedule", {}) or {}
        ceo = self.agents["ceo"]
        fired = []
        for kind, key in (("scout", "scan_every_minutes"),
                          ("research", "research_every_minutes"),
                          ("backtest", "backtest_every_minutes"),
                          ("execution", "execution_every_minutes"),
                          ("risk", "risk_review_every_minutes"),
                          ("cost", "cost_review_every_minutes")):
            if self._due(kind, float(s.get(key, 0) or 0)):
                if ceo.autonomous_cycle(kind):
                    fired.append(kind)
        return fired

    def tick(self) -> dict:
        """One pass: schedule, let each department work, reconcile."""
        fired = self.schedule()
        handled: dict[str, int] = {}
        for name in ORDER:
            agent = self.agents.get(name)
            if not agent or not agent.enabled:
                continue
            try:
                n = agent.work()
                if n:
                    handled[name] = n
                agent.tick()
            except Exception as e:
                self.memory.log("error", name, f"tick failed: {type(e).__name__}: {e}")
        return {"scheduled": fired, "handled": handled,
                "open_issues": len(self.memory.open_issues())}

    def run(self, cycles: int = 0, tick_seconds: float | None = None) -> None:
        """Run forever (cycles=0) or a fixed number of ticks."""
        ts = tick_seconds or float(self.cfg.get("schedule.tick_seconds", 10))
        i = 0
        try:
            while cycles == 0 or i < cycles:
                out = self.tick()
                if out["scheduled"] or out["handled"]:
                    print(f"[tick {i}] scheduled={out['scheduled']} "
                          f"handled={out['handled']} open={out['open_issues']}")
                i += 1
                if cycles == 0 or i < cycles:
                    time.sleep(ts)
        except KeyboardInterrupt:
            print("\nShutting down the firm...")
        finally:
            for b in self.brokers.values():
                try:
                    b.disconnect()
                except Exception:
                    pass

    # ---------------- status ----------------
    def status(self) -> dict:
        risk = self.agents["risk"]
        accounts = []
        for bid, br in self.brokers.items():
            try:
                a = br.account()
                accounts.append({"id": bid, "platform": a.platform, "login": a.login,
                                 "server": a.server, "currency": a.currency,
                                 "balance": a.balance, "equity": a.equity,
                                 "free_margin": a.free_margin,
                                 "connected": br.is_connected()})
            except Exception as e:
                accounts.append({"id": bid, "platform": getattr(br, "platform", "?"),
                                 "error": str(e)[:120], "connected": False})
        halted, why = risk.kill_switch()
        strat = self.memory.strategies()
        closed = self.memory.closed_trades(500)
        wins = [t for t in closed if (t.get("pnl") or 0) > 0]
        return {
            "firm": self.cfg.firm_name,
            "mode": "LIVE" if self.cfg.live_enabled else "PAPER",
            "paper_trading": self.cfg.paper_trading,
            "allow_live_orders": self.cfg.allow_live_orders,
            "symbols": self.cfg.symbols,
            "timeframe": self.cfg.timeframe,
            "accounts": accounts,
            "kill_switch": {"engaged": halted, "reason": why},
            "equity": round(risk.equity(), 2),
            "day_pnl": round(risk.daily_pnl(), 2),
            "drawdown_pct": round(risk.drawdown_pct(), 2),
            "strategies": {
                "proposed": [s["name"] for s in strat if s["status"] == "proposed"],
                "approved": [s["name"] for s in strat if s["status"] == "approved"],
                "rejected": [s["name"] for s in strat if s["status"] == "rejected"],
            },
            "open_trades": self.memory.open_trades(),
            "closed_trades": len(closed),
            "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0.0,
            "realised_pnl": round(sum((t.get("pnl") or 0) for t in closed), 2),
            "llm_spend_24h": round(self.memory.cost_today(), 4),
            "open_issues": self.memory.open_issues(),
        }
