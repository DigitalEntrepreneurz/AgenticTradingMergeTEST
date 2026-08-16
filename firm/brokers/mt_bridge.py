"""File-based bridge to MetaTrader 4 **and** MetaTrader 5.

MT4 has no Python API and MT5's API is Windows-only, so this back end talks to
an Expert Advisor (mql/ArenaBridge.mq4 / .mq5) through JSON files in the
terminal's sandboxed `MQL4/Files` / `MQL5/Files` directory. Works on every OS,
including a terminal running under Wine or on another machine via a synced
folder.

Protocol
--------
  request_<id>.json   written by python  ->  read by the EA
  response_<id>.json  written by the EA  ->  read by python
  state.json          heartbeat: account, positions, ticks (EA -> python)

Every request is {"id":..., "cmd":..., "params": {...}}; every response is
{"id":..., "ok": bool, "data": {...}, "error": "..."}.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from .base import (AccountInfo, Bar, OrderRequest, OrderResult, Position,
                   SymbolSpec, Tick)


class BridgeTimeout(RuntimeError):
    pass


class MTBridgeBroker:
    """Shared by MT4 and MT5 - only `platform` differs."""

    def __init__(self, account_id: str = "mt", files_dir: str = "./bridge",
                 platform: str = "MT5", magic: int = 770420,
                 poll_seconds: float = 0.25, request_timeout: float = 20.0):
        self.id = account_id
        self.platform = platform.upper()
        self.dir = Path(files_dir).expanduser().resolve()
        self.magic = magic
        self.poll = poll_seconds
        self.timeout = request_timeout
        self._connected = False

    # ---------------- transport ----------------
    def _call(self, cmd: str, params: dict[str, Any] | None = None,
              timeout: float | None = None) -> dict[str, Any]:
        self.dir.mkdir(parents=True, exist_ok=True)
        rid = uuid.uuid4().hex[:12]
        payload = {"id": rid, "cmd": cmd, "params": params or {},
                   "ts": time.time(), "magic": self.magic}
        req = self.dir / f"request_{rid}.json"
        tmp = self.dir / f".tmp_{rid}"
        tmp.write_text(json.dumps(payload))
        tmp.rename(req)                       # atomic for the EA's file lock

        resp = self.dir / f"response_{rid}.json"
        deadline = time.time() + (timeout or self.timeout)
        while time.time() < deadline:
            if resp.exists():
                for _ in range(5):            # tolerate a partial write
                    try:
                        data = json.loads(resp.read_text())
                        break
                    except (json.JSONDecodeError, OSError):
                        time.sleep(0.05)
                else:
                    data = {"ok": False, "error": "unreadable response"}
                resp.unlink(missing_ok=True)
                req.unlink(missing_ok=True)
                if not data.get("ok", False):
                    raise RuntimeError(f"{cmd} failed: {data.get('error', 'unknown')}")
                return data.get("data", {})
            time.sleep(self.poll)
        req.unlink(missing_ok=True)
        raise BridgeTimeout(
            f"No response to '{cmd}' within {timeout or self.timeout}s. "
            f"Is ArenaBridge attached to a chart in {self.platform} and is "
            f"'Allow Algo Trading' enabled? Watching: {self.dir}")

    def _state(self) -> dict[str, Any]:
        f = self.dir / "state.json"
        if not f.exists():
            raise BridgeTimeout(f"no state.json in {self.dir} - EA not running?")
        try:
            return json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            time.sleep(0.1)
            return json.loads(f.read_text())

    # ---------------- lifecycle ----------------
    def connect(self) -> bool:
        self.dir.mkdir(parents=True, exist_ok=True)
        info = self._call("ping", timeout=self.timeout)
        detected = str(info.get("platform", self.platform)).upper()
        if detected in ("MT4", "MT5"):
            self.platform = detected
        self._connected = True
        return True

    def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        if not self._connected:
            return False
        try:
            st = self._state()
            return (time.time() - float(st.get("ts", 0))) < 60
        except Exception:
            return False

    # ---------------- data ----------------
    def account(self) -> AccountInfo:
        d = self._call("account")
        return AccountInfo(login=str(d.get("login", "")), platform=self.platform,
                           currency=d.get("currency", "USD"),
                           balance=float(d.get("balance", 0)),
                           equity=float(d.get("equity", 0)),
                           margin=float(d.get("margin", 0)),
                           free_margin=float(d.get("free_margin", 0)),
                           leverage=int(d.get("leverage", 100)),
                           server=d.get("server", ""), company=d.get("company", ""))

    def symbol_spec(self, symbol: str) -> SymbolSpec:
        d = self._call("symbol", {"symbol": symbol})
        return SymbolSpec(name=symbol, digits=int(d.get("digits", 5)),
                          point=float(d.get("point", 1e-5)),
                          contract_size=float(d.get("contract_size", 100000)),
                          tick_value=float(d.get("tick_value", 1.0)),
                          tick_size=float(d.get("tick_size", d.get("point", 1e-5))),
                          volume_min=float(d.get("volume_min", 0.01)),
                          volume_max=float(d.get("volume_max", 100)),
                          volume_step=float(d.get("volume_step", 0.01)),
                          stops_level=int(d.get("stops_level", 0)))

    def tick(self, symbol: str) -> Tick:
        d = self._call("tick", {"symbol": symbol})
        return Tick(symbol=symbol, bid=float(d["bid"]), ask=float(d["ask"]),
                    time=float(d.get("time", time.time())))

    def bars(self, symbol: str, timeframe: str, count: int) -> list[Bar]:
        d = self._call("bars", {"symbol": symbol, "timeframe": timeframe.upper(),
                                "count": int(count)}, timeout=max(self.timeout, 30))
        return [Bar(time=float(b["t"]), open=float(b["o"]), high=float(b["h"]),
                    low=float(b["l"]), close=float(b["c"]), volume=float(b.get("v", 0)))
                for b in d.get("bars", [])]

    def positions(self) -> list[Position]:
        d = self._call("positions")
        return [Position(ticket=str(p["ticket"]), symbol=p["symbol"], side=p["side"],
                         lots=float(p["lots"]), entry=float(p["entry"]),
                         stop=float(p.get("stop", 0)), take=float(p.get("take", 0)),
                         profit=float(p.get("profit", 0)),
                         open_time=float(p.get("open_time", 0)),
                         comment=p.get("comment", ""))
                for p in d.get("positions", [])]

    # ---------------- orders ----------------
    def market_order(self, req: OrderRequest) -> OrderResult:
        d = self._call("order", {
            "symbol": req.symbol, "side": req.side, "lots": round(req.lots, 2),
            "stop": req.stop, "take": req.take, "comment": req.comment[:31],
            "magic": req.magic or self.magic, "deviation": req.deviation,
        }, timeout=max(self.timeout, 30))
        return OrderResult(ok=bool(d.get("ok", True)), ticket=str(d.get("ticket", "")),
                           price=float(d.get("price", 0)),
                           message=d.get("message", ""), raw=d)

    def close_position(self, ticket: str) -> OrderResult:
        d = self._call("close", {"ticket": str(ticket)}, timeout=max(self.timeout, 30))
        return OrderResult(ok=bool(d.get("ok", True)), ticket=str(ticket),
                           price=float(d.get("price", 0)),
                           message=d.get("message", ""), raw=d)

    def modify_position(self, ticket: str, stop: float, take: float) -> OrderResult:
        d = self._call("modify", {"ticket": str(ticket), "stop": stop, "take": take})
        return OrderResult(ok=bool(d.get("ok", True)), ticket=str(ticket),
                           message=d.get("message", ""), raw=d)
