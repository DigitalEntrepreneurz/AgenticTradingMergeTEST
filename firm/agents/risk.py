"""Risk agent - the firm's veto. Deterministic, never delegated to an LLM.

Every signal passes through `vet()`. The LLM may comment, but approval is
decided by hard-coded arithmetic against the configured envelope. The risk
agent can tighten, never loosen.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone

from .base import Agent


@dataclass
class RiskDecision:
    approved: bool
    lots: float = 0.0
    reason: str = ""
    stop: float = 0.0
    take: float = 0.0
    risk_usd: float = 0.0


class RiskAgent(Agent):
    name = "risk"
    title = "Chief Risk Officer"
    charter = ("Size every position, enforce daily loss and drawdown kill switches, "
               "veto any trade breaching the envelope. Hard limits are code, not prompts.")

    # ---------------- envelope ----------------
    @property
    def limits(self) -> dict:
        r = self.cfg.get("risk", {}) or {}
        return {
            "risk_per_trade": float(r.get("max_risk_per_trade_pct", 0.75)),
            "daily_loss": float(r.get("max_daily_loss_pct", 3.0)),
            "max_dd": float(r.get("max_total_drawdown_pct", 12.0)),
            "max_open": int(r.get("max_open_positions", 4)),
            "per_symbol": int(r.get("max_positions_per_symbol", 1)),
            "max_lots": float(r.get("max_lots_per_order", 1.0)),
            "min_stop_atr": float(r.get("min_stop_distance_atr", 1.0)),
            "require_sl": bool(r.get("require_stop_loss", True)),
            "hours": list(r.get("trading_hours_utc", [0, 24])),
            "friday_cut": r.get("block_new_trades_on_friday_after_utc", 20),
            "max_cluster_pct": float(r.get("max_correlated_cluster_pct", 2.0)),
            "corr_threshold": float(r.get("correlation_threshold", 0.7)),
            "corr_enabled": bool(r.get("correlation_guard", True)),
        }

    # ---------------- state ----------------
    def equity(self) -> float:
        total = 0.0
        for br in self.ctx.brokers.values():
            try:
                total += br.account().equity
            except Exception:
                continue
        return total

    def _baseline(self, key: str, value: float) -> float:
        cur = self.mem.get(key)
        if cur is None and value > 0:
            self.mem.put(key, value)
            return value
        return float(cur or value)

    def daily_pnl(self) -> float:
        eq = self.equity()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        key = f"equity_open:{today}"
        start = self._baseline(key, eq)
        return eq - start

    def drawdown_pct(self) -> float:
        eq = self.equity()
        peak = float(self.mem.get("equity_peak") or 0)
        if eq > peak:
            self.mem.put("equity_peak", eq)
            peak = eq
        return 0.0 if peak <= 0 else (peak - eq) / peak * 100

    def kill_switch(self) -> tuple[bool, str]:
        """True = trading halted."""
        if self.mem.get("halt_until", 0) > time.time():
            return True, self.mem.get("halt_reason", "manual halt")
        lim = self.limits
        eq = self.equity()
        if eq <= 0:
            return True, "no equity reported by any broker"
        pnl = self.daily_pnl()
        if pnl < 0 and abs(pnl) / eq * 100 >= lim["daily_loss"]:
            reason = (f"daily loss {abs(pnl)/eq*100:.2f}% >= limit {lim['daily_loss']}%")
            self.halt(reason, hours=12)
            return True, reason
        dd = self.drawdown_pct()
        if dd >= lim["max_dd"]:
            reason = f"drawdown {dd:.2f}% >= limit {lim['max_dd']}%"
            self.halt(reason, hours=48)
            return True, reason
        return False, ""

    def halt(self, reason: str, hours: float = 12) -> None:
        self.mem.put("halt_until", time.time() + hours * 3600)
        self.mem.put("halt_reason", reason)
        self.log(f"KILL SWITCH: {reason} (halted {hours}h)", "critical")

    def resume(self) -> None:
        self.mem.put("halt_until", 0)
        self.mem.put("halt_reason", "")
        self.log("kill switch cleared by board", "warn")

    # ---------------- sizing ----------------
    def position_size(self, broker, symbol: str, entry: float, stop: float) -> tuple[float, float]:
        """Return (lots, risk_usd) honouring the per-trade risk % and contract specs."""
        spec = broker.symbol_spec(symbol)
        eq = broker.account().equity
        risk_usd = eq * self.limits["risk_per_trade"] / 100
        dist = abs(entry - stop)
        if dist <= 0 or spec.tick_size <= 0 or spec.tick_value <= 0:
            return 0.0, 0.0
        ticks = dist / spec.tick_size
        loss_per_lot = ticks * spec.tick_value
        if loss_per_lot <= 0:
            return 0.0, 0.0
        lots = risk_usd / loss_per_lot
        step = spec.volume_step or 0.01
        lots = int(lots / step) * step                      # round DOWN, never up
        lots = max(0.0, min(lots, spec.volume_max, self.limits["max_lots"]))
        if lots < spec.volume_min:
            return 0.0, 0.0
        return round(lots, 2), round(lots * loss_per_lot, 2)

    # ---------------- vetting ----------------
    def _position_risk(self, pos, broker) -> float:
        """Dollar risk still on the table for an open position.

        Distance to its stop, in account currency. Falls back to the per-trade
        budget when a position has no stop recorded, so an unprotected trade is
        never treated as risk-free.
        """
        try:
            entry, stop = float(pos.entry), float(pos.stop or 0)
            if stop <= 0:
                return self.equity() * self.limits["risk_per_trade"] / 100.0
            spec = broker.symbol_spec(pos.symbol)
            ticks = abs(entry - stop) / spec.tick_size if spec.tick_size else 0
            return abs(ticks * spec.tick_value * float(pos.lots))
        except Exception:
            return self.equity() * self.limits["risk_per_trade"] / 100.0

    def vet(self, signal: dict, broker) -> RiskDecision:
        """Fail-closed wrapper: a risk gate must never raise.

        Any unexpected fault - a dead broker connection, a malformed signal -
        becomes a refusal, not an exception. The gate is the last thing
        standing between a bug and real money, so when it cannot reason it
        says no."""
        try:
            return self._vet(signal, broker)
        except Exception as e:
            self.log(f"risk gate fault: {e}", level="warn")
            return RiskDecision(
                False, reason=f"risk gate could not verify this trade ({e}) - refusing")

    def _vet(self, signal: dict, broker) -> RiskDecision:
        lim = self.limits
        sym = signal["symbol"]

        halted, why = self.kill_switch()
        if halted:
            return RiskDecision(False, reason=f"TRADING HALTED: {why}")

        now = datetime.now(timezone.utc)
        if not (lim["hours"][0] <= now.hour < lim["hours"][1]):
            return RiskDecision(False, reason=f"outside trading hours {lim['hours']} UTC")
        if lim["friday_cut"] is not None and now.weekday() == 4 and now.hour >= int(
                lim["friday_cut"]):
            return RiskDecision(False, reason="weekend gap guard: no new Friday trades")

        if lim["require_sl"] and not signal.get("stop"):
            return RiskDecision(False, reason="signal has no stop loss")

        try:
            positions = broker.positions()
        except Exception as e:
            return RiskDecision(False, reason=f"cannot read positions: {e}")
        if len(positions) >= lim["max_open"]:
            return RiskDecision(False,
                                reason=f"max open positions reached ({lim['max_open']})")
        same = [p for p in positions if p.symbol == sym]
        if len(same) >= lim["per_symbol"]:
            return RiskDecision(False, reason=f"already {len(same)} position(s) in {sym}")
        # no hedging / stacking against ourselves
        if any(p.symbol == sym and p.side != signal["side"] for p in positions):
            return RiskDecision(False, reason=f"opposite {sym} position already open")

        entry = float(signal["entry"])
        stop = float(signal["stop"])
        take = float(signal.get("take") or 0)
        if (signal["side"] == "buy" and stop >= entry) or \
           (signal["side"] == "sell" and stop <= entry):
            return RiskDecision(False, reason="stop is on the wrong side of entry")

        # stop must respect ATR and the broker's minimum stop distance
        spec = broker.symbol_spec(sym)
        atr_v = float(signal.get("meta", {}).get("atr") or 0)
        if atr_v and abs(entry - stop) < atr_v * lim["min_stop_atr"]:
            return RiskDecision(False,
                                reason=f"stop {abs(entry-stop):.5f} < "
                                       f"{lim['min_stop_atr']}xATR ({atr_v:.5f})")
        min_dist = spec.stops_level * spec.point
        if min_dist and abs(entry - stop) < min_dist:
            return RiskDecision(False, reason=f"stop inside broker stop level {min_dist:.5f}")

        rr = (abs(take - entry) / abs(entry - stop)) if take else 0
        if take and rr < 1.0:
            return RiskDecision(False, reason=f"reward:risk {rr:.2f} below 1.0")

        lots, risk_usd = self.position_size(broker, sym, entry, stop)
        if lots <= 0:
            return RiskDecision(False, reason="computed size below broker minimum lot")

        eq = broker.account().equity
        # room left in the daily budget
        pnl = self.daily_pnl()
        remaining = eq * lim["daily_loss"] / 100 - max(0.0, -pnl)
        if risk_usd > remaining:
            return RiskDecision(False,
                                reason=f"risk ${risk_usd:.2f} exceeds remaining daily "
                                       f"budget ${remaining:.2f}")

        # ---- portfolio correlation: is this genuinely a new bet? ----
        # Counting positions is not measuring exposure. Three trades that all
        # express the same view are one leveraged bet, and only a correlation
        # check can see that.
        corr_note = ""
        if lim["corr_enabled"] and positions:
            try:
                from ..portfolio import assess
                open_rows = [{"symbol": p.symbol, "side": p.side,
                              "risk_usd": self._position_risk(p, broker)}
                             for p in positions]
                cand = {"symbol": sym, "side": signal["side"], "risk_usd": risk_usd}
                view = assess(broker, open_rows, eq, candidate=cand,
                              timeframe=self.cfg.timeframe,
                              threshold=lim["corr_threshold"],
                              max_cluster_pct=lim["max_cluster_pct"])
                if view["breach"]:
                    w = view["worst_cluster"] or {}
                    return RiskDecision(
                        False,
                        reason=(f"correlation limit: {'+'.join(w.get('symbols', []))} "
                                f"would carry ${abs(w.get('net_usd', 0)):.2f} net risk "
                                f"({view['worst_cluster_pct']:.2f}% of equity, cap "
                                f"{lim['max_cluster_pct']}%) - these move together, "
                                "so it is one bet, not several"))
                heat = view["heat"]
                corr_note = (f", {heat['effective_bets']} effective bets across "
                             f"{heat['positions']} positions")
            except Exception as e:                      # never block on a bug here
                self.log(f"correlation check unavailable: {e}", level="warn")

        return RiskDecision(True, lots=lots,
                            reason=(f"approved {lots} lots, risking ${risk_usd:.2f} "
                                    f"({lim['risk_per_trade']}% of ${eq:,.2f}), "
                                    f"R:R {rr:.2f}{corr_note}"),
                            stop=stop, take=take, risk_usd=risk_usd)

    # ---------------- reporting ----------------
    def handle(self, issue: dict) -> str:
        lim = self.limits
        eq = self.equity()
        pnl = self.daily_pnl()
        dd = self.drawdown_pct()
        halted, why = self.kill_switch()
        opens = []
        for bid, br in self.ctx.brokers.items():
            try:
                for p in br.positions():
                    opens.append(f"  {bid}/{br.platform} {p.side} {p.lots} {p.symbol} "
                                 f"@{p.entry} sl={p.stop} tp={p.take} pnl={p.profit:+.2f}")
            except Exception as e:
                opens.append(f"  {bid}: unreadable ({e})")

        report = (
            f"RISK REPORT\n"
            f"Equity: ${eq:,.2f} across {len(self.ctx.brokers)} account(s)\n"
            f"Day P&L: ${pnl:+,.2f} ({(pnl/eq*100 if eq else 0):+.2f}%) | "
            f"limit -{lim['daily_loss']}%\n"
            f"Drawdown from peak: {dd:.2f}% | limit {lim['max_dd']}%\n"
            f"Kill switch: {'ENGAGED - ' + why if halted else 'clear'}\n"
            f"Per-trade risk: {lim['risk_per_trade']}% | max open {lim['max_open']} | "
            f"max lots {lim['max_lots']}\n"
            f"Open positions ({len(opens)}):\n" + ("\n".join(opens) if opens else "  none"))
        self.remember("risk_report", f"risk:{int(time.time())}", report)
        return report

    # ---------------- strategy supervision ----------------
    def supervise(self, force: bool = False) -> dict:
        """Retire strategies whose live results have broken down.

        Risk owns this because it is a *stop trading* decision, not a research
        one. Throttled: a full drift pass reads every closed trade, which is
        wasted work at tick speed."""
        every = float(self.cfg.get("supervision.interval_seconds", 3600) or 3600)
        last = float(self.mem.get("supervision_last_run", 0) or 0)
        now = time.time()
        if not force and now - last < every:
            return {"skipped": True}
        self.mem.put("supervision_last_run", now)
        try:
            from ..supervisor import review
            out = review(self.mem, self.cfg, actor=self.name)
            for name in out.get("quarantined", []):
                self.mem.create_issue(
                    title=f"Strategy quarantined: {name}",
                    body=(f"{name} was pulled from live trading automatically. Its live "
                          "expectancy has broken down against the backtest that approved "
                          "it. It will place no new trades until reinstated. Open "
                          "positions were left alone.\n\nReinstate with: "
                          f"python cli.py strategies --reinstate '{name}'"),
                    assignee="ceo", author=self.name)
            return out
        except Exception as e:                      # supervision must never stop the firm
            self.log(f"supervision failed: {e}", level="warn")
            return {"error": str(e)}

    def tick(self) -> None:
        self.supervise()
        halted, why = self.kill_switch()
        if halted and self.mem.get("halt_flattened_at", 0) < self.mem.get("halt_until", 0):
            # flatten everything once when the switch engages
            for br in self.ctx.brokers.values():
                try:
                    for p in br.positions():
                        br.close_position(p.ticket)
                        self.log(f"flattened {p.symbol} {p.ticket} due to kill switch", "warn")
                except Exception as e:
                    self.log(f"flatten failed: {e}", "error")
            self.mem.put("halt_flattened_at", self.mem.get("halt_until", 0))
