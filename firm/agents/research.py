"""Research agent - regime analysis and strategy proposals.

Reads live market structure from the broker, classifies the regime per symbol,
and proposes strategies from the library (or an LLM-refined variant). Never
repeats a proposal it already made - it checks firm memory first.
"""
from __future__ import annotations

import json
import time

from ..indicators import atr, closes, ema, rsi, slope
from ..strategies.library import all_strategies
from .base import Agent

SYSTEM = """You are the Head of Research at a systematic trading firm.
You analyse market regime data and recommend which of the firm's existing
strategies to deploy, with parameter adjustments. You are sceptical, concise
and quantitative. You never promise returns. You only recommend strategies from
the provided library. Reply with STRICT JSON, no prose outside it."""


class ResearchAgent(Agent):
    name = "research"
    title = "Head of Research"
    charter = ("Analyse market regime per symbol, propose and refine strategies, "
               "log findings to firm memory, never repeat prior work.")

    # ---------------- regime ----------------
    def regime(self, symbol: str) -> dict:
        br = self.ctx.primary_broker()
        if not br:
            return {"symbol": symbol, "error": "no broker connected"}
        bars = br.bars(symbol, self.cfg.timeframe, 320)
        if len(bars) < 120:
            return {"symbol": symbol, "error": f"only {len(bars)} bars"}
        c = closes(bars)
        e20, e50, e100 = ema(c, 20), ema(c, 50), ema(c, 100)
        a = atr(bars, 14)
        r = rsi(c, 14)
        i = len(c) - 1
        px = c[i]
        atr_pct = (a[i] / px * 100) if a[i] and px else 0.0
        tr_slope = slope(e50, 10)

        if e20[i] and e50[i] and e100[i]:
            if e20[i] > e50[i] > e100[i] and tr_slope > 0.001:
                label = "strong_uptrend"
            elif e20[i] < e50[i] < e100[i] and tr_slope < -0.001:
                label = "strong_downtrend"
            elif abs(tr_slope) < 0.0004:
                label = "range"
            else:
                label = "transitional"
        else:
            label = "unknown"

        recent = c[-60:]
        return {
            "symbol": symbol, "timeframe": self.cfg.timeframe, "price": round(px, 5),
            "regime": label, "ema20": round(e20[i] or 0, 5), "ema50": round(e50[i] or 0, 5),
            "ema100": round(e100[i] or 0, 5), "atr": round(a[i] or 0, 6),
            "atr_pct_of_price": round(atr_pct, 3), "rsi14": round(r[i] or 0, 1),
            "trend_slope": round(tr_slope, 5),
            "range_60_pct": round((max(recent) - min(recent)) / px * 100, 3),
        }

    # ---------------- heuristic mapping ----------------
    def _pick(self, reg: dict) -> tuple[str, dict, str]:
        label = reg.get("regime", "unknown")
        if label in ("strong_uptrend", "strong_downtrend"):
            return ("ema_trend_pullback",
                    {"atr_mult": 2.0, "rr": 2.2},
                    f"{label} on {reg['symbol']}: trade pullbacks with the trend.")
        if label == "range":
            return ("bollinger_reversion",
                    {"bb_mult": 2.2, "rr": 1.5},
                    f"{reg['symbol']} is ranging (slope {reg['trend_slope']}): fade extremes.")
        if label == "transitional":
            return ("donchian_breakout",
                    {"channel": 20, "rr": 2.5},
                    f"{reg['symbol']} is transitional: wait for a channel break.")
        return ("macd_momentum", {}, f"{reg['symbol']} unclear: momentum confirmation only.")

    # ---------------- main ----------------
    def handle(self, issue: dict) -> str:
        lines: list[str] = []
        proposals: list[dict] = []

        for sym in self.cfg.symbols:
            reg = self.regime(sym)
            if "error" in reg:
                lines.append(f"- {sym}: {reg['error']}")
                continue
            self.remember("regime", f"{sym}:{self.cfg.timeframe}:{int(time.time()//3600)}",
                          json.dumps(reg), reg)
            strat, params, why = self._pick(reg)
            proposals.append({"symbol": sym, "strategy": strat, "params": params,
                              "rationale": why, "regime": reg["regime"]})
            lines.append(f"- {sym}: {reg['regime']} | ATR {reg['atr_pct_of_price']}% "
                         f"| RSI {reg['rsi14']} -> propose {strat}")

        # Optional LLM refinement of the heuristic slate
        note = "heuristic engine"
        if self.llm.available and self.within_budget() and proposals:
            lib = {k: {"description": v["description"], "params": v["default_params"]}
                   for k, v in all_strategies().items()}
            reply = self.think(self.system_prompt(SYSTEM), (
                f"{self.firm_context()}\n\n"
                f"Strategy library:\n{json.dumps(lib, indent=2)}\n\n"
                f"Current regime read and my draft proposals:\n"
                f"{json.dumps(proposals, indent=2)}\n\n"
                f"Assignment: {issue['title']}\n{issue['body']}\n\n"
                'Return JSON: {"proposals":[{"symbol":"","strategy":"","params":{},'
                '"rationale":"","confidence":0.0}],"summary":""}\n'
                "Only use strategy names from the library. Adjust params only where "
                "the regime justifies it."), max_tokens=1400)
            data = reply.json()
            if isinstance(data, dict) and isinstance(data.get("proposals"), list):
                valid = [p for p in data["proposals"]
                         if p.get("strategy") in all_strategies()]
                if valid:
                    proposals = valid
                    note = f"LLM-refined ({reply.model}, ${reply.usd:.4f})"
                    if data.get("summary"):
                        lines.append(f"  research view: {data['summary'][:400]}")
            elif reply.error:
                lines.append(f"  (LLM unavailable: {reply.error}; used heuristics)")

        # Persist proposals as strategy candidates, skipping duplicates
        new = 0
        for p in proposals:
            name = f"{p['strategy']}@{p['symbol']}"
            key = f"{name}:{json.dumps(p.get('params', {}), sort_keys=True)}"
            if self.mem.has_seen("proposal", key):
                continue
            self.remember("proposal", key, p.get("rationale", ""), p)
            self.mem.upsert_strategy(
                name=name,
                spec={"strategy": p["strategy"], "symbol": p["symbol"],
                      "params": p.get("params", {}), "timeframe": self.cfg.timeframe},
                source="research", status="proposed",
                notes=p.get("rationale", ""))
            new += 1

        summary = (f"Research cycle complete ({note}).\n" + "\n".join(lines) +
                   f"\n{new} new strategy candidate(s) queued for backtesting; "
                   f"{len(proposals) - new} already known (skipped, no repeat work).")
        self.remember("cycle", f"research:{int(time.time())}", summary)
        return summary
