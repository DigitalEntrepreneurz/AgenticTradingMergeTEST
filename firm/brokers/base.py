"""Broker interface shared by the simulated engine, MT4 and MT5 back ends."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class Bar:
    time: float
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class Tick:
    symbol: str
    bid: float
    ask: float
    time: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2


@dataclass
class AccountInfo:
    login: str = ""
    platform: str = "MT5"      # MT4 | MT5 | SIM
    currency: str = "USD"
    balance: float = 0.0
    equity: float = 0.0
    margin: float = 0.0
    free_margin: float = 0.0
    leverage: int = 100
    server: str = ""
    company: str = ""


@dataclass
class Position:
    ticket: str
    symbol: str
    side: str                  # buy | sell
    lots: float
    entry: float
    stop: float = 0.0
    take: float = 0.0
    profit: float = 0.0
    open_time: float = 0.0
    comment: str = ""


@dataclass
class OrderRequest:
    symbol: str
    side: str                  # buy | sell
    lots: float
    stop: float = 0.0
    take: float = 0.0
    comment: str = "agentic"
    magic: int = 770420
    deviation: int = 20


@dataclass
class OrderResult:
    ok: bool
    ticket: str = ""
    price: float = 0.0
    message: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class SymbolSpec:
    """Contract details needed to size a position correctly."""
    name: str
    digits: int = 5
    point: float = 0.00001
    contract_size: float = 100_000.0
    tick_value: float = 1.0        # account currency per tick per 1.0 lot
    tick_size: float = 0.00001
    volume_min: float = 0.01
    volume_max: float = 100.0
    volume_step: float = 0.01
    stops_level: int = 0           # min stop distance, in points


class Broker(Protocol):
    """Every back end implements this. Agents never touch a terminal directly."""

    id: str
    platform: str

    def connect(self) -> bool: ...
    def disconnect(self) -> None: ...
    def is_connected(self) -> bool: ...
    def account(self) -> AccountInfo: ...
    def symbol_spec(self, symbol: str) -> SymbolSpec: ...
    def tick(self, symbol: str) -> Tick: ...
    def bars(self, symbol: str, timeframe: str, count: int) -> list[Bar]: ...
    def positions(self) -> list[Position]: ...
    def market_order(self, req: OrderRequest) -> OrderResult: ...
    def close_position(self, ticket: str) -> OrderResult: ...
    def modify_position(self, ticket: str, stop: float, take: float) -> OrderResult: ...


TIMEFRAMES = ["M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1"]

TF_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30,
              "H1": 60, "H4": 240, "D1": 1440, "W1": 10080}
