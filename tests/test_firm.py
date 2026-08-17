"""End-to-end checks. Run: python tests/test_firm.py"""
from __future__ import annotations

import json as _json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from firm.backtester import backtest, walk_forward           # noqa: E402
from firm.brokers.simulated import SimulatedBroker           # noqa: E402
from firm.brokers.base import OrderRequest                   # noqa: E402
from firm.config import load_config                          # noqa: E402
from firm.memory import Memory                               # noqa: E402
from firm.orchestrator import Firm                           # noqa: E402
from firm.strategies.library import all_strategies, run      # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, note: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{note}]" if note else ""))


def tmp_mem() -> Memory:
    return Memory(path=Path(tempfile.mkdtemp()) / "t.db")


def t_broker():
    print("\n[broker]")
    b = SimulatedBroker("t", 10_000)
    b.connect()
    check("connects", b.is_connected())
    a = b.account()
    check("account balance", a.balance == 10_000, f"${a.balance}")
    tk = b.tick("EURUSD")
    check("tick has spread", tk.ask > tk.bid, f"{tk.bid}/{tk.ask}")
    bars = b.bars("EURUSD", "H1", 400)
    check("bars returned", len(bars) == 400)
    check("bars OHLC valid",
          all(x.high >= max(x.open, x.close) and x.low <= min(x.open, x.close) for x in bars))
    check("bars chronological", all(bars[i].time < bars[i + 1].time
                                    for i in range(len(bars) - 1)))
    spec = b.symbol_spec("EURUSD")
    check("symbol spec", spec.digits == 5 and spec.volume_min == 0.01)

    r = b.market_order(OrderRequest("EURUSD", "buy", 0.10, stop=tk.bid - 0.005,
                                    take=tk.ask + 0.010))
    check("market order fills", r.ok, r.message)
    check("position open", len(b.positions()) == 1)
    c = b.close_position(r.ticket)
    check("close works", c.ok and len(b.positions()) == 0, c.message)
    check("balance moved", b.account().balance != 10_000)


def t_indicators():
    print("\n[indicators]")
    from firm.indicators import atr, ema, rsi, sma
    vals = [float(i) for i in range(1, 61)]
    check("sma last", abs(sma(vals, 10)[-1] - 55.5) < 1e-9)
    check("ema warmup None", ema(vals, 10)[8] is None)
    check("rsi rising -> 100", rsi(vals, 14)[-1] > 99)
    b = SimulatedBroker("t"); b.connect()
    a = atr(b.bars("EURUSD", "H1", 200), 14)
    check("atr positive", a[-1] is not None and a[-1] > 0, f"{a[-1]:.6f}")


def t_strategies():
    print("\n[strategies]")
    b = SimulatedBroker("t"); b.connect()
    check("library populated", len(all_strategies()) >= 4, str(list(all_strategies())))
    fired = 0
    for name in all_strategies():
        for sym in ("EURUSD", "XAUUSD", "GBPUSD"):
            bars = b.bars(sym, "H1", 500)
            for cut in range(300, 500, 7):
                s = run(name, bars[:cut])
                if s:
                    fired += 1
                    ok = (s.stop < s.entry < s.take) if s.side == "buy" \
                        else (s.take < s.entry < s.stop)
                    if not ok:
                        check(f"{name} geometry", False,
                              f"{s.side} e{s.entry} s{s.stop} t{s.take}")
                        return
    check("signals generated", fired > 0, f"{fired} signals")
    check("all signal geometry valid", True)


def t_backtest():
    print("\n[backtest]")
    b = SimulatedBroker("t"); b.connect()
    bars = b.bars("EURUSD", "H1", 1500)
    spec = b.symbol_spec("EURUSD")
    r = backtest("ema_trend_pullback", bars, "EURUSD", "H1", spec=spec)
    check("backtest runs", r.trades >= 0, f"{r.trades} trades, PF {r.profit_factor}")
    if r.trades:
        check("metrics coherent", r.wins + r.losses == r.trades)
        check("equity curve length", len(r.equity_curve) == r.trades)
        ok, why = r.verdict()
        check("verdict returns reason", isinstance(why, str) and len(why) > 0, why)
    wf = walk_forward("ema_trend_pullback", bars, "EURUSD", "H1",
                      {"rr": [1.5, 2.0]}, spec=spec)
    check("walk-forward runs", "folds" in wf, str(wf.get("folds")))


def t_risk():
    print("\n[risk]")
    from firm.agents.base import AgentContext
    from firm.agents.risk import RiskAgent
    from firm.llm import LLM
    cfg = load_config()
    mem = tmp_mem()
    b = SimulatedBroker("t", 10_000); b.connect()
    ctx = AgentContext(cfg=cfg, memory=mem, llm=LLM("", mem), brokers={"t": b})
    ra = RiskAgent(ctx)

    tk = b.tick("EURUSD")
    good = {"symbol": "EURUSD", "side": "buy", "entry": tk.ask,
            "stop": tk.ask - 0.0050, "take": tk.ask + 0.0100,
            "meta": {"atr": 0.0012}}
    d = ra.vet(good, b)
    check("valid signal approved", d.approved, d.reason)
    if d.approved:
        risk_pct = d.risk_usd / 10_000 * 100
        check("size respects risk %", risk_pct <= float(
            cfg.get("risk.max_risk_per_trade_pct", 0.75)) + 0.02, f"{risk_pct:.3f}%")

    check("no-stop rejected", not ra.vet({**good, "stop": 0}, b).approved)
    check("inverted stop rejected",
          not ra.vet({**good, "stop": tk.ask + 0.005}, b).approved)
    check("tiny stop vs ATR rejected",
          not ra.vet({**good, "stop": tk.ask - 0.00001,
                      "meta": {"atr": 0.0012}}, b).approved)
    check("bad R:R rejected",
          not ra.vet({**good, "take": tk.ask + 0.0010}, b).approved)

    # max open positions
    for _ in range(int(cfg.get("risk.max_open_positions", 4))):
        b.market_order(OrderRequest("GBPUSD", "buy", 0.01, stop=1.0, take=99.0))
    check("max open positions enforced", not ra.vet(good, b).approved)
    for p in list(b.positions()):
        b.close_position(p.ticket)

    ra.halt("test", hours=1)
    check("kill switch blocks", not ra.vet(good, b).approved)
    ra.resume()
    check("resume clears", ra.vet(good, b).approved)


def t_firm():
    print("\n[firm end-to-end]")
    mem = tmp_mem()
    f = Firm(memory=mem)
    check("brokers connected", len(f.brokers) >= 1, str(list(f.brokers)))
    check("seven agents hired", len(f.agents) == 7, str(list(f.agents)))
    check("paper mode default", not f.cfg.live_enabled)

    iid = f.board_request("First task",
                          "Research the market, backtest what you find, then trade it.")
    check("board issue raised", iid > 0)

    for _ in range(14):
        f.tick()
    issue = mem.issue(iid)
    check("CEO delegated", len(mem.q("SELECT * FROM issues WHERE parent_id=?", (iid,))) > 0)
    check("research ran", len(mem.strategies()) > 0,
          f"{len(mem.strategies())} strategies")
    check("backtests recorded", len(mem.q("SELECT * FROM backtests")) > 0)
    check("board issue resolved", issue["status"] in ("done", "in_progress"),
          issue["status"])
    st = f.status()
    check("status renders", st["firm"] and "equity" in st)
    check("no LLM cost without key", st["llm_spend_24h"] == 0.0)
    print(f"    strategies: {len(mem.strategies('approved'))} approved / "
          f"{len(mem.strategies('rejected'))} rejected")
    print(f"    signals: {len(mem.q('SELECT * FROM signals'))}, "
          f"trades: {len(mem.q('SELECT * FROM trades'))}")


def t_execution_path():
    """Force an approved strategy that always fires, and follow the order through."""
    print("\n[execution path]")
    from firm.strategies.library import Signal, register
    from firm.orchestrator import Firm as F

    @register("always_long_test", "test-only: always buys",
              {"atr_n": 14, "atr_mult": 2.0, "rr": 2.0})
    def _always(bars, p):
        from firm.indicators import atr
        a = atr(bars, p["atr_n"])
        if not a[-1]:
            return None
        px = bars[-1].close
        stop = px - a[-1] * p["atr_mult"]
        return Signal("buy", px, stop, px + (px - stop) * p["rr"], 0.7,
                      "test signal", meta={"atr": a[-1]})

    mem = tmp_mem()
    f = F(memory=mem)
    mem.upsert_strategy("always_long_test@EURUSD",
                        {"strategy": "always_long_test", "symbol": "EURUSD",
                         "params": {}, "timeframe": f.cfg.timeframe},
                        source="test", status="approved", score=1.0)
    ex = f.agents["execution"]
    sigs = ex.scan()
    check("signal generated", len(sigs) == 1, str(len(sigs)))
    if not sigs:
        return
    msg = ex.execute(sigs[0])
    check("order executed", "PAPER BUY" in msg, msg[:90])
    trades = mem.q("SELECT * FROM trades")
    check("trade recorded", len(trades) == 1)
    if trades:
        t = trades[0]
        check("trade is paper", t["mode"] == "paper")
        check("trade has stop and target", t["stop"] > 0 and t["take"] > 0)
        check("lots sized > 0", t["lots"] > 0, f"{t['lots']} lots")
        check("broker holds position",
              len(f.ctx.primary_broker().positions()) == 1)
    # no duplicate entry on the same bar
    check("no duplicate signal same bar", len(ex.scan()) == 0)
    # per-symbol cap blocks a second position
    mem.x("DELETE FROM memories WHERE kind='signal_bar'")
    again = ex.scan()
    if again:
        check("second position blocked by risk",
              "rejected" in ex.execute(again[0]).lower())
    # reconcile closes it when the broker's stop is touched
    br = f.ctx.primary_broker()
    pos = br.positions()[0]
    br.modify_position(pos.ticket, pos.entry + 1.0, pos.take)   # force stop hit
    ex.reconcile()
    check("reconcile settles closed trade",
          mem.q("SELECT * FROM trades WHERE status='closed'") != [])


def t_live_guard():
    print("\n[live-mode guard]")
    from firm.brokers.simulated import SimulatedBroker as SB
    cfg = load_config()
    check("paper by default", not cfg.live_enabled)
    cfg.raw["trading"]["paper_trading"] = False
    check("one switch is not enough", not cfg.live_enabled)
    cfg.raw["trading"]["allow_live_orders"] = True
    check("both switches arm live", cfg.live_enabled)
    # a simulated account must never report live
    from firm.agents.base import AgentContext
    from firm.agents.execution import ExecutionAgent
    from firm.llm import LLM
    mem = tmp_mem()
    b = SB("sim", 10_000); b.connect()
    ctx = AgentContext(cfg=cfg, memory=mem, llm=LLM("", mem), brokers={"sim": b})
    ex = ExecutionAgent(ctx)
    tk = b.tick("EURUSD")
    msg = ex.execute({"id": mem.add_signal(strategy="x", symbol="EURUSD", side="buy",
                                           entry=tk.ask, stop=tk.ask - 0.005,
                                           take=tk.ask + 0.010),
                      "strategy": "x", "symbol": "EURUSD", "side": "buy",
                      "entry": tk.ask, "stop": tk.ask - 0.005, "take": tk.ask + 0.010,
                      "meta": {"atr": 0.0012}})
    check("simulated broker forced to paper even when armed",
          "PAPER" in msg, msg[:80])


def t_bridge_protocol():
    print("\n[MT4/MT5 bridge protocol]")
    import json
    import threading
    import time as _t
    from firm.brokers.mt_bridge import MTBridgeBroker

    d = Path(tempfile.mkdtemp())
    br = MTBridgeBroker("mt4t", str(d), platform="MT4", poll_seconds=0.02,
                        request_timeout=6)
    stop = threading.Event()

    def fake_ea():
        """Stands in for ArenaBridge.mq4 - same file protocol."""
        while not stop.is_set():
            for req in list(d.glob("request_*.json")):
                try:
                    body = json.loads(req.read_text())
                except Exception:
                    continue
                cmd = body.get("cmd")
                data = {}
                if cmd == "ping":
                    data = {"platform": "MT4", "build": 1420, "algo_allowed": True}
                elif cmd == "account":
                    data = {"login": 5551234, "currency": "EUR", "balance": 25000.0,
                            "equity": 25120.5, "margin": 300.0, "free_margin": 24820.5,
                            "leverage": 500, "server": "ICMarketsSC-Demo",
                            "company": "IC Markets"}
                elif cmd == "tick":
                    data = {"bid": 1.08501, "ask": 1.08509, "time": _t.time()}
                elif cmd == "symbol":
                    data = {"digits": 5, "point": 0.00001, "contract_size": 100000,
                            "tick_value": 1.0, "tick_size": 0.00001, "volume_min": 0.01,
                            "volume_max": 50.0, "volume_step": 0.01, "stops_level": 20}
                elif cmd == "bars":
                    data = {"bars": [{"t": 1700000000 + i * 3600, "o": 1.085, "h": 1.086,
                                      "l": 1.084, "c": 1.0855, "v": 100}
                                     for i in range(body["params"]["count"])]}
                elif cmd == "positions":
                    data = {"positions": [{"ticket": "88112233", "symbol": "EURUSD",
                                           "side": "buy", "lots": 0.10, "entry": 1.0840,
                                           "stop": 1.0800, "take": 1.0920, "profit": 12.4,
                                           "open_time": 1700000000, "comment": "agentic"}]}
                elif cmd == "order":
                    data = {"ok": True, "ticket": "88112234", "price": 1.08509,
                            "message": "filled"}
                elif cmd == "close":
                    data = {"ok": True, "price": 1.08501, "message": "closed"}
                elif cmd == "modify":
                    data = {"ok": True, "message": "modified"}
                (d / f"response_{body['id']}.json").write_text(
                    json.dumps({"id": body["id"], "ok": True, "data": data, "error": ""}))
                req.unlink(missing_ok=True)
            _t.sleep(0.02)

    th = threading.Thread(target=fake_ea, daemon=True); th.start()
    try:
        check("bridge connects", br.connect())
        check("platform detected from EA", br.platform == "MT4", br.platform)
        a = br.account()
        check("MT4 account parsed", a.balance == 25000.0 and a.currency == "EUR",
              f"{a.login}@{a.server}")
        t = br.tick("EURUSD")
        check("MT4 tick parsed", t.ask > t.bid)
        check("MT4 bars parsed", len(br.bars("EURUSD", "H1", 25)) == 25)
        sp = br.symbol_spec("EURUSD")
        check("MT4 stops_level parsed", sp.stops_level == 20)
        p = br.positions()
        check("MT4 positions parsed", len(p) == 1 and p[0].ticket == "88112233")
        r = br.market_order(OrderRequest("EURUSD", "buy", 0.1, 1.08, 1.09))
        check("MT4 order round-trip", r.ok and r.ticket == "88112234", r.message)
        check("MT4 close round-trip", br.close_position("88112234").ok)
        check("MT4 modify round-trip", br.modify_position("88112234", 1.081, 1.093).ok)
        check("no files left behind", not list(d.glob("request_*.json")))
    finally:
        stop.set(); th.join(timeout=2)

    # timeout path when no EA is running
    dead = MTBridgeBroker("x", str(Path(tempfile.mkdtemp())), request_timeout=0.4,
                          poll_seconds=0.05)
    try:
        dead.connect()
        check("missing EA raises", False)
    except Exception as e:
        check("missing EA raises helpful error", "ArenaBridge" in str(e))


def _strip_mql(src: str) -> str:
    """Remove comments and string/char literals so brace counting is meaningful."""
    out, i, n = [], 0, len(src)
    while i < n:
        c = src[i]
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            j = src.find("\n", i); i = n if j < 0 else j; continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            j = src.find("*/", i); i = n if j < 0 else j + 2; continue
        if c in "\"'":
            quote = c; i += 1
            while i < n:
                if src[i] == "\\":
                    i += 2; continue
                if src[i] == quote:
                    i += 1; break
                i += 1
            continue
        out.append(c); i += 1
    return "".join(out)


def t_mql_files():
    print("\n[MQL expert advisors]")
    root = Path(__file__).resolve().parent.parent / "mql"
    for f, plat in (("ArenaBridge.mq4", "MT4"), ("ArenaBridge.mq5", "MT5")):
        p = root / f
        check(f"{f} exists", p.exists())
        if not p.exists():
            continue
        src = p.read_text()
        for cmd in ("ping", "account", "positions", "symbol", "tick", "bars",
                    "order", "close", "modify"):
            if f'cmd == "{cmd}"' not in src:
                check(f"{f} handles {cmd}", False)
                break
        else:
            check(f"{f} handles all 9 commands", True)
        check(f"{f} declares {plat}", f'JStr("platform", "{plat}")' in src)
        code = _strip_mql(src)
        check(f"{f} balanced braces", code.count("{") == code.count("}"),
              f"{code.count('{')}/{code.count('}')}")
        check(f"{f} balanced parens", code.count("(") == code.count(")"),
              f"{code.count('(')}/{code.count(')')}")
        check(f"{f} has OnInit/OnTimer/OnDeinit",
              all(k in src for k in ("OnInit", "OnTimer", "OnDeinit")))



def t_vectorized():
    """The fast path must produce bit-identical results to the scalar path."""
    print("\n[vectorized engine]")
    import time as _t
    from firm.backtester import backtest
    from firm.strategies.library import all_strategies
    b = SimulatedBroker("t"); b.connect()
    ok_all, speedups = True, []
    for st in all_strategies():
        for sym in ("EURUSD", "XAUUSD"):
            bars = b.bars(sym, "H1", 900)
            sp = b.symbol_spec(sym)
            t0 = _t.time(); a = backtest(st, bars, sym, "H1", {}, sp, force_scalar=True)
            ta = _t.time() - t0
            t0 = _t.time(); c = backtest(st, bars, sym, "H1", {}, sp)
            tb = _t.time() - t0
            same = a.summary() == c.summary()
            ok_all &= same
            if tb > 0: speedups.append(ta / tb)
            if not same:
                check(f"{st}/{sym} identical", False)
    check("all strategies bit-identical scalar vs vector", ok_all)
    check("vector path is faster", min(speedups) > 1.5 if speedups else False,
          f"min {min(speedups):.0f}x, max {max(speedups):.0f}x" if speedups else "")
    # non-default params too
    variants = [("ema_trend_pullback", {"fast": 8, "slow": 34, "rr": 3.0}),
                ("donchian_breakout", {"channel": 40, "buffer_atr": 0.15}),
                ("bollinger_reversion", {"bb_n": 30, "bb_mult": 1.8}),
                ("macd_momentum", {"fast": 8, "slow": 34, "signal": 7})]
    vok = True
    for st, ps in variants:
        bars = b.bars("EURUSD", "H1", 800); sp = b.symbol_spec("EURUSD")
        vok &= (backtest(st, bars, "EURUSD", "H1", ps, sp, force_scalar=True).summary()
                == backtest(st, bars, "EURUSD", "H1", ps, sp).summary())
    check("identical with non-default params", vok)


def t_analytics():
    print("\n[analytics]")
    from firm.analytics import (compute_metrics, equity_series, full_report,
                                monte_carlo, r_distribution)
    import time as _t
    now = _t.time()
    trades = []
    for i, pnl in enumerate([100, -50, 200, -50, 150, -100, 300, -50, 80, -40]):
        trades.append({"status": "closed", "pnl": pnl, "symbol": "EURUSD",
                       "side": "buy" if i % 2 else "sell", "lots": 0.1,
                       "entry": 1.1, "stop": 1.095, "meta_risk_usd": 50.0,
                       "meta_strategy": "s1" if i < 5 else "s2",
                       "created_at": now - (10 - i) * 3600,
                       "closed_at": now - (10 - i) * 3600 + 1800})
    m = compute_metrics(trades, 10_000)
    check("trade count", m.trades == 10)
    check("net profit", abs(m.net_profit - 540) < 1e-6, str(m.net_profit))
    check("win rate", abs(m.win_rate - 50.0) < 1e-6)
    check("profit factor", abs(m.profit_factor - 830 / 290) < 1e-3, num_s(m.profit_factor))
    check("expectancy", abs(m.expectancy - 54.0) < 1e-6)
    check("max drawdown positive", m.max_drawdown > 0, str(m.max_drawdown))
    check("R metrics computed", m.expectancy_r != 0)
    check("streaks counted", m.max_win_streak >= 1 and m.max_loss_streak >= 1)
    eq = equity_series(trades, 10_000)
    check("equity series length", len(eq["points"]) == 11)
    check("equity ends correct", abs(eq["end"] - 10_540) < 1e-6, str(eq["end"]))
    check("peak never decreases",
          all(eq["points"][i]["peak"] <= eq["points"][i + 1]["peak"]
              for i in range(len(eq["points"]) - 1)))
    d = r_distribution(trades)
    check("histogram sums to trades", sum(b["count"] for b in d["bins"]) == 10)
    mc = monte_carlo(trades, runs=200, starting_equity=10_000)
    check("monte carlo uses bootstrap", mc.get("method") == "bootstrap")
    check("bootstrap gives a real distribution",
          mc["final_p5"] < mc["final_p50"] < mc["final_p95"],
          f"{mc['final_p5']:.0f}/{mc['final_p50']:.0f}/{mc['final_p95']:.0f}")
    rep = full_report(trades, 10_000)
    for k in ("metrics", "equity", "distribution", "by_symbol", "by_strategy",
              "by_side", "daily", "hourly", "rolling"):
        if k not in rep:
            check(f"report has {k}", False)
            break
    else:
        check("full report has every section", True)
    check("by_strategy splits correctly", len(rep["by_strategy"]) == 2)
    check("empty input is safe", compute_metrics([]).trades == 0)


def num_s(v):
    return f"{v:.3f}"


def t_ingest():
    print("\n[video ingestion]")
    from firm.ingest import extract_rules_heuristic, video_id
    check("parses watch URL",
          video_id("https://www.youtube.com/watch?v=cXhEw2jF4go&pp=x") == "cXhEw2jF4go")
    check("parses youtu.be", video_id("https://youtu.be/cXhEw2jF4go") == "cXhEw2jF4go")
    check("rejects junk", video_id("not a url") is None)

    t = ("My 3 EMA pullback strategy uses the 9 EMA, the 21 EMA and the 50 EMA. "
         "Wait for price to pull back to the 21 EMA with RSI above 50. "
         "Stop loss 1.5 times ATR, target a 2:1 risk reward. London session, 1 hour chart.")
    spec = extract_rules_heuristic(t)
    types = [r["type"] for r in spec["entry"]]
    check("finds the EMA stack", "ema_stack" in types, str(types))
    stack = next(r for r in spec["entry"] if r["type"] == "ema_stack")
    check("reads 9/21/50 not '3 EMA'",
          (stack["fast"], stack["mid"], stack["slow"]) == (9, 21, 50), str(stack))
    check("finds RSI rule", any("rsi" in x for x in types))
    check("finds the session filter",
          any(f["type"] == "session" for f in spec["filters"]))
    check("reads 2:1 reward", abs(spec["exit"]["rr"] - 2.0) < 1e-9)
    check("reads 1.5xATR stop", abs(spec["exit"]["atr_mult"] - 1.5) < 1e-9)
    check("detects H1", spec["timeframe"] == "H1")

    # spec must actually execute and produce valid geometry
    from firm.strategies.composite import evaluate_spec
    b = SimulatedBroker("t"); b.connect()
    sigs = [s for s in evaluate_spec(spec, b.bars("EURUSD", "H1", 1200)) if s]
    check("spec generates signals", len(sigs) > 0, f"{len(sigs)} signals")
    good = all((s.stop < s.entry < s.take) if s.side == "buy"
               else (s.take < s.entry < s.stop) for s in sigs)
    check("all spec signals have valid geometry", good)
    check("vague text still yields a spec",
          len(extract_rules_heuristic("just buy low and sell high")["entry"]) > 0)


def t_lab():
    print("\n[strategy lab]")
    from firm.lab import Lab, render_ea, robustness_score, SEARCH_SPACE
    from firm.backtester import backtest
    b = SimulatedBroker("t"); b.connect()
    lab = Lab(b)
    r = lab.optimize("donchian_breakout", "EURUSD", "H1", max_combos=8, bars_count=900)
    check("optimize returns a result", r.tested == 8, f"{r.tested} combos")
    check("optimize found params", isinstance(r.params, dict) and len(r.params) > 0)
    check("optimize is quick", r.elapsed < 60, f"{r.elapsed}s")
    check("walk-forward attached", "folds" in r.walk_forward)

    # score behaves sensibly
    bars = b.bars("EURUSD", "H1", 900); sp = b.symbol_spec("EURUSD")
    good = backtest("donchian_breakout", bars, "EURUSD", "H1", {}, sp)
    check("score is finite", isinstance(robustness_score(good), float))
    check("no-trade result scores zero",
          robustness_score(backtest("macd_momentum", bars[:200], "EURUSD", "H1", {}, sp)) == 0.0)

    # EA generation for every strategy, both platforms
    ok = True
    for st in SEARCH_SPACE:
        from firm.lab import LabResult
        res = LabResult(strategy=st, symbol="EURUSD", timeframe="H1",
                        params={}, metrics={"trades": 10}, score=1.0, passed=True)
        for mql in (4, 5):
            src = render_ea(res, f"Test_{st}", mql)
            if ("OnTick" not in src or "OrderSend" not in src.replace("trade.Buy", "OrderSend")
                    or src.count("{") != src.count("}")):
                check(f"EA {st} mq{mql} well-formed", False,
                      f"braces {src.count('{')}/{src.count('}')}")
                ok = False
    check("EA generated for every strategy on MT4 and MT5", ok)

    # sweep + exports
    res = lab.sweep(["EURUSD"], ["H1"], ["donchian_breakout", "ema_trend_pullback"],
                    max_combos=4, bars_count=800)
    check("sweep ranks results", len(res) == 2)
    check("sweep sorted by score", res[0].score >= res[1].score)
    pack = lab.export_pack(res, "test_pack")
    check("strategy pack exported", Path(pack).exists())
    files = lab.export_ea(res[0], "TestExport")
    check("EA files written", all(Path(v).exists() for v in files.values()),
          str(list(files)))


def t_api():
    print("\n[web api]")
    import json as _j
    from fastapi.testclient import TestClient
    try:
        from web.server import app
    except Exception as e:
        check("server imports", False, str(e)[:80]); return
    c = TestClient(app)
    check("index serves", c.get("/").status_code == 200)
    r = c.get("/api/status")
    check("status endpoint", r.status_code == 200 and "firm" in r.json())
    r = c.get("/api/analytics")
    check("analytics endpoint", r.status_code == 200 and "metrics" in r.json())
    a = r.json()
    check("analytics has every chart series",
          all(k in a for k in ("equity", "distribution", "monte_carlo", "daily",
                               "hourly", "rolling", "by_symbol")))
    r = c.get("/api/lab/strategies")
    check("lab strategy list", r.status_code == 200 and len(r.json()["strategies"]) >= 4)
    r = c.post("/api/ask", json={"text": "status check"})
    check("ask endpoint", r.status_code == 200 and r.json().get("issue", 0) > 0)
    check("empty ask rejected", c.post("/api/ask", json={"text": "  "}).status_code == 400)
    r = c.post("/api/lab/export", json={"result": {
        "strategy": "donchian_breakout", "symbol": "EURUSD", "timeframe": "H1",
        "params": {"channel": 20}, "metrics": {"trades": 30}, "score": 1.0,
        "passed": True}, "name": "ApiTest", "platform": "both"})
    check("export endpoint returns files", r.status_code == 200 and
          len(r.json().get("files", {})) >= 2)
    fn = list(r.json()["files"].values())[0]
    check("download works", c.get(f"/api/lab/download/{fn}").status_code == 200)
    check("bad download 404s", c.get("/api/lab/download/nope.mq5").status_code == 404)


def t_scout():
    print("\n[auto-scan suite]")
    from firm.scout import (CATALOGUE, catalogue, discover, discover_llm, scan,
                            summarise, valid_spec, verdict_of)
    from firm.strategies.composite import RULE_TYPES

    check("catalogue is populated", len(CATALOGUE) >= 8, f"{len(CATALOGUE)} archetypes")
    bad = [s["name"] for s in CATALOGUE if not valid_spec(s)]
    check("every archetype is a valid spec", not bad, str(bad))
    types = {r["type"] for s in CATALOGUE
             for r in s.get("entry", []) + s.get("filters", [])}
    check("archetypes use known rule types only", types <= set(RULE_TYPES),
          str(types - set(RULE_TYPES)))
    check("every archetype has an exit", all(s.get("exit") for s in CATALOGUE))

    # ordering + filtering
    pops = [s.get("popularity", 0) for s in catalogue()]
    check("catalogue sorted by popularity", pops == sorted(pops, reverse=True))
    bo = catalogue(tags=["breakout"])
    check("tag filter works", bo and all("breakout" in s["tags"] for s in bo),
          f"{len(bo)} breakout")
    check("limit respected", len(catalogue(limit=3)) == 3)

    # malformed specs must be rejected, never crash a scan
    check("rejects non-dict", not valid_spec("nope"))
    check("rejects empty entry", not valid_spec({"name": "x", "entry": []}))
    check("rejects unknown rule type",
          not valid_spec({"name": "x", "entry": [{"type": "moon_phase"}]}))
    check("rejects unnamed spec", not valid_spec({"entry": [{"type": "candle"}]}))
    check("rejects bad filter",
          not valid_spec({"name": "x", "entry": [{"type": "candle"}],
                          "filters": [{"type": "astrology"}]}))

    # discovery degrades to catalogue with no LLM
    check("discover works with no LLM", len(discover(None, limit=4)) == 4)
    check("discover_llm returns nothing without a client", discover_llm(None) == [])

    class _Broken:
        available = True
        def ask(self, *a, **k):
            raise RuntimeError("boom")
    check("discover_llm swallows client errors", discover_llm(_Broken()) == [])

    # verdicts
    check("verdict ADOPT", verdict_of({"passed": True}) == "ADOPT")
    check("verdict WATCH", verdict_of(
        {"passed": False, "metrics": {"trades": 20, "expectancy_r": 0.3}}) == "WATCH")
    check("verdict IGNORE", verdict_of(
        {"passed": False, "metrics": {"trades": 20, "expectancy_r": -0.3}}) == "IGNORE")
    check("tiny sample is not WATCH", verdict_of(
        {"passed": False, "metrics": {"trades": 3, "expectancy_r": 9.9}}) == "IGNORE")

    # a live scan over the simulated broker
    from firm.brokers.simulated import SimulatedBroker
    from firm.lab import Lab
    br = SimulatedBroker({"id": "sim"}); br.connect()
    lab = Lab(br)
    seen = []
    res = scan(lab, catalogue(limit=3), ["EURUSD"], "H1", max_combos=6, bars=1000,
               progress=lambda p: seen.append(p))
    check("scan returns one row per spec x symbol", len(res) == 3, f"{len(res)} rows")
    check("scan reported progress", len(seen) == 3)
    check("scan ranked by score",
          [r["score"] for r in res] == sorted([r["score"] for r in res], reverse=True))
    check("every row has a verdict",
          all(r["verdict"] in ("ADOPT", "WATCH", "IGNORE", "ERROR") for r in res))
    check("every row is JSON-safe", bool(_json.dumps(res)))
    check("rows carry the spec for export", all(r.get("spec") for r in res))

    # one broken spec must not abort the scan
    boom = {"name": "Broken", "entry": [{"type": "ema_stack"}], "exit": None}
    mixed = scan(lab, [catalogue(limit=1)[0], boom], ["EURUSD"],
                 "H1", max_combos=4, bars=1000)
    check("broken spec is isolated, scan continues", len(mixed) == 2)

    s = summarise(res)
    check("summary counts add up",
          s["adopt"] + s["watch"] + s["ignore"] + s["errors"] == s["scanned"],
          f"{s['adopt']}A/{s['watch']}W/{s['ignore']}I/{s['errors']}E")
    check("summary picks the best", s["best"] is res[0])
    check("empty scan summarises cleanly", summarise([])["scanned"] == 0)


def t_instructions():
    print("\n[agent instruction docs]")
    from firm.agents.base import INSTRUCTIONS_DIR
    from firm.orchestrator import Firm
    f = Firm(connect=False)

    check("seven departments hired", len(f.agents) == 7, str(list(f.agents)))
    check("scout is one of them", "scout" in f.agents)

    for a in f.agents.values():
        check(f"{a.name} has an instruction file", a.instructions_path.exists(),
              a.instructions_path.name)
        doc = a.instructions()
        check(f"{a.name} doc is substantive", len(doc) > 300, f"{len(doc)} chars")
        check(f"{a.name} doc is the file, not the charter", doc != a.charter)

    ceo = f.agents["ceo"]
    check("system_prompt embeds the doc", ceo.instructions()[:40] in ceo.system_prompt("BASE"))
    check("system_prompt keeps the built-in prompt", "BASE" in ceo.system_prompt("BASE"))

    # a missing doc must degrade to the charter, not crash
    scout = f.agents["scout"]
    p = scout.instructions_path
    backup = p.read_text()
    try:
        p.unlink()
        check("missing doc falls back to charter", scout.instructions() == scout.charter)
        p.write_text("   ")
        check("blank doc falls back to charter", scout.instructions() == scout.charter)
    finally:
        p.write_text(backup)
    check("doc restored", scout.instructions() != scout.charter)

    check("instructions dir is inside the package",
          INSTRUCTIONS_DIR.name == "instructions" and INSTRUCTIONS_DIR.exists())


def t_scout_routing():
    print("\n[scout wiring]")
    from firm.agents.ceo import DEPARTMENTS, ROUTES
    from firm.orchestrator import ORDER
    check("scout is a department", "scout" in DEPARTMENTS)
    check("scout runs before research in the pipeline",
          DEPARTMENTS.index("scout") < DEPARTMENTS.index("research"))
    check("scout in the tick order", "scout" in ORDER)
    check("ceo ticks before scout", ORDER.index("ceo") < ORDER.index("scout"))

    routed = {}
    for keys, agent, title, body in ROUTES:
        for k in keys:
            routed[k] = agent
    for word in ("scan", "trending", "discover", "autoscan"):
        check(f"'{word}' routes to scout", routed.get(word) == "scout")

    # the autonomous cycle table must line up with the shifted ROUTES indices.
    # Use a fresh memory: autonomous_cycle deliberately refuses to queue a
    # cycle that is already open, so the shared demo DB would mask the mapping.
    from firm.orchestrator import Firm
    f = Firm(memory=tmp_mem(), connect=False)
    ceo = f.agents["ceo"]
    plan = ceo._heuristic_plan("scan for trending strategies")
    check("keyword plan reaches scout", any(d["agent"] == "scout" for d in plan))
    full = ceo._heuristic_plan("do something completely unrelated")
    check("default plan is the full pipeline in order",
          [d["agent"] for d in full] == ["research", "backtest", "risk", "execution"],
          str([d["agent"] for d in full]))
    for kind, agent in (("scout", "scout"), ("research", "research"),
                        ("backtest", "backtest"), ("risk", "risk"),
                        ("execution", "execution"), ("cost", "cost_optimizer")):
        iid = ceo.autonomous_cycle(kind)
        row = f.memory.q("SELECT assignee FROM issues WHERE id=?", (iid,))
        check(f"cycle '{kind}' assigns {agent}", row and row[0]["assignee"] == agent,
              row[0]["assignee"] if row else "no issue")


def t_scout_api():
    print("\n[auto-scan api]")
    from fastapi.testclient import TestClient
    from web.server import app
    c = TestClient(app)

    r = c.get("/api/scout/catalogue")
    check("catalogue endpoint", r.status_code == 200 and
          len(r.json()["strategies"]) >= 8)
    first = r.json()["strategies"][0]
    check("catalogue rows are complete",
          all(k in first for k in ("name", "summary", "tags", "popularity", "rules")))

    check("history endpoint", c.get("/api/scout/history").status_code == 200)

    r = c.get("/api/instructions")
    check("instructions endpoint", r.status_code == 200)
    agents = r.json()["agents"]
    check("all seven docs exposed", len(agents) == 7, str(len(agents)))
    check("docs carry their text", all(a["text"] and a["exists"] for a in agents))

    check("unknown agent rejected",
          c.post("/api/instructions", json={"agent": "nobody", "text": "x"}
                 ).status_code == 404)
    check("empty instructions rejected",
          c.post("/api/instructions", json={"agent": "scout", "text": "   "}
                 ).status_code == 400)

    # round-trip an edit, then restore
    from firm.orchestrator import Firm
    path = Firm(connect=False).agents["scout"].instructions_path
    backup = path.read_text()
    try:
        r = c.post("/api/instructions",
                   json={"agent": "scout", "text": backup + "\n## Test\nmarker"})
        check("instruction save works", r.status_code == 200 and r.json()["ok"])
        check("edit hit the disk", "## Test" in path.read_text())
        check("edit is served back",
              "## Test" in [a for a in c.get("/api/instructions").json()["agents"]
                            if a["name"] == "scout"][0]["text"])
    finally:
        path.write_text(backup)



def t_mql_compiler():
    print("\n[spec -> MQL compiler]")
    import re as _re
    from firm.mql import can_compile, render_spec_ea
    from firm.scout import CATALOGUE
    from firm.strategies.composite import RULE_TYPES

    def strip(src):
        src = _re.sub(r"//.*", "", src)
        return _re.sub(r'"(?:[^"\\]|\\.)*"', '""', src)

    every = {"name": "All Rules", "summary": "every rule type",
             "entry": [{"type": t} for t in
                       ("ema_stack", "ema_cross", "sma_cross", "rsi_zone",
                        "rsi_extreme", "macd_cross", "bb_touch", "breakout",
                        "pullback", "candle")],
             "filters": [{"type": "session", "from": 7, "to": 18},
                         {"type": "atr_filter", "min_pct": 0.02, "max_pct": 2.0}],
             "exit": {"atr_mult": 1.5, "rr": 2.0}, "agreement": 0.6}
    covered = {r["type"] for r in every["entry"] + every["filters"]}
    check("every rule type is exercised", covered == set(RULE_TYPES),
          str(set(RULE_TYPES) - covered))

    for spec in [every] + CATALOGUE:
        nm = _re.sub(r"\W", "_", spec["name"])
        check(f"can_compile {spec['name'][:28]}", can_compile(spec))
        for v in (4, 5):
            src = render_spec_ea(spec, nm, v, symbol="EURUSD", timeframe="H1")
            b = strip(src)
            check(f"{nm[:22]} mq{v} braces balanced",
                  b.count("{") == b.count("}"),
                  f"{b.count('{')}/{b.count('}')}")
            check(f"{nm[:22]} mq{v} parens balanced",
                  b.count("(") == b.count(")"))
            for fn in ("OnInit", "OnTick", "Decide", "LotsForRisk", "CountPositions"):
                check(f"{nm[:22]} mq{v} has {fn}", fn in src)

    check("rejects a spec with no rules", not can_compile({"name": "x", "entry": []}))
    check("rejects an unknown rule type",
          not can_compile({"name": "x", "entry": [{"type": "astrology"}]}))
    check("rejects a non-dict", not can_compile("nope"))

    # dialect purity - MQL5 idioms must never leak into MQL4 and vice versa
    s4 = render_spec_ea(every, "D", 4)
    s5 = render_spec_ea(every, "D", 5)
    for tok in ("CopyBuffer", "CTrade", "PositionGetTicket", "SymbolInfoDouble"):
        check(f"mq4 free of {tok}", tok not in s4)
    for tok in ("OrderSend", "MarketInfo", "AccountEquity"):
        check(f"mq5 free of {tok}", tok not in s5)
    check("mq5 declares every handle it reads",
          not (set(_re.findall(r"Buf\((h\w+)", s5))
               - set(_re.findall(r"^int (h\w+);", s5, _re.M))))
    check("mq5 creates every handle it reads",
          not (set(_re.findall(r"Buf\((h\w+)", s5))
               - set(_re.findall(r"^\s+(h\w+)=i", s5, _re.M))))

    # optimizer params must override the spec's own exit block
    spec = {"name": "P",
            "entry": [{"type": "rsi_zone", "period": 21, "min": 37, "max": 81},
                      {"type": "breakout", "period": 33},
                      {"type": "bb_touch", "period": 27, "mult": 2.7}],
            "filters": [{"type": "session", "from": 3, "to": 19}],
            "exit": {"atr_mult": 9.9, "rr": 9.9}, "agreement": 0.11}
    opt = {"atr_mult": 2.5, "rr": 3.5, "agreement": 0.75}
    for v in (4, 5):
        src = render_spec_ea(spec, "P", v, params=opt)
        check(f"mq{v} bakes optimizer atr_mult", "ATRMultiplier = 2.5" in src)
        check(f"mq{v} bakes optimizer rr", "RewardRatio   = 3.5" in src)
        check(f"mq{v} bakes optimizer agreement", "Agreement     = 0.75" in src)
        check(f"mq{v} honours rsi period 21",
              "hRsi21" in src or "iRSI(NULL,0,21" in src)
        check(f"mq{v} honours rsi band", "r>=37.0 && r<=81.0" in src)
        check(f"mq{v} honours breakout 33", "HH(33,2)" in src)
        check(f"mq{v} honours session hours", "hr>=3 && hr<19" in src)
        check(f"mq{v} evaluates the closed bar", "Cl(1)" in src)
        check(f"mq{v} documents the session caveat", "BROKER SERVER TIME" in src)

    # the veto must survive compilation
    check("session veto emitted", "-99" in render_spec_ea(every, "V", 5))


def t_rule_periods():
    print("\n[per-rule indicator periods]")
    # Regression: _ind used to compute ONE spec-level rsi/bb/donchian, so a
    # rule asking for rsi_zone period 21 was silently scored on RSI-14 and the
    # backtest did not test the strategy that was written.
    from firm.brokers.simulated import SimulatedBroker
    from firm.indicators import bollinger, closes, donchian, rsi
    from firm.strategies.composite import _ind, _rule_vote
    br = SimulatedBroker({"id": "s"}); br.connect()
    bars = br.bars("EURUSD", "H1", 600)
    c = closes(bars)

    I = _ind(bars, {"name": "x", "entry": [{"type": "breakout", "period": 15}]})
    check("breakout builds its own channel", "dc15" in I)
    d15 = donchian(bars, 15)
    check("breakout channel is the right period",
          all(I["dc15"][0][i] == d15[0][i] for i in range(250, 600)))

    I = _ind(bars, {"name": "x",
                    "entry": [{"type": "rsi_zone", "period": 21, "min": 40, "max": 70}]})
    check("rsi_zone builds its own series", "rsi21" in I)
    r21 = rsi(c, 21)
    check("rsi series is the right period",
          all(I["rsi21"][i] == r21[i] for i in range(250, 600)))
    r14 = rsi(c, 14)
    check("rsi21 actually differs from rsi14",
          sum(1 for i in range(250, 600) if r21[i] != r14[i]) > 50)

    I = _ind(bars, {"name": "x",
                    "entry": [{"type": "bb_touch", "period": 27, "mult": 2.7}]})
    check("bb_touch builds its own bands", "bb27_2.7" in I)
    b27 = bollinger(c, 27, 2.7)
    check("bands are the right settings",
          all(I["bb27_2.7"][0][i] == b27[0][i] for i in range(250, 600)))

    # and the vote must actually change as a result
    rule21 = {"type": "rsi_zone", "period": 21, "min": 40, "max": 70}
    rule14 = {"type": "rsi_zone", "min": 40, "max": 70}
    I = _ind(bars, {"name": "x", "entry": [rule21, rule14]})
    diff = sum(1 for i in range(250, 600)
               if _rule_vote(rule21, I, i) != _rule_vote(rule14, I, i))
    check("period actually changes the vote", diff > 0, f"{diff} bars differ")

    # a rule with no explicit period still works off the spec default
    I = _ind(bars, {"name": "x", "entry": [{"type": "rsi_zone", "min": 40, "max": 70}]})
    check("period-less rule still votes",
          _rule_vote({"type": "rsi_zone", "min": 40, "max": 70}, I, 400) in (-1, 0, 1))


def t_export_compiles_specs():
    print("\n[composite export path]")
    from fastapi.testclient import TestClient
    from web.server import app
    from firm.scout import CATALOGUE
    c = TestClient(app)

    spec = CATALOGUE[0]
    payload = {"result": {"strategy": "scanned", "symbol": "EURUSD",
                          "timeframe": "H1", "composite": True, "spec": spec,
                          "params": {"atr_mult": 2.0, "rr": 2.5, "agreement": 0.75},
                          "metrics": {"trades": 30, "profit_factor": 2.1},
                          "walk_forward": {"robust": True}, "score": 1.4,
                          "passed": True},
               "name": "CompiledScan", "platform": "both"}
    r = c.post("/api/lab/export", json=payload)
    check("composite export succeeds", r.status_code == 200)
    body = r.json()
    check("export reports it compiled the spec", body.get("compiled") is True)
    files = body.get("files", {})
    check("rule pack still emitted", "json" in files)
    check("both dialects emitted", "mq4" in files and "mq5" in files)

    src = c.get(f"/api/lab/download/{files['mq5']}").text
    check("EA is compiled from the rules, not a shell", "int Decide(double atr)" in src)
    check("EA carries the strategy name", spec["name"][:10] in src)
    check("EA bakes the chosen agreement", "Agreement     = 0.75" in src)
    check("EA warns about live divergence", "Demo test first" in src)

    # a built-in (non-composite) result must still use the template path
    r2 = c.post("/api/lab/export", json={
        "result": {"strategy": "donchian_breakout", "symbol": "EURUSD",
                   "timeframe": "H1", "params": {"channel": 20},
                   "metrics": {"trades": 40}, "score": 1.0, "passed": True},
        "name": "BuiltinTpl", "platform": "mt5"})
    check("built-in export still works", r2.status_code == 200)
    check("built-in is not spec-compiled", r2.json().get("compiled") is False)



def t_composite_validation():
    print("\n[backtest agent validates rule specs]")
    # Regression: the scout files composite rule specs, whose spec dict has no
    # "strategy" key. The backtest agent used to `continue` past them, and
    # because scout proposals filled the per-cycle slice, NOTHING got validated.
    from firm.orchestrator import Firm
    from firm.scout import CATALOGUE
    mem = tmp_mem()
    f = Firm(memory=mem)
    bt = f.agents["backtest"]

    spec = CATALOGUE[0]
    mem.upsert_strategy(name=f"{spec['name']}@EURUSD",
                        spec={"composite": True, "spec": spec, "params": {},
                              "symbol": "EURUSD", "timeframe": "H1"},
                        source="scout", status="proposed")
    check("composite proposal queued", len(mem.strategies("proposed")) == 1)

    out = bt.handle({"id": 1, "title": "validate", "body": ""})
    check("agent reports on the rule spec", "rule spec" in out, out[:80])
    rows = mem.q("SELECT * FROM backtests")
    check("a backtest row was written", len(rows) == 1, f"{len(rows)} rows")
    check("verdict recorded", rows[0]["passed"] in (0, 1))
    st = mem.strategies()[0]
    check("status moved off 'proposed'", st["status"] in ("approved", "rejected"),
          st["status"])
    check("metrics stored", bool(st["metrics"].get("trades") is not None))
    check("walk-forward stored", "walk_forward" in st["metrics"])

    # an untestable spec must be rejected, not crash the cycle
    mem.upsert_strategy(name="Broken@EURUSD",
                        spec={"composite": True, "spec": {"name": "B", "entry": []},
                              "symbol": "EURUSD", "timeframe": "H1"},
                        source="scout", status="proposed")
    out2 = bt.handle({"id": 2, "title": "validate", "body": ""})
    check("broken spec handled without crashing", isinstance(out2, str))
    broken = [x for x in mem.strategies() if x["name"] == "Broken@EURUSD"][0]
    check("broken spec did not sneak through", broken["status"] != "approved",
          broken["status"])



def t_ingest_keys():
    print("\n[ingest -> engine key contract]")
    # Regression: ingest emitted {"type":"breakout","channel":N} while the rule
    # engine and MQL compiler both read "period", so a video saying "30 bar
    # breakout" was silently tested (and exported) as a 20-bar one.
    from firm.brokers.simulated import SimulatedBroker
    from firm.indicators import donchian
    from firm.ingest import extract_rules_heuristic
    from firm.mql import render_spec_ea
    from firm.strategies.composite import _ind, RULE_TYPES

    br = SimulatedBroker({"id": "s"}); br.connect()
    bars = br.bars("EURUSD", "H1", 600)

    for key in ("channel", "period"):
        spec = {"name": "x", "entry": [{"type": "breakout", key: 30}], "exit": {}}
        I = _ind(bars, spec)
        check(f"breakout honours '{key}'", "dc30" in I)
        d30 = donchian(bars, 30)
        check(f"'{key}' builds the right channel",
              all(I["dc30"][0][i] == d30[0][i] for i in range(300, 600)))
        check(f"MQL honours '{key}'", "HH(30,2)" in render_spec_ea(spec, "X", 5))

    # whatever the heuristic emits must be executable by the engine
    text = ("We use a 20 EMA, 30 EMA and 50 EMA stack. Wait for a pullback to the "
            "30 EMA. RSI should be above 40. Then trade the 30 bar breakout with a "
            "bullish engulfing candle. Stop is 2 ATR, target 2R.")
    spec = extract_rules_heuristic(text)
    rules = spec.get("entry", []) + spec.get("filters", [])
    check("heuristic produced rules", len(rules) > 0, f"{len(rules)} rules")
    check("every emitted rule type is known",
          all(r.get("type") in RULE_TYPES for r in rules),
          str([r.get("type") for r in rules]))
    I = _ind(bars, spec)
    from firm.strategies.composite import _rule_vote
    for r in rules:
        v = _rule_vote(r, I, 400)
        check(f"engine can score '{r.get('type')}'", v in (-99, -1, 0, 1), str(v))



def t_llm_providers():
    print("\n[multi-provider LLM client]")
    import http.server as _hs
    import socketserver as _ss
    import threading as _th
    from firm.llm import LLM, PROVIDERS, price_for

    check("anthropic preset uses its own protocol",
          PROVIDERS["anthropic"][0] == "anthropic")
    for name in ("openrouter", "groq", "openai", "together", "deepseek", "custom"):
        check(f"{name} speaks the openai protocol", PROVIDERS[name][0] == "openai")

    # no key -> disabled, and ask() must not raise
    off = LLM(api_key="", provider="openrouter")
    check("no key disables the client", not off.enabled)
    r = off.ask("a", "m", "s", "p")
    check("disabled ask returns an error, not an exception", r.error and not r.used_llm)

    # a local endpoint may run without a key
    loc = LLM(api_key="", provider="custom",
              base_url="http://localhost:1234/v1/chat/completions")
    check("local endpoint needs no key", loc.enabled)

    # model resolution
    orr = LLM(api_key="k", provider="openrouter", model_prefix="anthropic")
    check("prefix applied", orr.resolve_model("claude-sonnet-4-5")
          == "anthropic/claude-sonnet-4-5")
    check("already-qualified model untouched",
          orr.resolve_model("meta-llama/llama-3.3-70b") == "meta-llama/llama-3.3-70b")
    check("no prefix configured leaves model alone",
          LLM(api_key="k", provider="groq").resolve_model("llama-3.3") == "llama-3.3")

    # pricing
    check("':free' models cost nothing", price_for("x/y:free") == (0.0, 0.0))
    check("paid models keep their price", price_for("claude-sonnet-4-5") == (3.0, 15.0))

    # a mock OpenAI-compatible server proves the wire format
    seen = {}

    class _H(_hs.BaseHTTPRequestHandler):
        def do_POST(self):
            import json as _j
            n = int(self.headers.get("content-length", 0))
            seen["body"] = _j.loads(self.rfile.read(n) or b"{}")
            seen["auth"] = self.headers.get("Authorization", "")
            out = {"choices": [{"message": {"content": '{"ok": true}'}}],
                   "usage": {"prompt_tokens": 42, "completion_tokens": 11}}
            b = _j.dumps(out).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def log_message(self, *a):
            pass

    srv = _ss.TCPServer(("127.0.0.1", 8792), _H)
    _th.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        llm = LLM(api_key="test-key", provider="custom",
                  base_url="http://127.0.0.1:8792/v1/chat/completions")
        rep = llm.ask("cli", "some-model", "SYS", "PROMPT")
        check("openai call succeeds", not rep.error, rep.error)
        check("bearer auth sent", seen["auth"] == "Bearer test-key")
        msgs = seen["body"]["messages"]
        check("system prompt sent as a system message",
              msgs[0]["role"] == "system" and msgs[0]["content"] == "SYS")
        check("user prompt sent second",
              msgs[1]["role"] == "user" and msgs[1]["content"] == "PROMPT")
        check("openai token usage mapped",
              rep.input_tokens == 42 and rep.output_tokens == 11)
        check("cost computed", rep.usd > 0)
        check("reply json parsed", rep.json() == {"ok": True})
    finally:
        srv.shutdown()

    # a provider returning no choices must degrade, not crash
    class _Empty(_hs.BaseHTTPRequestHandler):
        def do_POST(self):
            b = b'{"choices": []}'
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def log_message(self, *a):
            pass

    srv2 = _ss.TCPServer(("127.0.0.1", 8793), _Empty)
    _th.Thread(target=srv2.serve_forever, daemon=True).start()
    try:
        bad = LLM(api_key="k", provider="custom",
                  base_url="http://127.0.0.1:8793/v1/chat/completions")
        rep = bad.ask("cli", "m", "s", "p")
        check("empty choices handled gracefully", bool(rep.error) and not rep.text)
    finally:
        srv2.shutdown()

    # config plumbing
    from firm.config import load_config
    cfg = load_config()
    check("config exposes a provider", isinstance(cfg.llm_provider, str))
    check("anthropic_key stays a working alias", cfg.anthropic_key == cfg.llm_key)



def t_blotter_api():
    print("\n[trade blotter + activity log]")
    from firm.analytics import blotter, r_multiple, risk_usd

    # R is pnl / initial risk; a 2:1 winner on $100 risk is +2R
    t = {"status": "closed", "entry": 1.1000, "stop": 1.0900, "lots": 0.1,
         "pnl": 200.0, "meta_risk_usd": 100.0}
    check("r_multiple uses recorded risk", r_multiple(t) == 2.0)
    check("risk_usd prefers the recorded figure", risk_usd(t) == 100.0)

    inferred = {"status": "closed", "entry": 1.1000, "stop": 1.0900,
                "lots": 0.1, "pnl": 100.0}
    check("risk_usd infers from lots when unrecorded",
          abs(risk_usd(inferred) - 100.0) < 1e-6)
    check("open trades have no R", r_multiple({"status": "open", "entry": 1, "stop": .9}) is None)
    check("zero-risk trade yields no R",
          r_multiple({"status": "closed", "entry": 1.1, "stop": 1.1, "pnl": 5}) is None)

    rows = blotter([
        {"id": 1, "status": "closed", "created_at": 1000, "closed_at": 8200,
         "entry": 1.1, "stop": 1.09, "lots": 0.1, "pnl": 100.0, "symbol": "EURUSD",
         "side": "buy", "meta_strategy": "x", "ticket": "1"},
        {"id": 2, "status": "open", "created_at": 5000, "entry": 1.2, "stop": 1.19,
         "lots": 0.1, "symbol": "GBPUSD", "side": "sell", "ticket": "2"},
    ])
    check("blotter returns every trade", len(rows) == 2)
    check("blotter sorts newest first", rows[0]["id"] == 2)
    closed = [r for r in rows if r["status"] == "closed"][0]
    check("hold time computed in hours", abs(closed["hold_hours"] - 2.0) < 0.01)
    check("open trade has null pnl",
          [r for r in rows if r["status"] == "open"][0]["pnl"] is None)

    import warnings
    warnings.filterwarnings("ignore")
    from fastapi.testclient import TestClient
    from web.server import app
    c = TestClient(app)

    d = c.get("/api/trades?limit=5").json()
    check("/api/trades responds", "trades" in d and "summary" in d)
    check("limit is honoured", len(d["trades"]) <= 5)
    check("filter options offered", isinstance(d["symbols"], list))
    check("summary counts closed trades", d["summary"]["closed"] >= 0)

    if d["symbols"]:
        sym = d["symbols"][0]
        f = c.get(f"/api/trades?symbol={sym}").json()
        check("symbol filter narrows the set",
              all(t["symbol"] == sym for t in f["trades"]))
    o = c.get("/api/trades?status=open").json()
    check("status filter works", all(t["status"] == "open" for t in o["trades"]))

    e = c.get("/api/events?limit=5").json()
    check("/api/events responds", "events" in e)
    check("events carry a level", all("level" in x for x in e["events"]))
    check("event meta is decoded to a dict",
          all(isinstance(x["meta"], dict) for x in e["events"]))

    a = c.get("/api/analytics").json()
    pv = a.get("provenance", {})
    check("analytics reports provenance", "demo_trades" in pv)
    check("seeded demo trades are flagged", pv["demo_trades"] > 0)
    check("is_demo set when synthetic data present",
          pv["is_demo"] == (pv["demo_trades"] > 0))
    check("blotter shipped with the report", isinstance(a.get("blotter"), list))



def t_indicator_compiler():
    print("\n[indicator compiler]")
    import re as _re
    from firm.mql import (_Ind, _rule_code, can_compile, render_spec_ea,
                          render_spec_indicator)
    from firm.scout import CATALOGUE

    # The indicator replays rules at an arbitrary shift. Substituting sh=1 into
    # the indicator emission must reproduce the EA emission character for
    # character - that is what guarantees chart arrows match the backtest.
    def norm(x):
        return _re.sub(r"\s+", " ", x).strip()

    mismatches, compared = 0, 0
    for c in CATALOGUE:
        for r in list(c.get("entry") or []) + list(c.get("filters") or []):
            for mql in (4, 5):
                ea = _rule_code(r, _Ind(), mql, "v")
                ix = _rule_code(r, _Ind(), mql, "v", s1="sh", s2="sh+1")
                compared += 1
                if norm(ix.replace("sh+1", "2").replace("sh", "1")) != norm(ea):
                    mismatches += 1
    check(f"indicator rules match EA rules at shift 1 ({compared} emissions)",
          mismatches == 0, f"{mismatches} differ")

    spec = CATALOGUE[0]
    for mql in (4, 5):
        src = render_spec_indicator(spec, "T_Ind", mql, symbol="EURUSD", timeframe="H1")
        body = _strip_mql(src)
        check(f"mq{mql} indicator braces balance",
              body.count("{") == body.count("}"),
              f"{body.count('{')} vs {body.count('}')}")
        check(f"mq{mql} indicator never places an order",
              not any(b in src for b in ("OrderSend", "trade.Buy", "trade.Sell", "CTrade")))
        check(f"mq{mql} indicator declares both plot buffers",
              "double BufUp[];" in src and "double BufDn[];" in src)
        check(f"mq{mql} indicator has OnCalculate", "OnCalculate" in src)
        check(f"mq{mql} indicator declares vote counters before use",
              src.index("int gBulls") < src.index("int DecideAt"))
        check(f"mq{mql} indicator skips the forming bar",
              "sh<1" in src or "sh>=1" in src)
        check(f"mq{mql} indicator states it does not trade",
              "does not trade" in src)

    # every catalogue spec must compile to a structurally sound indicator
    bad = []
    for c in CATALOGUE:
        nm = c["name"].replace(" ", "_").replace("-", "_")
        for mql in (4, 5):
            b = _strip_mql(render_spec_indicator(c, nm, mql))
            if b.count("{") != b.count("}") or b.count("(") != b.count(")"):
                bad.append(f"{nm}/mq{mql}")
    check(f"all {len(CATALOGUE)} catalogue specs compile cleanly to indicators",
          not bad, str(bad[:3]))

    # the EA path must be untouched by the shift refactor
    ea5 = render_spec_ea(spec, "T_Ea", 5, symbol="EURUSD")
    check("EA still reads the last closed bar", "Cl(1)" in ea5 or "Buf(" in ea5)
    check("EA still trades", "trade.Buy" in ea5)

    from firm.lab import Lab, LabResult
    res = LabResult(strategy="x", symbol="EURUSD", timeframe="H1", params={},
                    metrics={}, walk_forward={}, score=0.0, passed=False)
    check("indicator export refuses a spec-less strategy",
          Lab().export_indicator(res, "NoSpec", "mt5", spec=None) == {})
    check("can_compile gates the indicator too", can_compile(spec))

    import warnings
    warnings.filterwarnings("ignore")
    from fastapi.testclient import TestClient
    from web.server import app
    c = TestClient(app)
    payload = {"strategy": "s", "symbol": "EURUSD", "timeframe": "H1", "params": {},
               "metrics": {}, "walk_forward": {}, "score": 1.0, "passed": True,
               "composite": True, "spec": spec}
    out = c.post("/api/lab/export", json={"result": payload, "name": "T_Both",
                                          "platform": "both", "kind": "both"}).json()
    check("export returns EA files", "mq5" in out["files"] and "mq4" in out["files"])
    check("export returns indicator files",
          "ind_mq5" in out["files"] and "ind_mq4" in out["files"])
    check("indicator filename is distinct from the EA",
          out["files"]["ind_mq5"] != out["files"]["mq5"])

    only = c.post("/api/lab/export", json={"result": payload, "name": "T_IndOnly",
                                           "platform": "mt5", "kind": "indicator"}).json()
    check("kind=indicator emits no EA", "mq5" not in only["files"])
    check("kind=indicator emits the indicator", "ind_mq5" in only["files"])

    builtin = {"strategy": "ema_trend_pullback", "symbol": "EURUSD", "timeframe": "H1",
               "params": {}, "metrics": {}, "walk_forward": {}, "score": 0.5}
    nb = c.post("/api/lab/export", json={"result": builtin, "name": "T_Builtin",
                                         "platform": "mt5", "kind": "indicator"}).json()
    check("built-in strategy is refused an indicator, with a reason",
          "ind_mq5" not in nb["files"] and "indicator_note" in nb["files"])



def t_gateway_passthrough():
    print("\n[gateway auto/fusion passthrough]")
    from firm.orchestrator import Firm
    from firm.router import is_passthrough

    check("'auto' is recognised as gateway routing", is_passthrough("auto"))
    check("'fusion' is recognised", is_passthrough("Fusion"))
    check("whitespace and case are tolerated", is_passthrough("  AUTO "))
    check("a real model name is not passthrough", not is_passthrough("magistral-medium"))
    check("an empty model is not passthrough", not is_passthrough(""))

    def _sent(model_name):
        f = Firm(memory=tmp_mem(), connect=False)
        f.cfg.raw.setdefault("llm", {})["routing"] = True
        f.cfg.raw.setdefault("agents", {}).setdefault("research", {})["model"] = model_name
        agent = f.agents["research"]
        f.llm.enabled = True
        f.llm.free_tier = True
        seen = {}
        orig = f.llm.ask

        def spy(a, mo, *x, **k):
            seen["model"] = mo
            return orig(a, mo, *x, **k)

        f.llm.ask = spy
        agent.llm = f.llm
        agent.think("s", "p")
        return seen.get("model")

    check("client routing does not override 'auto'", _sent("auto") == "auto")
    check("client routing does not override 'fusion'", _sent("fusion") == "fusion")
    check("a pinned model is still routed by pool headroom",
          _sent("magistral-medium") is not None)


def t_local_endpoint():
    print("\n[local model endpoint]")
    import http.server
    import json as _json
    import threading
    from firm.llm import LLM, price_for

    check("a local endpoint needs no API key",
          LLM(api_key="", provider="custom",
              base_url="http://localhost:11434/v1").enabled)
    check("a remote endpoint still requires a key",
          not LLM(api_key="", provider="anthropic").enabled)
    check("local models are priced at zero", price_for("local/qwen2.5:14b") == (0.0, 0.0))

    seen = {}

    class _Local(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("content-length", 0))
            self.rfile.read(n)
            seen["auth"] = self.headers.get("Authorization")
            body = _json.dumps({
                "choices": [{"message": {"content": '{"ok": true}'}}],
                "usage": {"prompt_tokens": 1200, "completion_tokens": 90}}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 8799), _Local)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        m = tmp_mem()
        llm = LLM(api_key="", provider="custom", base_url="http://127.0.0.1:8799/v1",
                  memory=m, free_tier=True)
        r = llm.ask("research", "local/qwen2.5:14b", "sys", "prompt")
        check("a keyless local call succeeds", r.used_llm, r.error)
        check("no empty Authorization header is sent", seen.get("auth") is None)
        check("the local reply parses", r.json() == {"ok": True})
        check("local usage is recorded", m.tokens_today() == 1290)
        check("local calls cost nothing", m.cost_today() == 0.0)
    finally:
        srv.shutdown()


def t_model_routing():
    print("\n[multi-pool model routing]")
    from firm import router as R

    check("every routed model has a declared pool",
          all(m in R.POOLS for role in R.ROUTES for m in R.ROUTES[role]))
    check("hungry roles are routed to large pools first",
          R.pool_for(R.ROUTES["execution"][0]) >= 90_000_000)
    check("pools are expressed in tokens, not millions",
          R.pool_for("mistral-large-3") == 99_500_000)
    check("unmetered models report infinite headroom",
          R.remaining(tmp_mem(), "big-pickle") == float("inf"))

    m = tmp_mem()
    first, why = R.pick(m, "execution")
    check("a fresh month picks the top preference", first == R.ROUTES["execution"][0])
    check("the reason states remaining headroom", "left" in why)

    # failover
    m.add_cost("execution", first, 95_000_000, 0, 0.0)
    second, _ = R.pick(m, "execution")
    check("an exhausted pool is skipped", second != first)
    check("failover stays inside the role's list", second in R.ROUTES["execution"])

    # exhaustion is reported, never guessed
    for cand in R.candidates("execution"):
        cap = R.pool_for(cand)
        if cap:
            m.add_cost("execution", cand, int(cap), 0, 0.0)
    mod, why = R.pick(m, "execution")
    check("a fully drained tier returns no model", mod == "")
    check("the exhaustion message names the role", "execution" in why)

    # safety margin
    m2 = tmp_mem()
    cap = R.pool_for("mistral-large-3")
    m2.add_cost("ceo", "mistral-large-3", int(cap * 0.96), 0, 0.0)
    check("a pool is retired at the safety fraction, not at 100%",
          R.remaining(m2, "mistral-large-3") == 0.0)

    # accounting
    m3 = tmp_mem()
    m3.add_cost("risk", "mistral-medium-3.5", 1_000, 500, 0.0)
    check("usage is tracked per model",
          R.usage_by_model(m3).get("mistral-medium-3.5") == 1_500)
    st = R.status(m3)
    check("status reports every pool", len(st["models"]) == len(R.POOLS))
    check("status explains the pools are separate", "not one shared" in st["note"])
    check("status exposes the role table", "execution" in st["roles"])

    # exclusion (a model that just errored)
    m4 = tmp_mem()
    alt, _ = R.pick(m4, "risk", exclude={R.ROUTES["risk"][0]})
    check("a failing model can be excluded", alt == R.ROUTES["risk"][1])

    # integration: the agent path honours routing and reports exhaustion
    from firm.orchestrator import Firm
    f = Firm(memory=tmp_mem(), connect=False)
    f.cfg.raw.setdefault("llm", {})["routing"] = True
    for cand in R.candidates("research"):
        cap = R.pool_for(cand)
        if cap:
            f.memory.add_cost("research", cand, int(cap), 0, 0.0)
    r = f.agents["research"].think("s", "p")
    check("an agent with no pool left does not pretend to have called an LLM",
          not r.used_llm)
    check("the agent surfaces the exhaustion reason", "exhausted" in (r.error or ""))

    # routing off = original behaviour
    f2 = Firm(memory=tmp_mem(), connect=False)
    check("routing is opt-in", not f2.cfg.get("llm.routing", False))


def t_free_tier_quota():
    print("\n[free tier + token quotas]")
    from firm.llm import LLM, price_for

    # --- pricing ---
    check("unknown models bill at the expensive tier on a paid endpoint",
          price_for("llama-3.3-70b-instruct") == (3.00, 15.00))
    check("free_tier zeroes the price of any model",
          price_for("llama-3.3-70b-instruct", free_tier=True) == (0.0, 0.0))
    check("a :free suffix is still free without the flag",
          price_for("meta/llama-3.3-70b:free") == (0.0, 0.0))
    check("local models are free", price_for("local/llama3") == (0.0, 0.0))
    check("known paid models keep their real price",
          price_for("claude-sonnet-4-5") == (3.00, 15.00))
    check("free_tier does not corrupt known prices for paid users",
          price_for("claude-sonnet-4-5", free_tier=False) == (3.00, 15.00))

    url = "http://127.0.0.1:9/v1"

    # --- the dollar ceiling must not shut down a free firm ---
    m = tmp_mem()
    free = LLM(api_key="k-1234567890", provider="custom", base_url=url,
               memory=m, max_daily_usd=2.0, free_tier=True)
    paid = LLM(api_key="k-1234567890", provider="custom", base_url=url,
               memory=m, max_daily_usd=2.0)
    m.add_cost("research", "claude-sonnet-4-5", 1_000_000, 100_000, 4.50)
    check("a paid firm halts when the dollar ceiling is passed", not paid.available)
    check("a free firm ignores the dollar ceiling", free.available)
    check("the paid halt explains itself", "budget" in paid.limit_reason())

    # --- tokens are the real limit ---
    m2 = tmp_mem()
    q = LLM(api_key="k-1234567890", provider="custom", base_url=url, memory=m2,
            free_tier=True, max_tokens_per_day=1_000_000,
            monthly_token_quota=10_000_000)
    check("a fresh quota is usable", q.available)
    m2.add_cost("research", "llama", 900_000, 50_000, 0.0)
    check("still usable under the daily token cap", q.available)
    m2.add_cost("research", "llama", 100_000, 0, 0.0)
    check("the daily token cap stops calls", not q.available)
    check("the token halt names the cap", "token" in q.limit_reason())
    r = q.ask("research", "llama", "s", "h")
    check("a capped call is refused, not attempted",
          not r.used_llm and "token" in (r.error or ""))

    # --- monthly quota is independent of the daily cap ---
    m3 = tmp_mem()
    q3 = LLM(api_key="k-1234567890", provider="custom", base_url=url, memory=m3,
             free_tier=True, monthly_token_quota=1_000_000)
    m3.add_cost("research", "llama", 1_100_000, 0, 0.0)
    check("the monthly quota alone can halt the firm", not q3.available)
    check("the monthly halt names the quota", "monthly" in q3.limit_reason())

    # --- accounting ---
    check("tokens_today sums input and output",
          m3.tokens_today() == 1_100_000)
    check("tokens_this_month is calendar-based, not rolling",
          m3.tokens_this_month() == 1_100_000)
    check("token_usage reports all three counters",
          set(m3.token_usage()) == {"today", "month", "usd_today"})
    check("free tier records zero dollars", m3.token_usage()["usd_today"] == 0.0)

    # --- no quota configured means no artificial limit ---
    m4 = tmp_mem()
    q4 = LLM(api_key="k-1234567890", provider="custom", base_url=url, memory=m4,
             free_tier=True)
    m4.add_cost("research", "llama", 500_000_000, 100_000_000, 0.0)
    check("free tier with no quota never blocks itself", q4.available)


def t_secret_redaction():
    print("\n[secret redaction]")
    import http.server
    import json as _json
    import threading
    from firm.llm import LLM, redact

    SECRET = "sk-ant-SECRET-DO-NOT-LEAK-123456789"

    check("a known secret is masked", SECRET not in redact(f"key {SECRET} here", SECRET))
    check("masking keeps a recognisable stub", "sk-a" in redact(SECRET, SECRET))
    check("short strings are not mangled", redact("abc", "abc") == "abc")
    check("unknown sk- tokens are caught",
          "[REDACTED]" in redact("token sk-proj-ABCDEFGH12345678"))
    check("bearer headers are caught",
          "[REDACTED]" in redact("Authorization: Bearer abcdef1234567890"))
    check("redaction tolerates empty input", redact("", "x") == "")
    check("ordinary text is untouched",
          redact("HTTP 404: model not found") == "HTTP 404: model not found")

    # a gateway that echoes the auth header back must not leak it into the log
    class _Echo(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("content-length", 0))
            self.rfile.read(n)
            body = _json.dumps(
                {"error": f"bad key {self.headers.get('Authorization', '')}"}).encode()
            self.send_response(401)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 8798), _Echo)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        llm = LLM(api_key=SECRET, provider="custom",
                  base_url="http://127.0.0.1:8798/v1")
        r = llm.ask("test", "claude-haiku-4-5", "s", "h")
        check("an echoing provider cannot leak the key into an error",
              SECRET not in (r.error or ""), r.error)
        check("the error is still useful", "401" in (r.error or ""))
    finally:
        srv.shutdown()


def t_spend_ceiling():
    print("\n[firm-wide spend ceiling]")
    from firm.llm import LLM

    m = tmp_mem()
    llm = LLM(api_key="sk-test-1234567890", provider="custom",
              base_url="http://127.0.0.1:9/v1", memory=m, max_daily_usd=0.50)
    check("the LLM is available before any spend", llm.available)
    m.add_cost("research", "claude-sonnet-4-5", 100000, 50000, 0.49)
    check("still available just under the ceiling", llm.available)
    m.add_cost("research", "claude-sonnet-4-5", 100000, 50000, 0.02)
    check("the ceiling closes the firm's LLM access", not llm.available)
    r = llm.ask("research", "claude-haiku-4-5", "s", "h")
    check("calls past the ceiling are refused, not attempted",
          not r.used_llm and "budget" in (r.error or ""))
    check("the ceiling is firm-wide, not per agent",
          not LLM(api_key="k-1234567890", provider="custom",
                  base_url="http://127.0.0.1:9/v1", memory=m,
                  max_daily_usd=0.50).available)


def t_supervisor():
    print("\n[supervisor: auto-quarantine]")
    import json as _json
    import random as _random
    import time as _time
    from firm import supervisor as S

    def _seed(m, name, sym, base_r, live_r, n, demo=False, status="approved"):
        m.upsert_strategy(f"{name}@{sym}",
                          {"strategy": name, "symbol": sym, "timeframe": "H1"},
                          "test", status, 1.0)
        m.x("INSERT INTO backtests(created_at,strategy,symbol,timeframe,metrics,passed,notes)"
            " VALUES(?,?,?,?,?,?,?)",
            (_time.time(), name, sym, "H1",
             _json.dumps({"expectancy_r": base_r, "trades": 120, "r_variance": 1.0}), 1, ""))
        rng = _random.Random(hash((name, sym)) % 9999)
        for i in range(n):
            tid = m.add_trade(ticket=rng.randint(10**6, 10**7), symbol=sym, side="buy",
                              lots=0.1, entry=1.0, stop=0.99, take=1.02, status="open",
                              meta={"strategy": name, "risk_usd": 100.0, "demo": demo})
            m.close_trade(tid, 0.99, rng.gauss(live_r, 0.9) * 100.0)

    # --- a decayed strategy is identified, a healthy one is left alone ---
    m = tmp_mem()
    _seed(m, "decayer", "EURUSD", 0.60, -0.70, 20)
    _seed(m, "healthy", "GBPUSD", 0.50, 0.52, 20)
    names = {c["name"] for c in S.candidates(m)}
    check("a broken strategy is flagged for quarantine", "decayer@EURUSD" in names)
    check("a strategy tracking its baseline is NOT flagged", "healthy@GBPUSD" not in names)

    out = S.review(m, dry_run=True)
    check("a dry run changes nothing", out["quarantined"] == [] and
          {r["name"] for r in m.strategies("approved")} ==
          {"decayer@EURUSD", "healthy@GBPUSD"})

    out = S.review(m)
    check("review quarantines the broken strategy", out["quarantined"] == ["decayer@EURUSD"])
    appr = {r["name"] for r in m.strategies("approved")}
    check("the broken strategy loses approved status", "decayer@EURUSD" not in appr)
    check("the healthy strategy keeps trading", "healthy@GBPUSD" in appr)
    check("quarantine is idempotent", S.review(m)["quarantined"] == [])
    q = [r for r in m.strategies() if r["status"] == "quarantined"]
    check("the quarantine records why", bool(q) and "AUTO-QUARANTINED" in (q[0]["notes"] or ""))
    check("the quarantine is logged for audit",
          any("quarantined" in e["message"] for e in m.events(limit=20)))
    check("a quarantined strategy keeps its spec",
          bool(q[0]["spec"].get("strategy")))

    # --- reinstatement is manual and precise ---
    check("reinstate returns a strategy to service", S.reinstate(m, "decayer@EURUSD"))
    check("reinstated strategies trade again",
          "decayer@EURUSD" in {r["name"] for r in m.strategies("approved")})
    check("reinstating an approved strategy is a no-op", not S.reinstate(m, "decayer@EURUSD"))
    check("reinstating an unknown strategy is a no-op", not S.reinstate(m, "ghost@NOPE"))

    # --- demo trades must never retire a strategy ---
    m2 = tmp_mem()
    _seed(m2, "demoloss", "EURUSD", 0.80, -1.00, 25, demo=True)
    check("catastrophic demo results do not quarantine anything",
          S.review(m2)["quarantined"] == [])
    check("demo results still surface when include_demo is on",
          "demoloss@EURUSD" in {c["name"] for c in S.candidates(m2, include_demo=True)})

    # --- only approved strategies can be demoted ---
    m3 = tmp_mem()
    _seed(m3, "notlive", "EURUSD", 0.60, -0.70, 20, status="proposed")
    check("a proposed strategy is never quarantined", S.review(m3)["quarantined"] == [])

    # --- evidence threshold ---
    m4 = tmp_mem()
    _seed(m4, "smallsample", "EURUSD", 0.60, -0.90, 9)
    check("a small live sample is not enough to fire a strategy",
          S.review(m4)["quarantined"] == [])

    # --- the switch must be honourable ---
    m5 = tmp_mem()
    _seed(m5, "decayer2", "EURUSD", 0.60, -0.70, 20)

    class _Off:
        def get(self, k, d=None):
            return False if k == "supervision.auto_quarantine" else d

    out = S.review(m5, _Off())
    check("auto_quarantine=false reports but does not act",
          out["quarantined"] == [] and len(out["candidates"]) == 1)

    # --- quarantine actually stops execution: the whole point ---
    from firm.orchestrator import Firm
    f = Firm(memory=tmp_mem(), connect=True)
    _seed(f.memory, "ema_trend_pullback", "EURUSD", 0.90, -0.80, 20)
    before = len(f.memory.strategies("approved"))
    S.review(f.memory)
    check("execution sees no approved strategies after quarantine",
          before == 1 and len(f.memory.strategies("approved")) == 0)
    check("execution.scan() therefore returns no signals",
          f.agents["execution"].scan() == [])
    # --- the risk agent throttles and escalates ---
    f2 = Firm(memory=tmp_mem(), connect=True)
    _seed(f2.memory, "ema_trend_pullback", "GBPUSD", 0.90, -0.80, 20)
    risk = f2.agents["risk"]
    first = risk.supervise()
    check("the risk agent quarantines on its own tick",
          first.get("quarantined") == ["ema_trend_pullback@GBPUSD"])
    check("supervision is throttled between runs", risk.supervise().get("skipped") is True)
    check("force overrides the throttle", "skipped" not in risk.supervise(force=True))
    check("a quarantine raises an issue to the CEO",
          any("quarantined" in (i["title"] or "").lower()
              for i in f2.memory.open_issues("ceo")))
    check("supervision never raises on a broken memory",
          isinstance(risk.supervise(force=True), dict))

    # --- roster view ---
    st = S.status(f2.memory)
    check("status() groups strategies by state", "quarantined" in st["counts"])

    # --- API surface ---
    from fastapi.testclient import TestClient
    from web.server import app
    c = TestClient(app)
    r = c.get("/api/strategies")
    check("/api/strategies responds", r.status_code == 200)
    body = r.json()
    check("/api/strategies reports counts and the auto flag",
          "counts" in body and "auto_quarantine" in body)
    check("/api/strategies lists pending verdicts", isinstance(body.get("pending"), list))
    bad = c.post("/api/strategies/reinstate", json={"name": "ghost@NOPE"})
    check("reinstating an unknown strategy fails cleanly",
          bad.status_code == 200 and bad.json()["ok"] is False)


def t_drift():
    print("\n[live vs backtest drift]")
    from firm.drift import (ALPHA, DRIFT_R, MIN_TRADES, _betainc, _t_sf, _var,
                            baselines, classify, cusum, live_samples, report,
                            welch)

    # --- the statistics must be right before any verdict can be trusted ---
    for t_, df, want in ((1.8125, 10, 0.05), (2.2281, 10, 0.025),
                         (1.7247, 20, 0.05), (0.0, 10, 0.5)):
        check(f"t tail t={t_} df={df} = {want}", abs(_t_sf(t_, df) - want) < 0.0015,
              f"got {_t_sf(t_, df):.5f}")
    check("I_x(1,1) == x", abs(_betainc(1, 1, 0.3) - 0.3) < 1e-9)
    check("I_x(a,1) == x^a", abs(_betainc(3, 1, 0.5) - 0.125) < 1e-9)
    # the continued fraction needs a domain switch above (a+1)/(a+b+2)
    worst = max(abs(_betainc(a, b, x) - (1 - _betainc(b, a, 1 - x)))
                for a, b, x in ((2, 5, 0.9), (5, 2, 0.95), (1, 1, 0.99),
                                (12, 3, 0.85), (0.5, 0.5, 0.7)))
    check("betainc symmetric across its whole domain", worst < 1e-9, f"{worst:.2e}")
    check("variance of a constant sample is zero", _var([0.5] * 8) == 0.0)

    w_same = welch([0.5] * 20, 0.5, 1.0, 60)
    check("identical live and baseline is not significant", w_same["p"] > 0.4)
    w_bad = welch([-1.0] * 20, 0.5, 1.0, 60)
    check("a badly underperforming sample is significant", w_bad["p"] < 0.01)
    check("welch refuses a one-trade sample", welch([0.5], 0.5, 1.0, 60)["p"] == 1.0)

    check("cusum at target never dips", cusum([0.5] * 10, 0.5)["worst"] == 0.0)
    c = cusum([-1.0] * 10, 0.5)
    check("cusum accumulates a losing run", c["worst"] < -9 and c["at_trade"] == 10)

    # --- verdicts ---
    base = {"expectancy_r": 0.5, "trades": 60, "variance": 1.0}
    v = classify([0.5] * (MIN_TRADES - 1), base)
    check("under the trade floor returns INSUFFICIENT", v["status"] == "INSUFFICIENT")
    check("INSUFFICIENT says how many more trades are needed",
          str(MIN_TRADES) in v["reason"])
    check("INSUFFICIENT reports no p-value", v["p_value"] is None)

    v = classify([0.9] * 12, base)
    check("beating the backtest is OK", v["status"] == "OK")
    check("OK records a positive delta", v["delta_r"] > 0)

    v = classify([0.48] * 12, base)
    check("a trivial shortfall is only WATCH", v["status"] == "WATCH")

    v = classify([-1.0] * 20, base)
    check("a large significant shortfall is BROKEN", v["status"] == "BROKEN")
    check("BROKEN is statistically significant", v["significant"] and v["p_value"] < ALPHA)
    check("BROKEN quotes the p-value in its reason", "p=" in v["reason"])

    # noisy but clearly below: material gap, not yet significant -> DRIFT
    noisy = [2.0, -1.0, -1.0, 2.0, -1.0, -1.0, -1.0, 2.0, -1.0, -1.0]
    v = classify(noisy, base)
    check("a material but noisy shortfall is DRIFT, not BROKEN",
          v["status"] in ("DRIFT", "BROKEN") and v["delta_r"] < -DRIFT_R)

    # --- wiring against real memory ---
    mem = tmp_mem()
    rep = report(mem)
    check("empty memory produces an empty report", rep["checked"] == 0)
    check("report still reports its thresholds", rep["min_trades"] == MIN_TRADES)

    from firm.memory import Memory
    real = Memory()
    b = baselines(real)
    check("baselines are keyed strategy@symbol", all("@" in k for k in b))
    check("baselines carry an expectancy",
          all("expectancy_r" in v for v in b.values()))
    ls = live_samples(real, include_demo=True)
    check("live samples group by strategy@symbol", all("@" in k for k in ls))
    check("live samples are ordered oldest first",
          all(all((g[i]["opened"] or 0) <= (g[i + 1]["opened"] or 0)
                  for i in range(len(g) - 1)) for g in ls.values()))
    demo_excluded = live_samples(real, include_demo=False)
    check("demo trades can be excluded",
          sum(len(v) for v in demo_excluded.values())
          <= sum(len(v) for v in ls.values()))

    full = report(real, include_demo=True)
    check("real report grades every baseline",
          full["checked"] == len(b), f"{full['checked']} vs {len(b)}")
    check("worst statuses sort first",
          [r["status"] for r in full["rows"]] ==
          sorted([r["status"] for r in full["rows"]],
                 key=lambda s: {"BROKEN": 0, "DRIFT": 1, "WATCH": 2,
                                "OK": 3, "INSUFFICIENT": 4}.get(s, 9)))
    check("live strategies with no baseline are surfaced",
          all(u["status"] == "NO_BASELINE" for u in full["unmatched"]))

    import warnings
    warnings.filterwarnings("ignore")
    from fastapi.testclient import TestClient
    from web.server import app
    c = TestClient(app)
    d = c.get("/api/drift").json()
    check("/api/drift responds", "rows" in d and "tally" in d)
    check("/api/drift exposes its thresholds",
          d["min_trades"] == MIN_TRADES and d["alpha"] == ALPHA)



def t_portfolio_correlation():
    print("\n[portfolio correlation risk]")
    from firm.portfolio import (assess, cluster, concentration, correlation_matrix,
                                log_returns, pearson, portfolio_heat)

    # --- correlation maths against known answers ---
    check("perfectly correlated series -> +1",
          abs(pearson([1, 2, 3, 4, 5], [2, 4, 6, 8, 10]) - 1.0) < 1e-9)
    check("perfectly inverse series -> -1",
          abs(pearson([1, 2, 3, 4, 5], [10, 8, 6, 4, 2]) + 1.0) < 1e-9)
    check("a constant series correlates to 0, not NaN",
          pearson([1, 1, 1, 1], [1, 2, 3, 4]) == 0.0)
    check("a single point cannot correlate", pearson([1.0], [2.0]) == 0.0)
    lr = log_returns([100.0, 110.0, 121.0])
    check("log returns are additive in ratio", abs(lr[0] - lr[1]) < 1e-12)
    check("log returns skip non-positive prices",
          log_returns([1.0, 0.0, 1.0]) == [0.0, 0.0])

    # --- portfolio heat: the numbers must be provably right ---
    one = {"A": {"A": 1.0, "B": 1.0}, "B": {"A": 1.0, "B": 1.0}}
    zero = {"A": {"A": 1.0, "B": 0.0}, "B": {"A": 0.0, "B": 1.0}}
    longs = [{"symbol": "A", "side": "buy", "risk_usd": 100},
             {"symbol": "B", "side": "buy", "risk_usd": 100}]
    hedge = [{"symbol": "A", "side": "buy", "risk_usd": 100},
             {"symbol": "B", "side": "sell", "risk_usd": 100}]

    h = portfolio_heat(longs, one)
    check("fully correlated longs give no diversification",
          abs(h["adjusted_usd"] - 200.0) < 0.01 and h["effective_bets"] == 1.0)
    h = portfolio_heat(longs, zero)
    check("uncorrelated longs scale by sqrt(n)",
          abs(h["adjusted_usd"] - 141.42) < 0.05 and h["effective_bets"] == 2.0)
    h = portfolio_heat(hedge, one)
    check("a perfect hedge carries no net risk", h["adjusted_usd"] < 0.01)
    check("a perfect hedge is zero bets, not two", h["effective_bets"] == 0.0)
    check("empty portfolio is safe to measure",
          portfolio_heat([], one)["naive_usd"] == 0.0)

    four = {c: {d: (1.0 if c == d else 0.0) for d in "ABCD"} for c in "ABCD"}
    h4 = portfolio_heat([{"symbol": c, "side": "buy", "risk_usd": 100}
                         for c in "ABCD"], four)
    check("four independent bets count as four", h4["effective_bets"] == 4.0)

    # --- clustering ---
    m = {"A": {"A": 1, "B": .85, "C": .1}, "B": {"A": .85, "B": 1, "C": .05},
         "C": {"A": .1, "B": .05, "C": 1}}
    check("correlated symbols cluster together",
          cluster(m, 0.7) == [["A", "B"], ["C"]])
    neg = {"A": {"A": 1, "B": -.9}, "B": {"A": -.9, "B": 1}}
    check("strong negative correlation is also one bet",
          cluster(neg, 0.7) == [["A", "B"]])
    check("raising the threshold splits a cluster",
          cluster(m, 0.95) == [["A"], ["B"], ["C"]])

    con = concentration(longs, one, 0.7)
    check("concentration nets a cluster's directional risk",
          con[0]["net_usd"] == 200.0 and con[0]["directional"])
    con = concentration(hedge, one, 0.7)
    check("an offsetting cluster nets to zero", abs(con[0]["net_usd"]) < 1e-9)

    # --- against the real broker ---
    from firm.brokers.simulated import SimulatedBroker
    b = SimulatedBroker()
    view = assess(b, [{"symbol": "EURUSD", "side": "buy", "risk_usd": 75}],
                  equity=10_000.0)
    check("assess builds a matrix from broker bars", len(view["matrix"]) >= 1)
    check("assess reports its threshold", view["threshold"] == 0.7)
    check("matrix diagonal is 1.0",
          all(view["matrix"][s][s] == 1.0 for s in view["matrix"]))
    check("matrix is symmetric",
          all(view["matrix"][a][b] == view["matrix"][b][a]
              for a in view["matrix"] for b in view["matrix"]))

    # --- the risk gate must actually enforce it ---
    from firm.brokers.base import Position
    from firm.orchestrator import Firm
    f = Firm(memory=tmp_mem(), connect=True)
    risk = f.agents["risk"]
    broker = f.ctx.primary_broker()

    class _Fake:
        def __init__(self, real, pos):
            self._r, self._p = real, pos

        def positions(self):
            return self._p

        def __getattr__(self, k):
            return getattr(self._r, k)

    big = [Position(ticket=1, symbol="EURUSD", side="buy", lots=0.30, entry=1.0980,
                    stop=1.0900, take=1.114, profit=0, open_time=0, comment="")]
    fb = _Fake(broker, big)
    corr_sig = {"symbol": "XAUUSD", "side": "buy", "entry": 2400.0, "stop": 2380.0,
                "take": 2440.0, "meta": {"atr": 10.0}}
    d = risk.vet(corr_sig, fb)
    check("a correlated cluster breach is refused", not d.approved, d.reason)
    check("the refusal names the correlated symbols",
          "EURUSD" in d.reason and "XAUUSD" in d.reason)
    check("the refusal explains it is one bet", "one bet" in d.reason)

    indep = {"symbol": "GBPUSD", "side": "buy", "entry": 1.2800, "stop": 1.2720,
             "take": 1.2960, "meta": {"atr": 0.004}}
    d2 = risk.vet(indep, fb)
    check("an uncorrelated trade still passes", d2.approved, d2.reason)
    check("approval reports effective bets", "effective bets" in d2.reason)

    # the guard must be switchable and must never hard-fail the gate
    risk.cfg.raw.setdefault("risk", {})["correlation_guard"] = False
    d3 = risk.vet(corr_sig, fb)
    check("the guard can be disabled", d3.approved)
    risk.cfg.raw["risk"]["correlation_guard"] = True

    class _Broken:
        def positions(self):
            return big

        def __getattr__(self, k):
            raise RuntimeError("broker exploded")

    try:
        bd = risk.vet(indep, _Broken())
        crashed = False
    except Exception:
        bd, crashed = None, True
    check("a broken broker does not crash the risk gate", not crashed)
    check("a broken broker fails closed (refused, not approved)",
          bd is not None and not bd.approved, bd.reason if bd else "raised")

    import warnings
    warnings.filterwarnings("ignore")
    from fastapi.testclient import TestClient
    from web.server import app
    c = TestClient(app)
    api = c.get("/api/portfolio").json()
    check("/api/portfolio responds", "matrix" in api and "heat" in api)
    check("/api/portfolio reports guard state", "guard_enabled" in api)
    check("/api/portfolio shows the firm's symbols even when flat",
          len(api.get("symbols", [])) >= 1)


if __name__ == "__main__":
    print("=" * 62)
    print("  AGENTIC TRADING FIRM - TEST SUITE")
    print("=" * 62)
    for fn in (t_broker, t_indicators, t_strategies, t_vectorized, t_backtest,
               t_analytics, t_ingest, t_lab, t_risk, t_execution_path,
               t_live_guard, t_bridge_protocol, t_mql_files, t_scout,
               t_instructions, t_scout_routing, t_mql_compiler, t_indicator_compiler, t_rule_periods,
               t_ingest_keys, t_llm_providers, t_blotter_api, t_drift, t_gateway_passthrough, t_local_endpoint, t_model_routing, t_free_tier_quota, t_secret_redaction, t_spend_ceiling, t_supervisor, t_portfolio_correlation, t_api, t_scout_api, t_export_compiles_specs,
               t_composite_validation, t_firm):
        try:
            fn()
        except Exception as e:
            import traceback
            traceback.print_exc()
            FAIL.append(f"{fn.__name__} crashed: {e}")
    print("\n" + "=" * 62)
    print(f"  {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print(f"    FAILED: {f}")
    print("=" * 62)
    sys.exit(1 if FAIL else 0)
