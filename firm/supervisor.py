"""Auto-quarantine: the firm's ability to fire its own strategies.

Promotion was already automatic - `backtest.py` moves a strategy from
`proposed` to `approved` when it clears verdict() and walk-forward. Demotion
was not. Nothing in the codebase could ever remove `approved`, so a strategy
that passed once traded forever, and `execution.scan()` picked it up on every
tick regardless of how badly it was performing live.

This module closes that loop. It reads the drift report, and when a strategy
is measurably broken it flips the status to `quarantined`. Because
`execution.scan()` only ever queries `strategies("approved")`, that single
status change stops new entries immediately - no other code path needs to
know about quarantine for it to take effect.

Three deliberate constraints:

1.  **Demo trades never trigger a quarantine.** The seeded demo dataset would
    otherwise retire strategies on synthetic evidence. Auto-action runs on
    real fills only; the dashboard may still *display* demo-inclusive drift.
2.  **Quarantine does not touch open positions.** Pulling a strategy stops it
    opening new risk; liquidating its existing book is a separate, larger
    decision that belongs to the risk agent's kill switch, not to a
    statistical drift reading.
3.  **Quarantine is sticky and needs a human (or a fresh backtest) to undo.**
    A quarantined strategy stops trading, so it can never generate the
    evidence that would clear it. Recovery is therefore explicit:
    `reinstate()` from the board, or a new passing backtest.
"""

from __future__ import annotations

import time
from typing import Any

from .drift import report as drift_report

# A strategy must be broken by BOTH the statistical test and the size test
# before it loses its job. `drift.classify` already enforces p < ALPHA and
# |delta| >= DRIFT_R for BROKEN; we additionally demand a minimum sample so a
# short unlucky run cannot retire a good system.
MIN_TRADES_TO_ACT = 12
QUARANTINE = "quarantined"
APPROVED = "approved"


def _cfg(cfg: Any, key: str, default: Any) -> Any:
    if cfg is None:
        return default
    got = cfg.get(f"supervision.{key}", None)
    return default if got is None else got


def candidates(memory: Any, include_demo: bool = False,
               min_trades: int = MIN_TRADES_TO_ACT,
               quarantine_no_baseline: bool = False) -> list[dict]:
    """Approved strategies that have earned a demotion, with the evidence."""
    rep = drift_report(memory, include_demo=include_demo)
    approved = {r["name"]: r for r in memory.strategies(APPROVED)}
    out: list[dict] = []

    for row in rep.get("rows", []):
        key = row.get("key")
        if key not in approved:
            continue                      # not trading, nothing to withdraw
        if row.get("status") != "BROKEN":
            continue
        if int(row.get("trades") or 0) < min_trades:
            continue                      # real, but not yet enough evidence
        out.append({
            "name": key,
            "verdict": "BROKEN",
            "reason": row.get("reason", ""),
            "live_expectancy_r": row.get("live_expectancy_r"),
            "baseline_expectancy_r": row.get("baseline_expectancy_r"),
            "delta_r": row.get("delta_r"),
            "p_value": row.get("p_value"),
            "trades": row.get("trades"),
            "cusum": (row.get("cusum") or {}).get("worst"),
        })

    if quarantine_no_baseline:
        for row in rep.get("unmatched", []):
            key = row.get("key")
            if key not in approved:
                continue
            out.append({
                "name": key,
                "verdict": "NO_BASELINE",
                "reason": "approved and trading with no backtest to be judged against",
                "live_expectancy_r": row.get("live_expectancy_r"),
                "baseline_expectancy_r": None, "delta_r": None, "p_value": None,
                "trades": row.get("trades"), "cusum": None,
            })
    return out


def quarantine(memory: Any, name: str, evidence: dict, actor: str = "supervisor") -> bool:
    """Flip one strategy out of `approved`. Idempotent; returns True if changed."""
    rows = [r for r in memory.strategies() if r["name"] == name]
    if not rows:
        return False
    row = rows[0]
    if row.get("status") != APPROVED:
        return False

    note = (f"AUTO-QUARANTINED {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}: "
            f"{evidence.get('reason', 'failed live review')}")
    memory.upsert_strategy(
        name=name, spec=row.get("spec") or {}, source=row.get("source") or "",
        status=QUARANTINE, score=float(row.get("score") or 0.0),
        metrics=row.get("metrics") or {}, notes=note)

    memory.log("warn", actor,
               f"quarantined {name}: live {evidence.get('live_expectancy_r')}R vs "
               f"backtest {evidence.get('baseline_expectancy_r')}R "
               f"(delta {evidence.get('delta_r')}R, p={evidence.get('p_value')}, "
               f"n={evidence.get('trades')})",
               {"strategy": name, "action": "quarantine", **evidence})
    memory.remember(actor, "quarantine", name, note, evidence)
    return True


def reinstate(memory: Any, name: str, who: str = "board") -> bool:
    """Return a quarantined strategy to service. Deliberately manual."""
    rows = [r for r in memory.strategies() if r["name"] == name]
    if not rows or rows[0].get("status") != QUARANTINE:
        return False
    row = rows[0]
    memory.upsert_strategy(
        name=name, spec=row.get("spec") or {}, source=row.get("source") or "",
        status=APPROVED, score=float(row.get("score") or 0.0),
        metrics=row.get("metrics") or {},
        notes=f"reinstated by {who} {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}")
    memory.log("warn", who, f"reinstated {name} to approved",
               {"strategy": name, "action": "reinstate"})
    return True


def review(memory: Any, cfg: Any = None, dry_run: bool = False,
           actor: str = "supervisor") -> dict:
    """One supervision pass. Safe to call every tick."""
    enabled = bool(_cfg(cfg, "auto_quarantine", True))
    include_demo = bool(_cfg(cfg, "include_demo", False))
    min_trades = int(_cfg(cfg, "min_trades", MIN_TRADES_TO_ACT))
    no_base = bool(_cfg(cfg, "quarantine_no_baseline", False))

    found = candidates(memory, include_demo=include_demo, min_trades=min_trades,
                       quarantine_no_baseline=no_base)
    acted: list[str] = []
    if enabled and not dry_run:
        for c in found:
            if quarantine(memory, c["name"], c, actor=actor):
                acted.append(c["name"])

    return {"enabled": enabled, "dry_run": dry_run, "include_demo": include_demo,
            "min_trades": min_trades, "candidates": found,
            "quarantined": acted, "checked_at": time.time()}


def status(memory: Any) -> dict:
    """Roster by status, for the dashboard and the CEO."""
    rows = memory.strategies()
    by: dict[str, list[dict]] = {}
    for r in rows:
        by.setdefault(r.get("status") or "unknown", []).append(
            {"name": r["name"], "score": r.get("score"), "notes": r.get("notes") or ""})
    return {"counts": {k: len(v) for k, v in by.items()}, "by_status": by,
            "quarantined": by.get(QUARANTINE, [])}
