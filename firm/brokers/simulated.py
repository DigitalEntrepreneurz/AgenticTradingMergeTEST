"""Offline paper-trading broker.

Generates a deterministic but realistic price series so the whole firm can be
developed and tested with no terminal, no broker and no network. Fills carry a
spread, positions mark to market and stops/targets are honoured on each tick.
"""
from __future__ import annotations

import math
import random
import time

from .base import (AccountInfo, Bar, OrderRequest, OrderResult, Position,
                   SymbolSpec, Tick, TF_MINUTES)

# realistic-ish defaults per instrument
_DEFAULTS = {
    "EURUSD": dict(price=1.0850, digits=5, spread_pts=8,  contract=100_000, tick_value=1.0,
                   vol=0.0009),
    "GBPUSD": dict(price=1.2720, digits=5, spread_pts=11, contract=100_000, tick_value=1.0,
                   vol=0.0011),
    "USDJPY": dict(price=154.30, digits=3, spread_pts=9,  contract=100_000, tick_value=0.65,
                   vol=0.09),
    "XAUUSD": dict(price=2380.0, digits=2, spread_pts=25, contract=100,     tick_value=1.0,
                   vol=6.5),
    "US30":   dict(price=39500.0, digits=1, spread_pts=30, contract=1,      tick_value=0.1,
                   vol=180.0),
    "BTCUSD": dict(price=64000.0, digits=2, spread_pts=1500, contract=1,    tick_value=0.01,
                   vol=900.0),
}


def _spec_for(symbol: str) -> dict:
    if symbol in _DEFAULTS:
        return _DEFAULTS[symbol]
    seed = sum(ord(c) for c in symbol)
    return dict(price=1.0 + (seed % 50) / 25, digits=5, spread_pts=10,
                contract=100_000, tick_value=1.0, vol=0.0010)


class SimulatedBroker:
    platform = "SIM"

    def __init__(self, account_id: str = "sim", starting_balance: float = 10_000.0,
                 platform: str = "SIM", seed: int = 7):
        self.id = account_id
        self.platform = platform
        self._balance = float(starting_balance)
        self._start_balance = float(starting_balance)
        self._positions: dict[str, Position] = {}
        self._ticket = 1000
        self._connected = False
        self._rng = random.Random(seed)
        self._seed = seed
        self._closed_pnl: list[float] = []

    # ---------------- lifecycle ----------------
    def connect(self) -> bool:
        self._connected = True
        return True

    def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    # ---------------- market data ----------------
    def _base_price(self, symbol: str, at: float | None = None) -> float:
        """Deterministic pseudo-random walk: sin/cos drift + hashed noise."""
        d = _spec_for(symbol)
        t = (at if at is not None else time.time()) / 60.0     # minutes
        h = sum(ord(c) * 31 for c in symbol)
        trend = math.sin((t + h) / 900.0) * d["vol"] * 22
        swing = math.sin((t + h) / 130.0) * d["vol"] * 8
        chop = math.sin((t * 2.7 + h) / 11.0) * d["vol"] * 2
        micro = (math.sin(t * 7.3 + h) + math.cos(t * 3.1 + h * 0.7)) * d["vol"] * 0.5
        return max(d["price"] * 0.35, d["price"] + trend + swing + chop + micro)

    def symbol_spec(self, symbol: str) -> SymbolSpec:
        d = _spec_for(symbol)
        point = 10 ** (-d["digits"])
        return SymbolSpec(name=symbol, digits=d["digits"], point=point,
                          contract_size=d["contract"], tick_value=d["tick_value"],
                          tick_size=point, volume_min=0.01, volume_max=100.0,
                          volume_step=0.01, stops_level=0)

    def tick(self, symbol: str) -> Tick:
        d = _spec_for(symbol)
        point = 10 ** (-d["digits"])
        mid = self._base_price(symbol)
        half = d["spread_pts"] * point / 2
        return Tick(symbol=symbol, bid=round(mid - half, d["digits"]),
                    ask=round(mid + half, d["digits"]), time=time.time())

    def bars(self, symbol: str, timeframe: str, count: int) -> list[Bar]:
        mins = TF_MINUTES.get(timeframe.upper(), 60)
        d = _spec_for(symbol)
        now = time.time()
        step = mins * 60
        out: list[Bar] = []
        for i in range(count, 0, -1):
            t0 = now - i * step
            o = self._base_price(symbol, t0)
            c = self._base_price(symbol, t0 + step * 0.98)
            mid_a = self._base_price(symbol, t0 + step * 0.33)
            mid_b = self._base_price(symbol, t0 + step * 0.66)
            hi = max(o, c, mid_a, mid_b) + d["vol"] * 0.25
            lo = min(o, c, mid_a, mid_b) - d["vol"] * 0.25
            out.append(Bar(time=t0, open=round(o, d["digits"]), high=round(hi, d["digits"]),
                           low=round(lo, d["digits"]), close=round(c, d["digits"]),
                           volume=500 + (i * 37) % 900))
        return out

    # ---------------- account ----------------
    def _mark_to_market(self) -> float:
        total = 0.0
        for p in self._positions.values():
            spec = self.symbol_spec(p.symbol)
            t = self.tick(p.symbol)
            px = t.bid if p.side == "buy" else t.ask
            diff = (px - p.entry) if p.side == "buy" else (p.entry - px)
            profit = diff / spec.tick_size * spec.tick_value * p.lots
            p.profit = round(profit, 2)
            total += profit
        return total

    def account(self) -> AccountInfo:
        floating = self._mark_to_market()
        used_margin = sum(p.lots * 1000 for p in self._positions.values())
        return AccountInfo(login=self.id, platform=self.platform, currency="USD",
                           balance=round(self._balance, 2),
                           equity=round(self._balance + floating, 2),
                           margin=round(used_margin, 2),
                           free_margin=round(self._balance + floating - used_margin, 2),
                           leverage=100, server="SimServer", company="Agentic Paper Engine")

    def positions(self) -> list[Position]:
        self._mark_to_market()
        return list(self._positions.values())

    # ---------------- orders ----------------
    def market_order(self, req: OrderRequest) -> OrderResult:
        if not self._connected:
            return OrderResult(ok=False, message="not connected")
        spec = self.symbol_spec(req.symbol)
        t = self.tick(req.symbol)
        price = t.ask if req.side == "buy" else t.bid
        lots = max(spec.volume_min, round(req.lots / spec.volume_step) * spec.volume_step)
        self._ticket += 1
        ticket = str(self._ticket)
        self._positions[ticket] = Position(
            ticket=ticket, symbol=req.symbol, side=req.side, lots=round(lots, 2),
            entry=price, stop=req.stop, take=req.take, open_time=time.time(),
            comment=req.comment)
        return OrderResult(ok=True, ticket=ticket, price=price, message="filled (paper)")

    def close_position(self, ticket: str) -> OrderResult:
        p = self._positions.get(str(ticket))
        if not p:
            return OrderResult(ok=False, message=f"no position {ticket}")
        spec = self.symbol_spec(p.symbol)
        t = self.tick(p.symbol)
        px = t.bid if p.side == "buy" else t.ask
        diff = (px - p.entry) if p.side == "buy" else (p.entry - px)
        pnl = diff / spec.tick_size * spec.tick_value * p.lots
        self._balance += pnl
        self._closed_pnl.append(pnl)
        del self._positions[str(ticket)]
        return OrderResult(ok=True, ticket=str(ticket), price=px,
                           message=f"closed pnl={pnl:.2f}", raw={"pnl": pnl})

    def modify_position(self, ticket: str, stop: float, take: float) -> OrderResult:
        p = self._positions.get(str(ticket))
        if not p:
            return OrderResult(ok=False, message=f"no position {ticket}")
        p.stop, p.take = stop, take
        return OrderResult(ok=True, ticket=str(ticket), message="modified")

    # ---------------- housekeeping ----------------
    def sweep_stops(self) -> list[tuple[str, float, float]]:
        """Close positions whose stop or target was touched. Returns (ticket, px, pnl)."""
        hits: list[tuple[str, float, float]] = []
        for ticket, p in list(self._positions.items()):
            t = self.tick(p.symbol)
            px = t.bid if p.side == "buy" else t.ask
            hit = False
            if p.side == "buy":
                if p.stop and px <= p.stop:
                    hit = True
                elif p.take and px >= p.take:
                    hit = True
            else:
                if p.stop and px >= p.stop:
                    hit = True
                elif p.take and px <= p.take:
                    hit = True
            if hit:
                res = self.close_position(ticket)
                hits.append((ticket, res.price, float(res.raw.get("pnl", 0.0))))
        return hits
