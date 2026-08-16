"""MetaTrader 5 back end using the official `MetaTrader5` python package.

Requires a running MT5 terminal on the same machine (Windows, or Linux/macOS
under Wine). MT5 only - MT4 has no python API, use the file bridge for MT4.

    pip install MetaTrader5
"""
from __future__ import annotations

import time
from typing import Any

from .base import (AccountInfo, Bar, OrderRequest, OrderResult, Position,
                   SymbolSpec, Tick)

try:  # optional dependency
    import MetaTrader5 as mt5  # type: ignore
except Exception:  # pragma: no cover - not installed off-Windows
    mt5 = None  # type: ignore


class MT5NativeBroker:
    platform = "MT5"

    def __init__(self, account_id: str = "mt5", login: int = 0, password: str = "",
                 server: str = "", terminal_path: str = "", magic: int = 770420):
        self.id = account_id
        self.login = int(login or 0)
        self.password = password
        self.server = server
        self.terminal_path = terminal_path
        self.magic = magic
        self._connected = False

    # ---------------- lifecycle ----------------
    def connect(self) -> bool:
        if mt5 is None:
            raise RuntimeError(
                "MetaTrader5 package not installed. `pip install MetaTrader5` "
                "(Windows/Wine only), or use broker.kind: mt5_bridge instead.")
        kwargs: dict[str, Any] = {}
        if self.terminal_path:
            kwargs["path"] = self.terminal_path
        if self.login:
            kwargs.update(login=self.login, password=self.password, server=self.server)
        ok = mt5.initialize(**kwargs)
        if not ok:
            raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
        self._connected = True
        return True

    def disconnect(self) -> None:
        if mt5 is not None and self._connected:
            mt5.shutdown()
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    # ---------------- data ----------------
    def _tf(self, timeframe: str):
        return {
            "M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15,
            "M30": mt5.TIMEFRAME_M30, "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4,
            "D1": mt5.TIMEFRAME_D1, "W1": mt5.TIMEFRAME_W1,
        }.get(timeframe.upper(), mt5.TIMEFRAME_H1)

    def account(self) -> AccountInfo:
        a = mt5.account_info()
        if a is None:
            raise RuntimeError("no account info")
        return AccountInfo(login=str(a.login), platform="MT5", currency=a.currency,
                           balance=a.balance, equity=a.equity, margin=a.margin,
                           free_margin=a.margin_free, leverage=a.leverage,
                           server=a.server, company=a.company)

    def symbol_spec(self, symbol: str) -> SymbolSpec:
        info = mt5.symbol_info(symbol)
        if info is None:
            mt5.symbol_select(symbol, True)
            info = mt5.symbol_info(symbol)
        if info is None:
            raise RuntimeError(f"unknown symbol {symbol}")
        if not info.visible:
            mt5.symbol_select(symbol, True)
        return SymbolSpec(name=symbol, digits=info.digits, point=info.point,
                          contract_size=info.trade_contract_size,
                          tick_value=info.trade_tick_value, tick_size=info.trade_tick_size,
                          volume_min=info.volume_min, volume_max=info.volume_max,
                          volume_step=info.volume_step, stops_level=info.trade_stops_level)

    def tick(self, symbol: str) -> Tick:
        t = mt5.symbol_info_tick(symbol)
        if t is None:
            mt5.symbol_select(symbol, True)
            t = mt5.symbol_info_tick(symbol)
        if t is None:
            raise RuntimeError(f"no tick for {symbol}")
        return Tick(symbol=symbol, bid=t.bid, ask=t.ask, time=float(t.time))

    def bars(self, symbol: str, timeframe: str, count: int) -> list[Bar]:
        rates = mt5.copy_rates_from_pos(symbol, self._tf(timeframe), 0, count)
        if rates is None:
            return []
        return [Bar(time=float(r["time"]), open=float(r["open"]), high=float(r["high"]),
                    low=float(r["low"]), close=float(r["close"]),
                    volume=float(r["tick_volume"])) for r in rates]

    def positions(self) -> list[Position]:
        pos = mt5.positions_get() or []
        out = []
        for p in pos:
            if self.magic and p.magic and p.magic != self.magic:
                continue
            out.append(Position(
                ticket=str(p.ticket), symbol=p.symbol,
                side="buy" if p.type == mt5.POSITION_TYPE_BUY else "sell",
                lots=p.volume, entry=p.price_open, stop=p.sl, take=p.tp,
                profit=p.profit, open_time=float(p.time), comment=p.comment))
        return out

    # ---------------- orders ----------------
    def market_order(self, req: OrderRequest) -> OrderResult:
        spec = self.symbol_spec(req.symbol)
        t = self.tick(req.symbol)
        price = t.ask if req.side == "buy" else t.bid
        order_type = mt5.ORDER_TYPE_BUY if req.side == "buy" else mt5.ORDER_TYPE_SELL
        lots = max(spec.volume_min,
                   min(spec.volume_max,
                       round(req.lots / spec.volume_step) * spec.volume_step))
        request = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": req.symbol, "volume": float(lots),
            "type": order_type, "price": price, "deviation": req.deviation,
            "magic": req.magic or self.magic, "comment": req.comment[:31],
            "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC,
        }
        if req.stop:
            request["sl"] = round(req.stop, spec.digits)
        if req.take:
            request["tp"] = round(req.take, spec.digits)

        result = mt5.order_send(request)
        if result is None:
            return OrderResult(ok=False, message=f"order_send returned None {mt5.last_error()}")
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            # retry once with the broker's other filling mode
            request["type_filling"] = mt5.ORDER_FILLING_FOK
            result = mt5.order_send(request)
        ok = result is not None and result.retcode == mt5.TRADE_RETCODE_DONE
        return OrderResult(ok=ok, ticket=str(getattr(result, "order", "")),
                           price=float(getattr(result, "price", price) or price),
                           message=f"retcode={getattr(result, 'retcode', '?')} "
                                   f"{getattr(result, 'comment', '')}",
                           raw={"retcode": getattr(result, "retcode", None)})

    def close_position(self, ticket: str) -> OrderResult:
        pos = mt5.positions_get(ticket=int(ticket))
        if not pos:
            return OrderResult(ok=False, message=f"no position {ticket}")
        p = pos[0]
        spec = self.symbol_spec(p.symbol)
        t = self.tick(p.symbol)
        is_buy = p.type == mt5.POSITION_TYPE_BUY
        request = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": p.symbol, "volume": p.volume,
            "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
            "position": int(ticket), "price": t.bid if is_buy else t.ask,
            "deviation": 20, "magic": self.magic, "comment": "agentic close",
            "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC,
        }
        r = mt5.order_send(request)
        ok = r is not None and r.retcode == mt5.TRADE_RETCODE_DONE
        return OrderResult(ok=ok, ticket=str(ticket),
                           price=float(getattr(r, "price", 0.0) or 0.0),
                           message=f"retcode={getattr(r, 'retcode', '?')}")

    def modify_position(self, ticket: str, stop: float, take: float) -> OrderResult:
        pos = mt5.positions_get(ticket=int(ticket))
        if not pos:
            return OrderResult(ok=False, message=f"no position {ticket}")
        p = pos[0]
        spec = self.symbol_spec(p.symbol)
        r = mt5.order_send({
            "action": mt5.TRADE_ACTION_SLTP, "symbol": p.symbol, "position": int(ticket),
            "sl": round(stop, spec.digits), "tp": round(take, spec.digits),
            "magic": self.magic,
        })
        ok = r is not None and r.retcode == mt5.TRADE_RETCODE_DONE
        return OrderResult(ok=ok, ticket=str(ticket),
                           message=f"retcode={getattr(r, 'retcode', '?')}")
