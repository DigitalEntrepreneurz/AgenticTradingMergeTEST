"""Broker factory - builds the right back end from config."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import (AccountInfo, Bar, Broker, OrderRequest, OrderResult,  # noqa: F401
                   Position, SymbolSpec, Tick, TF_MINUTES, TIMEFRAMES)
from .mt_bridge import MTBridgeBroker
from .mt5_native import MT5NativeBroker
from .simulated import SimulatedBroker

ROOT = Path(__file__).resolve().parent.parent.parent


def build_broker(account: dict[str, Any], cfg) -> Broker:
    """account: one entry from broker.accounts; cfg: firm Config."""
    kind = str(account.get("kind") or cfg.get("broker.kind", "simulated")).lower()
    acc_id = str(account.get("id", "default"))
    platform = str(account.get("platform", "MT5")).upper()

    if kind == "simulated":
        return SimulatedBroker(account_id=acc_id, platform=platform,
                               starting_balance=float(account.get("starting_balance", 10_000)))

    if kind == "mt5_native":
        n = cfg.get("broker.mt5_native", {}) or {}
        return MT5NativeBroker(account_id=acc_id,
                               login=int(account.get("login", n.get("login", 0)) or 0),
                               password=account.get("password", n.get("password", "")),
                               server=account.get("server", n.get("server", "")),
                               terminal_path=account.get("terminal_path",
                                                         n.get("terminal_path", "")),
                               magic=int(n.get("magic", 770420)))

    if kind in ("mt4_bridge", "mt5_bridge"):
        b = cfg.get("broker.bridge", {}) or {}
        files_dir = account.get("files_dir") or b.get("files_dir", "./bridge")
        if not Path(files_dir).is_absolute():
            files_dir = str((ROOT / files_dir).resolve())
        return MTBridgeBroker(account_id=acc_id, files_dir=files_dir,
                              platform="MT4" if kind == "mt4_bridge" else "MT5",
                              magic=int(b.get("magic", 770420)),
                              poll_seconds=float(b.get("poll_seconds", 0.25)),
                              request_timeout=float(b.get("request_timeout", 20)))

    raise ValueError(f"unknown broker kind '{kind}'")


def build_all(cfg) -> dict[str, Broker]:
    out: dict[str, Broker] = {}
    for acc in cfg.accounts():
        try:
            br = build_broker(acc, cfg)
            br.connect()
            out[br.id] = br
        except Exception as e:   # a dead terminal must not kill the firm
            print(f"[broker] could not connect '{acc.get('id')}': {e}")
    return out
