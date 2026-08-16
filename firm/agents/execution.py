"""Execution agent - generates signals from APPROVED strategies and routes orders
to MT4/MT5. Every order passes the risk agent first, and live orders require both
config switches. Paper mode routes to the simulated engine instead.
"""
from __future__ import annotations

import time

from ..brokers.base import OrderRequest
from ..strategies.library import run as run_strategy
from .base import Agent
from .risk import RiskAgent


class ExecutionAgent(Agent):
    name = "execution"
    title = "Head of Execution"
    charter = ("Scan approved strategies for signals, obtain risk approval, route "
               "orders to the MT4/MT5 terminal, manage and reconcile open trades.")

    def __init__(self, ctx, risk: RiskAgent | None = None):
        super().__init__(ctx)
        self.risk = risk or RiskAgent(ctx)

    # ---------------- routing ----------------
    def _broker_for(self, symbol: str):
        """Pick the account that can trade this symbol (first that answers)."""
        preferred = self.mem.get("route_default")
        ordered = list(self.ctx.brokers.items())
        if preferred and preferred in self.ctx.brokers:
            ordered.sort(key=lambda kv: kv[0] != preferred)
        for _bid, br in ordered:
            try:
                br.symbol_spec(symbol)
                return br
            except Exception:
                continue
        return self.ctx.primary_broker()

    # ---------------- signal generation ----------------
    def scan(self) -> list[dict]:
        approved = self.mem.strategies("approved")
        if not approved:
            return []
        out: list[dict] = []
        for row in approved:
            spec = row["spec"]
            strat, sym = spec.get("strategy"), spec.get("symbol")
            tf = spec.get("timeframe", self.cfg.timeframe)
            br = self._broker_for(sym)
            if not br:
                continue
            try:
                bars = br.bars(sym, tf, 320)
            except Exception as e:
                self.log(f"bars failed for {sym}: {e}", "error")
                continue
            if len(bars) < 150:
                continue

            # one signal per strategy per bar - no duplicate entries
            bar_key = f"{row['name']}:{int(bars[-1].time)}"
            if self.mem.has_seen("signal_bar", bar_key):
                continue

            sig = run_strategy(strat, bars, spec.get("params", {}))
            if not sig:
                continue
            self.remember("signal_bar", bar_key, f"{sig.side} {sym}")
            sid = self.mem.add_signal(
                strategy=row["name"], symbol=sym, side=sig.side, entry=sig.entry,
                stop=sig.stop, take=sig.take, confidence=sig.confidence,
                rationale=sig.rationale, status="pending")
            out.append({"id": sid, "strategy": row["name"], "symbol": sym,
                        "side": sig.side, "entry": sig.entry, "stop": sig.stop,
                        "take": sig.take, "confidence": sig.confidence,
                        "rationale": sig.rationale, "meta": sig.meta})
        return out

    # ---------------- order placement ----------------
    def execute(self, signal: dict) -> str:
        br = self._broker_for(signal["symbol"])
        if not br:
            return f"no broker for {signal['symbol']}"

        decision = self.risk.vet(signal, br)
        if not decision.approved:
            self.mem.set_signal_status(signal["id"], "rejected", decision.reason)
            self.log(f"REJECTED {signal['side']} {signal['symbol']}: {decision.reason}")
            return f"rejected: {decision.reason}"

        live = self.cfg.live_enabled
        mode = "live" if live else "paper"
        if live and getattr(br, "platform", "SIM") == "SIM":
            mode = "paper"        # simulated broker can never be live
            live = False

        req = OrderRequest(symbol=signal["symbol"], side=signal["side"],
                           lots=decision.lots, stop=decision.stop, take=decision.take,
                           comment=f"agentic:{signal['strategy'][:18]}",
                           magic=int(self.cfg.get("broker.bridge.magic", 770420)))
        res = br.market_order(req)
        if not res.ok:
            self.mem.set_signal_status(signal["id"], "failed", res.message)
            self.log(f"ORDER FAILED {signal['symbol']}: {res.message}", "error")
            return f"order failed: {res.message}"

        tid = self.mem.add_trade(
            account=br.id, platform=getattr(br, "platform", "SIM"), ticket=res.ticket,
            symbol=signal["symbol"], side=signal["side"], lots=decision.lots,
            entry=res.price or signal["entry"], stop=decision.stop, take=decision.take,
            status="open", mode=mode, signal_id=signal["id"],
            meta={"strategy": signal["strategy"], "rationale": signal.get("rationale", ""),
                  "risk_usd": decision.risk_usd})
        self.mem.set_signal_status(signal["id"], "executed", decision.reason)
        msg = (f"{mode.upper()} {signal['side'].upper()} {decision.lots} {signal['symbol']} "
               f"@ {res.price:.5f} sl={decision.stop:.5f} tp={decision.take:.5f} "
               f"on {br.id}/{getattr(br, 'platform', 'SIM')} ticket={res.ticket} "
               f"({decision.reason})")
        self.log(msg)
        self.remember("execution", f"trade:{tid}", msg)
        return msg

    # ---------------- reconciliation ----------------
    def reconcile(self) -> list[str]:
        """Sync DB with the terminal; settle trades the broker already closed."""
        notes: list[str] = []
        for br in self.ctx.brokers.values():
            if hasattr(br, "sweep_stops"):        # simulated engine
                for ticket, px, pnl in br.sweep_stops():
                    rows = self.mem.q("SELECT * FROM trades WHERE ticket=? AND status='open'",
                                      (str(ticket),))
                    if rows:
                        self.mem.close_trade(rows[0]["id"], px, pnl)
                        notes.append(f"{rows[0]['symbol']} closed @{px:.5f} pnl {pnl:+.2f}")
            try:
                live_tickets = {p.ticket for p in br.positions()}
            except Exception:
                continue
            for row in self.mem.open_trades():
                if row["account"] != br.id:
                    continue
                if row["ticket"] not in live_tickets:
                    # gone from the terminal: stop/target hit or closed manually
                    pnl = float(row.get("pnl") or 0.0)
                    self.mem.close_trade(row["id"], row.get("entry") or 0.0, pnl)
                    notes.append(f"{row['symbol']} ticket {row['ticket']} closed at broker")
        for n in notes:
            self.log(f"reconcile: {n}")
        return notes

    # ---------------- entry points ----------------
    def handle(self, issue: dict) -> str:
        self.reconcile()
        halted, why = self.risk.kill_switch()
        if halted:
            return f"Execution paused - kill switch engaged: {why}"

        signals = self.scan()
        if not signals:
            approved = len(self.mem.strategies("approved"))
            return (f"Scanned {approved} approved strategy/strategies across "
                    f"{', '.join(self.cfg.symbols)}: no valid setups on the current bar. "
                    f"No trades placed.")
        results = [f"- {self.execute(s)}" for s in signals]
        mode = "LIVE" if self.cfg.live_enabled else "PAPER"
        return f"Execution cycle ({mode}), {len(signals)} signal(s):\n" + "\n".join(results)

    def tick(self) -> None:
        self.reconcile()
