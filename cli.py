#!/usr/bin/env python3
"""Agentic Trading Firm - command line.

    python cli.py setup                 intake interview -> config/firm.yaml
    python cli.py status                firm + account snapshot
    python cli.py ask "..."             raise an issue with the CEO
    python cli.py inbox                 read the CEO's reports back to you
    python cli.py run [--cycles N]      run the firm loop
    python cli.py brokers               test MT4/MT5 connectivity
    python cli.py backtest STRAT SYMBOL run a one-off backtest
    python cli.py golive                arm live trading (double confirmation)
    python cli.py halt / resume         kill switch control
    python cli.py dashboard             web UI on :8000
"""
from __future__ import annotations

import argparse
import json
import sys
import textwrap
import time
from pathlib import Path

import yaml

from firm.config import CONFIG_PATH, EXAMPLE_PATH, load_config
from firm.memory import Memory
from firm.orchestrator import Firm

ROOT = Path(__file__).resolve().parent
BAR = "=" * 72


def _p(s: str = "") -> None:
    print(s)


# ---------------------------------------------------------------- setup
def cmd_setup(args) -> None:
    cfg_raw = yaml.safe_load(EXAMPLE_PATH.read_text())
    _p(BAR); _p("  AGENTIC TRADING FIRM - INTAKE INTERVIEW"); _p(BAR)
    _p("Five questions and your firm is hired. Enter accepts the default.\n")

    name = input("1. What is the firm called?  [Demo Capital] ").strip() or "Demo Capital"
    cfg_raw["firm"]["name"] = name

    _p("\n2. What is this firm for? (grow capital / generate income / test a strategy)")
    goal = input("   > ").strip() or ("Compound capital steadily with strictly "
                                      "limited drawdown.")
    cfg_raw["firm"]["goal"] = goal

    _p("\n3. Strategy: (a) I have one to describe  (b) build from scratch  "
       "(c) use the built-in library")
    s = (input("   [c] ").strip().lower() or "c")
    if s == "a":
        desc = input("   Describe it: ").strip()
        cfg_raw["firm"]["strategy_preference"] = "describe_own"
        cfg_raw["firm"]["goal"] += f"\nBoard strategy brief: {desc}"
    else:
        cfg_raw["firm"]["strategy_preference"] = (
            "build_from_scratch" if s == "b" else "library")

    _p("\n4. Team: (a) standard 6 agents - CEO, research, backtest, risk, execution, "
       "cost optimizer  (b) custom")
    t = (input("   [a] ").strip().lower() or "a")
    if t == "b":
        for ag in ["research", "backtest", "risk", "execution", "cost_optimizer"]:
            keep = (input(f"   include {ag}? [Y/n] ").strip().lower() or "y")
            cfg_raw["agents"][ag]["enabled"] = keep.startswith("y")
        if not cfg_raw["agents"]["risk"]["enabled"]:
            _p("   -> risk agent is mandatory, re-enabling it.")
            cfg_raw["agents"]["risk"]["enabled"] = True

    _p("\n5. Risk tolerance: (a) conservative  (b) moderate  (c) aggressive")
    r = (input("   [b] ").strip().lower() or "b")
    presets = {
        "a": dict(max_risk_per_trade_pct=0.25, max_daily_loss_pct=1.5,
                  max_total_drawdown_pct=6.0, max_open_positions=2),
        "b": dict(max_risk_per_trade_pct=0.75, max_daily_loss_pct=3.0,
                  max_total_drawdown_pct=12.0, max_open_positions=4),
        "c": dict(max_risk_per_trade_pct=1.5, max_daily_loss_pct=5.0,
                  max_total_drawdown_pct=20.0, max_open_positions=8),
    }
    cfg_raw["risk"].update(presets.get(r, presets["b"]))
    cfg_raw["firm"]["risk_tolerance"] = {"a": "conservative", "b": "moderate",
                                         "c": "aggressive"}.get(r, "moderate")

    # --- platform ---
    _p("\n6. Which terminal will the firm trade through?")
    _p("   (a) none yet - simulated paper engine (default, no terminal needed)")
    _p("   (b) MT5 via the ArenaBridge EA (any OS)")
    _p("   (c) MT4 via the ArenaBridge EA (any OS)")
    _p("   (d) MT5 via the MetaTrader5 python package (Windows/Wine)")
    b = (input("   [a] ").strip().lower() or "a")
    kinds = {"a": "simulated", "b": "mt5_bridge", "c": "mt4_bridge", "d": "mt5_native"}
    kind = kinds.get(b, "simulated")
    cfg_raw["broker"]["kind"] = kind
    plat = "MT4" if kind == "mt4_bridge" else "MT5"
    if kind in ("mt4_bridge", "mt5_bridge"):
        _p(f"\n   In {plat}: File -> Open Data Folder -> "
           f"MQL{'4' if plat == 'MT4' else '5'} -> Files")
        d = input("   Paste that full path: ").strip()
        if d:
            cfg_raw["broker"]["bridge"]["files_dir"] = d
        _p(f"   Remember to copy mql/ArenaBridge.mq{'4' if plat=='MT4' else '5'} into "
           f"MQL{'4' if plat=='MT4' else '5'}/Experts, compile it, and attach it to a chart.")
    elif kind == "mt5_native":
        login = input("   MT5 login (blank = use the already-open terminal): ").strip()
        if login:
            cfg_raw["broker"]["mt5_native"]["login"] = int(login)
            cfg_raw["broker"]["mt5_native"]["password"] = input("   Password: ").strip()
            cfg_raw["broker"]["mt5_native"]["server"] = input("   Server: ").strip()

    cfg_raw["broker"]["accounts"] = [{
        "id": f"{plat.lower()}-main" if kind != "simulated" else "sim-main",
        "kind": kind, "platform": plat, "enabled": True, "starting_balance": 10000}]

    syms = input("\n7. Symbols to trade [EURUSD,GBPUSD,XAUUSD]: ").strip()
    if syms:
        cfg_raw["trading"]["symbols"] = [s.strip().upper() for s in syms.split(",") if s.strip()]
    tf = input("8. Timeframe [H1]: ").strip().upper()
    if tf:
        cfg_raw["trading"]["timeframe"] = tf

    key = input("\n9. Anthropic API key (blank = run on the deterministic engine, $0): ").strip()
    if key:
        (ROOT / ".env").write_text(f"ANTHROPIC_API_KEY={key}\n")
        _p("   Saved to .env (git-ignored).")

    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(yaml.safe_dump(cfg_raw, sort_keys=False, allow_unicode=True))

    _p("\n" + BAR)
    _p(f"  {name} is hired. 6 agents ready.")
    _p(BAR)
    _p(f"  Config : {CONFIG_PATH}")
    _p(f"  Mode   : PAPER TRADING (no real orders possible)")
    _p(f"  Broker : {kind} ({plat})")
    _p(f"  Symbols: {', '.join(cfg_raw['trading']['symbols'])} "
       f"on {cfg_raw['trading']['timeframe']}")
    _p("\nNext:")
    _p("  python cli.py brokers                 # verify the terminal link")
    _p('  python cli.py ask "Get to work"       # give the CEO its first task')
    _p("  python cli.py run                     # start the autonomous loop")
    _p("  python cli.py dashboard               # watch it work")


# ---------------------------------------------------------------- status
def cmd_status(args) -> None:
    f = Firm()
    s = f.status()
    _p(BAR); _p(f"  {s['firm']}  [{s['mode']}]"); _p(BAR)
    _p(f"Symbols     : {', '.join(s['symbols'])} on {s['timeframe']}")
    _p(f"Equity      : ${s['equity']:,.2f}   Day P&L ${s['day_pnl']:+,.2f}   "
       f"DD {s['drawdown_pct']:.2f}%")
    ks = s["kill_switch"]
    _p(f"Kill switch : {'ENGAGED - ' + ks['reason'] if ks['engaged'] else 'clear'}")
    _p(f"LLM spend   : ${s['llm_spend_24h']:.4f} (24h)")
    _p("\nAccounts:")
    for a in s["accounts"]:
        if "error" in a:
            _p(f"  {a['id']:<12} {a['platform']:<4} OFFLINE - {a['error']}")
        else:
            _p(f"  {a['id']:<12} {a['platform']:<4} {a['login']}@{a['server'] or 'sim'} "
               f"bal ${a['balance']:,.2f} eq ${a['equity']:,.2f}")
    _p("\nStrategies:")
    for k in ("approved", "proposed", "rejected"):
        names = s["strategies"][k]
        _p(f"  {k:<9}: {len(names)}" + (f"  {', '.join(names[:4])}" if names else ""))
    _p(f"\nTrades: {len(s['open_trades'])} open, {s['closed_trades']} closed, "
       f"win rate {s['win_rate']}%, realised ${s['realised_pnl']:+,.2f}")
    for t in s["open_trades"]:
        _p(f"  {t['symbol']} {t['side']} {t['lots']} @{t['entry']} "
           f"[{t['mode']}] {t['platform']}/{t['account']}")
    if s["open_issues"]:
        _p(f"\nOpen assignments: {len(s['open_issues'])}")
        for i in s["open_issues"][:8]:
            _p(f"  #{i['id']} {i['assignee']:<14} {i['status']:<12} {i['title'][:44]}")


# ---------------------------------------------------------------- ask / inbox
def cmd_ask(args) -> None:
    f = Firm()
    text = " ".join(args.text)
    title = text[:70] + ("..." if len(text) > 70 else "")
    iid = f.board_request(title, text)
    _p(f"Board issue #{iid} raised with the CEO.")
    if args.wait:
        _p("Working (Ctrl-C to detach; the firm keeps the task queued)...\n")
        deadline = time.time() + args.wait
        try:
            while time.time() < deadline:
                f.tick()
                issue = f.memory.issue(iid)
                if issue and issue["status"] == "done":
                    _p(BAR); _p(f"  CEO report on #{iid}"); _p(BAR)
                    _p(issue["result"] or "")
                    return
                time.sleep(2)
            _p("Still working. Check `python cli.py inbox` shortly.")
        except KeyboardInterrupt:
            _p("\nDetached. The assignment stays queued.")
    else:
        _p("Run `python cli.py run` (or the dashboard) to let the firm work on it.")


def cmd_inbox(args) -> None:
    f = Firm(connect=False)
    rows = f.inbox(args.limit)
    if not rows:
        _p("Inbox empty. Ask the CEO something: python cli.py ask \"...\"")
        return
    for r in rows:
        _p(BAR)
        _p(f"#{r['id']} [{r['status']}] {r['title']}")
        _p(time.strftime("%Y-%m-%d %H:%M", time.localtime(r["created_at"])))
        _p(BAR)
        for c in r["comments"]:
            _p(f"\n-- {c['author']} --")
            _p(textwrap.indent((c["body"] or "").strip(), "   "))
        _p()


# ---------------------------------------------------------------- brokers
def cmd_brokers(args) -> None:
    cfg = load_config()
    _p(BAR); _p("  BROKER CONNECTIVITY"); _p(BAR)
    from firm.brokers import build_broker
    for acc in cfg.accounts():
        _p(f"\n{acc.get('id')}  kind={acc.get('kind')}  platform={acc.get('platform')}")
        try:
            br = build_broker(acc, cfg)
            br.connect()
            a = br.account()
            _p(f"  connected: {a.platform} login {a.login} @ {a.server or 'simulated'}")
            _p(f"  {a.company}")
            _p(f"  balance ${a.balance:,.2f} | equity ${a.equity:,.2f} | "
               f"free margin ${a.free_margin:,.2f} | leverage 1:{a.leverage}")
            for sym in cfg.symbols[:3]:
                try:
                    t = br.tick(sym)
                    sp = br.symbol_spec(sym)
                    bars = br.bars(sym, cfg.timeframe, 50)
                    _p(f"  {sym}: bid {t.bid} ask {t.ask} "
                       f"spread {(t.ask-t.bid)/sp.point:.0f}pts | "
                       f"{len(bars)} {cfg.timeframe} bars | lot step {sp.volume_step}")
                except Exception as e:
                    _p(f"  {sym}: FAILED - {e}")
            _p(f"  open positions: {len(br.positions())}")
            br.disconnect()
        except Exception as e:
            _p(f"  FAILED: {e}")
            if "bridge" in str(acc.get("kind", "")):
                _p("  -> Is ArenaBridge attached to a chart with algo trading enabled?")
                _p(f"  -> Does files_dir point at the terminal's MQL{'4' if acc.get('platform')=='MT4' else '5'}/Files folder?")


# ---------------------------------------------------------------- backtest
def cmd_backtest(args) -> None:
    from firm.backtester import backtest, walk_forward
    from firm.strategies.library import all_strategies
    cfg = load_config()
    if args.strategy not in all_strategies():
        _p("Available strategies:")
        for k, v in all_strategies().items():
            _p(f"  {k:<22} {v['description'][:70]}")
        return
    f = Firm()
    br = f.ctx.primary_broker()
    if not br:
        _p("No broker connected."); return
    sym = args.symbol or cfg.symbols[0]
    tf = args.timeframe or cfg.timeframe
    bars = br.bars(sym, tf, args.bars)
    spec = br.symbol_spec(sym)
    _p(f"Backtesting {args.strategy} on {sym} {tf} over {len(bars)} bars...")
    r = backtest(args.strategy, bars, sym, tf, spec=spec)
    _p(json.dumps(r.summary(), indent=2))
    ok, why = r.verdict()
    _p(f"\nVerdict: {'PASS' if ok else 'FAIL'} - {why}")
    if args.walk:
        _p("\nWalk-forward:")
        _p(json.dumps(walk_forward(args.strategy, bars, sym, tf,
                                   {"rr": [1.5, 2.0, 2.5]}, spec=spec), indent=2))


# ---------------------------------------------------------------- run
def cmd_run(args) -> None:
    f = Firm()
    s = f.status()
    _p(BAR)
    _p(f"  {s['firm']} running - {s['mode']} mode")
    _p(f"  accounts: {', '.join(a['id'] + '/' + a['platform'] for a in s['accounts'])}")
    _p(f"  Ctrl-C to stop")
    _p(BAR)
    if not f.memory.open_issues() and not args.cycles:
        f.board_request(
            "Standing mandate",
            "Run the firm continuously: research the market regime, validate "
            "strategies by backtest, keep risk inside the envelope and execute "
            "approved setups. Report anything material to the board.")
    f.run(cycles=args.cycles)


# ---------------------------------------------------------------- live / halt
def cmd_golive(args) -> None:
    cfg = load_config()
    _p(BAR); _p("  ARMING LIVE TRADING"); _p(BAR)
    _p("This lets the agents send REAL orders to your broker with REAL money.")
    _p("You can lose everything. Backtests do not predict the future.\n")
    _p(f"Firm     : {cfg.firm_name}")
    _p(f"Accounts : {[a.get('id') + '/' + str(a.get('kind')) for a in cfg.accounts()]}")
    _p(f"Risk     : {cfg.get('risk.max_risk_per_trade_pct')}% per trade, "
       f"{cfg.get('risk.max_daily_loss_pct')}% daily stop, "
       f"{cfg.get('risk.max_total_drawdown_pct')}% max drawdown\n")
    if input('Type exactly "I ACCEPT THE RISK": ').strip() != "I ACCEPT THE RISK":
        _p("Aborted. Still in paper mode."); return
    if input(f'Type the firm name "{cfg.firm_name}" to confirm: ').strip() != cfg.firm_name:
        _p("Aborted. Still in paper mode."); return
    if any(str(a.get("kind", "")).startswith("sim") for a in cfg.accounts()):
        _p("\nNOTE: simulated accounts stay on paper regardless - "
           "point broker.accounts at a real MT4/MT5 terminal first.")
    cfg.raw["trading"]["paper_trading"] = False
    cfg.raw["trading"]["allow_live_orders"] = True
    cfg.save()
    _p("\nLIVE TRADING ARMED. Revert any time with: python cli.py paper")


def cmd_paper(args) -> None:
    cfg = load_config()
    cfg.raw["trading"]["paper_trading"] = True
    cfg.raw["trading"]["allow_live_orders"] = False
    cfg.save()
    _p("Back to PAPER mode. No real orders can be sent.")


def cmd_halt(args) -> None:
    f = Firm(connect=False)
    f.agents["risk"].halt(args.reason or "manual halt by board", hours=args.hours)
    _p(f"Trading halted for {args.hours}h. Open positions will be flattened next tick.")


def cmd_resume(args) -> None:
    f = Firm(connect=False)
    f.agents["risk"].resume()
    _p("Kill switch cleared.")



def cmd_llm(args) -> None:
    """Verify the configured LLM provider end to end, cheaply."""
    from firm.config import load_config
    from firm.llm import LLM, PROVIDERS

    cfg = load_config()
    prov = cfg.llm_provider
    proto, default_url, env_var = PROVIDERS.get(prov, PROVIDERS["anthropic"])
    llm = LLM(api_key=cfg.llm_key, provider=prov, base_url=cfg.llm_base_url,
              model_prefix=cfg.llm_model_prefix,
              max_daily_usd=float(cfg.get("llm.max_daily_usd", 10.0)),
              free_tier=bool(cfg.get("llm.free_tier", False)),
              max_tokens_per_day=int(cfg.get("llm.max_tokens_per_day", 0) or 0),
              monthly_token_quota=int(cfg.get("llm.monthly_token_quota", 0) or 0))

    key = cfg.llm_key
    masked = f"{key[:6]}...{key[-4:]} ({len(key)} chars)" if key else "(none)"
    print(f"provider     {prov}  [{proto} protocol]")
    print(f"endpoint     {llm.base_url or '(not set)'}")
    print(f"key source   {env_var} / llm.api_key -> {masked}")
    print(f"model prefix {cfg.llm_model_prefix or '(none)'}")
    print(f"enabled      {llm.enabled}")
    if not llm.enabled:
        print("\nNo key found. Put it in .env (which is gitignored):")
        print(f"    echo '{env_var}=sk-your-key-here' >> .env")
        print("Then set llm.provider in config/firm.yaml to match.")
        return

    model = args.model or cfg.get("agents.research.model", "claude-sonnet-4-5")
    print(f"\nasking {llm.resolve_model(model)} for a one-line reply...")
    r = llm.ask("cli", model,
                "You are terse. Reply with STRICT JSON only.",
                'Reply exactly: {"ok": true, "provider": "<your model name>"}',
                max_tokens=120)
    if r.error:
        print(f"FAILED  {r.error}")
        print("\nCommon causes: wrong provider for this key, a model name the "
              "provider does not serve, or no credit on the account.")
        return
    print(f"OK      {r.text.strip()[:180]}")
    print(f"tokens  in {r.input_tokens} / out {r.output_tokens}  "
          f"est ${r.usd:.5f}")
    parsed = r.json()
    print(f"json    {'parsed cleanly' if parsed else 'NOT valid JSON - agents will fall back'}")



def cmd_history(args) -> None:
    """Download M1 once; every other timeframe is derived from it."""
    from firm import history as H
    from firm.config import load_config
    from firm.orchestrator import Firm

    if args.list:
        rows = H.stored_symbols()
        if not rows:
            print("No stored history. Download with:")
            print("    python cli.py history --download EURUSD --days 3650")
            print("or import a broker/HistData CSV:")
            print("    python cli.py history --import-csv EURUSD data/EURUSD_M1.csv")
            return
        print(f"{'symbol':10} {'bars':>10} {'days':>7} {'complete':>9} {'gaps':>5}  size")
        for r in rows:
            print(f"{r['symbol']:10} {r['bars']:>10,} {r['days']:>7.0f} "
                  f"{r['completeness']:>8.1f}% {r['gap_count']:>5}  {r['size_mb']} MB")
        return

    if args.import_csv:
        sym, path = args.import_csv
        out = H.import_csv(sym, path, tz_offset_hours=args.tz_offset)
        print(f"{out['symbol']}: imported {out['imported']:,} M1 bars "
              f"({out['skipped']} unparseable rows skipped)")
        print(f"  span {out['days']:.0f} days, completeness {out['completeness']}%, "
              f"{out['gap_count']} gaps")
        if out["gaps"]:
            print("  largest gaps:")
            for g in out["gaps"][:5]:
                print(f"    {g['hours']:.1f}h")
        return

    if args.download:
        cfg = load_config()
        f = Firm(cfg=cfg, memory=None, connect=True)
        br = f.ctx.primary_broker()
        for sym in args.download:
            print(f"downloading {sym} M1, target {args.days} days...")
            out = H.download_m1(br, sym, days=args.days)
            if out.get("error"):
                print(f"  FAILED: {out['error']}")
                continue
            print(f"  stored {out['stored']:,} bars, {out.get('days', 0):.0f} days, "
                  f"completeness {out.get('completeness', 0)}%")
        return

    if args.from_ticks:
        sym, path = args.from_ticks
        import csv as _csv
        rows = []
        bad = 0
        with open(path, newline="") as fh:
            sample = fh.read(4096)
            fh.seek(0)
            delim = ";" if sample.count(";") > sample.count(",") else ","
            rdr = _csv.reader(fh, delimiter=delim)
            if any(ch.isalpha() for ch in sample.split("\n")[0].replace("T", "")):
                next(rdr, None)
            for row in rdr:
                try:
                    ts = row[0].strip()
                    t = float(ts) if H._is_number(ts) else H._to_epoch(ts)
                    nums = [float(x) for x in row[1:4] if H._is_number(x)]
                    if not nums:
                        raise ValueError
                    # bid/ask -> mid when both present, else the single price
                    price = (nums[0] + nums[1]) / 2 if len(nums) >= 2 else nums[0]
                    rows.append((t - args.tz_offset * 3600, price))
                except (ValueError, IndexError):
                    bad += 1
        bars = H.ticks_to_m1(rows)
        if bars:
            H.save_m1(sym, bars)
        cov = H.coverage(bars, "M1")
        print(f"{sym.upper()}: {len(rows):,} ticks -> {len(bars):,} M1 bars "
              f"({bad} unparseable rows)")
        print(f"  span {cov['days']:.0f} days, completeness {cov['completeness']}%")
        print("  ticks are now redundant: every timeframe derives from this M1.")
        return

    if args.check_ticks:
        sym = args.check_ticks
        m1 = H.load_m1(sym)
        if not m1:
            print(f"no stored M1 for {sym}")
            return
        print(f"{sym}: would tick data change your backtests?\n")
        print(f"  {'stop size':28} {'ambiguous':>10} {'verdict'}")
        for frac, label in ((0.25, "very tight (0.25x M1 range)"),
                            (0.40, "tight (0.40x M1 range)"),
                            (0.50, "normal (0.50x M1 range)")):
            a = H.ambiguous_fraction(m1, frac)
            print(f"  {label:28} {a['pct']:>9.2f}% {a['verdict']}")
        print("\n  Ambiguity only matters when a stop AND target sit inside one")
        print("  M1 candle. ATR-sized stops (this firm's default is 1.6-2.0x ATR)")
        print("  span many minutes, so bar data resolves them.")
        return

    if args.resample:
        sym, tf = args.resample
        m1 = H.load_m1(sym)
        if not m1:
            print(f"no stored M1 for {sym}")
            return
        out = H.resample(m1, tf)
        print(f"{sym}: {len(m1):,} M1 bars -> {len(out):,} {tf} bars")
        cov = H.coverage(out, tf)
        print(f"  span {cov['days']:.0f} days, {cov['gap_count']} gaps")
        return

    print("Nothing to do. Try --list, --download, --import-csv or --resample.")


def cmd_bulktest(args) -> None:
    """Mass-test strategies and rank them for survival, not for return."""
    from firm import history as H
    from firm import screen as S
    from firm.config import load_config
    from firm.lab import Lab
    from firm.orchestrator import Firm

    cfg = load_config()
    f = Firm(cfg=cfg, memory=None, connect=True)
    broker = f.ctx.primary_broker()

    symbols = args.symbols or list(cfg.symbols)
    timeframes = args.timeframes or ["M15", "H1", "H4"]

    stored = {r["symbol"] for r in H.stored_symbols()}
    use_local = bool(stored & {s.upper() for s in symbols})
    if use_local:
        broker = H.HistoryBroker(broker, symbols)
        print(f"using stored M1 history for {sorted(stored)} "
              "(all timeframes resampled from it)")
    else:
        print("no stored M1 - using live broker history. "
              "Download once with: python cli.py history --download EURUSD")

    lab = Lab(broker=broker)
    print(f"\nsweeping {len(symbols)} symbol(s) x {len(timeframes)} timeframe(s), "
          f"{args.bars} bars each...")

    def prog(p):
        print(f"  [{p['done']}/{p['total']}] {p['current']}", flush=True)

    results = lab.sweep(symbols, timeframes, max_combos=args.max_combos,
                        bars_count=args.bars, optimize_each=not args.fast,
                        progress=prog)

    rules = dict(S.DEFAULTS)
    if args.min_per_day is not None:
        rules["min_trades_per_day"] = args.min_per_day
    if args.max_dd is not None:
        rules["max_drawdown_r"] = args.max_dd

    out = S.screen(results, rules, bars_tested=args.bars)
    print(f"\n{'='*74}")
    print(f"tested {out['tested']} configurations -> "
          f"{out['deploy_count']} survive, {out['reject_count']} rejected")
    print(f"{'='*74}")

    if out["deploy"]:
        print(f"\n{'strategy':26} {'sym':8} {'tf':4} {'t/day':>6} {'PF':>5} "
              f"{'expR':>6} {'maxDD':>6} {'surv':>5}")
        for g in out["deploy"][:args.top]:
            print(f"{g['strategy'][:26]:26} {g['symbol']:8} {g['timeframe']:4} "
                  f"{g['trades_per_day']:>6.2f} {g['profit_factor']:>5.2f} "
                  f"{g['expectancy_r']:>6.3f} {g['max_drawdown_r']:>6.1f} "
                  f"{g['survival_score']:>5.2f}")
    else:
        print("\nNothing survived the screen. That is a result, not a failure -")
        print("it means none of these configurations is safe to trade daily.")

    if out["common_failures"]:
        print("\nwhy the rest were rejected:")
        for reason, n in out["common_failures"]:
            print(f"  {n:>4}x  {reason}")

    if args.save and out["deploy"]:
        import json as _json
        from pathlib import Path as _P
        dest = _P(args.save)
        dest.write_text(_json.dumps(out["deploy"], indent=2))
        print(f"\nsaved {len(out['deploy'])} survivors -> {dest}")


def cmd_preflight(args) -> None:
    """Everything that should be true BEFORE a real API key goes in.

    Checks configuration, safety locks, credential hygiene and broker
    connectivity without spending a cent, then optionally makes exactly one
    paid call so the LLM path is proven rather than assumed.
    """
    import os
    from pathlib import Path as _P

    from firm.config import load_config
    from firm.llm import LLM, PROVIDERS
    from firm.memory import Memory

    cfg = load_config()
    ok, warn, bad = [], [], []

    def good(m): ok.append(m); print(f"  \033[92mOK\033[0m    {m}")
    def caution(m): warn.append(m); print(f"  \033[93mWARN\033[0m  {m}")
    def fail(m): bad.append(m); print(f"  \033[91mFAIL\033[0m  {m}")

    print("Preflight\n")

    # ---- 1. money cannot move by accident ----
    print("safety locks")
    paper, live_ok = cfg.paper_trading, cfg.allow_live_orders
    if paper and not live_ok:
        good("paper_trading=true, allow_live_orders=false - no real order can leave")
    elif cfg.live_enabled:
        caution("LIVE TRADING IS ARMED - both locks are open, real orders will be sent")
    else:
        good(f"paper_trading={paper}, allow_live_orders={live_ok} - live disabled")

    # ---- 2. credential hygiene ----
    print("\ncredentials")
    envf = _P(".env")
    gitignored = ".env" in _P(".gitignore").read_text() if _P(".gitignore").exists() else False
    if not envf.exists():
        caution(".env does not exist yet - copy .env.example and add your key")
    else:
        good(".env present")
        mode = envf.stat().st_mode & 0o777
        if mode & 0o077:
            caution(f".env is readable by others (mode {mode:o}) - chmod 600 .env")
        else:
            good(".env permissions are private (600)")
    if gitignored:
        good(".env is gitignored")
    else:
        fail(".env is NOT gitignored - your key could be committed")
    if str(cfg.get("llm.api_key") or "").strip():
        caution("llm.api_key is set in config/firm.yaml - prefer .env; "
                "config files get shared and copied")
    else:
        good("no key hard-coded in config/firm.yaml")

    # ---- 3. spend ceiling ----
    print("\nspend")
    cap = float(cfg.get("llm.max_daily_usd", 10.0))
    agents = cfg.get("agents") or {}
    per_agent = sum(float((a or {}).get("budget_usd_per_day", 1.0))
                    for a in agents.values() if (a or {}).get("enabled", True))
    mem = Memory()
    spent = mem.cost_today()
    free_tier = bool(cfg.get("llm.free_tier", False))
    day_tok = int(cfg.get("llm.max_tokens_per_day", 0) or 0)
    mon_tok = int(cfg.get("llm.monthly_token_quota", 0) or 0)

    if free_tier:
        good("free_tier on - tokens are metered, dollar ceilings are bypassed")
        if mon_tok or day_tok:
            good(f"token quota: {mon_tok:,}/month, {day_tok:,}/day (0 = unlimited)")
        else:
            caution("free_tier is on but no token quota is set - nothing caps "
                    "usage if the plan runs out")
        agent_models = {n: str((a or {}).get("model", ""))
                        for n, a in (cfg.get("agents") or {}).items()}
        from firm.router import is_passthrough as _pt
        gateway_routed = [n for n, mo in agent_models.items() if _pt(mo)]
        if gateway_routed:
            good(f"{len(gateway_routed)} agent(s) use the gateway's own routing "
                 f"('auto'/'fusion') - the firm passes those through untouched")
            caution("with gateway routing the model varies per call: watch the "
                    "JSON-compliance line below, since a weak model breaks the "
                    "strict-JSON contract agents rely on")
        all_gateway = gateway_routed and len(gateway_routed) == len(agent_models)
        if all_gateway:
            good("every agent defers to the gateway - client-side pool routing "
                 "is inactive by design")
        elif cfg.get("llm.routing", False):
            from firm import router as _R
            good("multi-pool routing on - agents fail over when a pool empties")
            print("        role -> model (headroom this month):")
            for role in ("execution", "risk", "ceo", "research", "backtest",
                         "scout", "cost_optimizer"):
                mod, why = _R.pick(mem, role)
                if mod:
                    print(f"          {role:15} {why}")
                else:
                    fail(f"{role}: {why}")
            hot = [r for r in _R.status(mem)["models"]
                   if r["metered"] and r["pct_used"] > 80]
            if hot:
                caution("pools past 80%: " + ", ".join(
                    f"{r['model']} {r['pct_used']:.0f}%" for r in hot))
        else:
            caution("llm.routing is off - each agent is pinned to one model and "
                    "stops when that model's pool empties, even with tokens left "
                    "elsewhere. Set llm.routing: true for an aggregator key")
        used_m, used_d = mem.tokens_this_month(), mem.tokens_today()
        print(f"        tokens used: {used_d:,} today / {used_m:,} this month")
        if mon_tok:
            pct = used_m / mon_tok * 100
            (caution if pct > 80 else good)(
                f"{pct:.1f}% of the monthly quota consumed")
    else:
        good(f"firm-wide ceiling ${cap:.2f}/day (enforced in LLM.available)")
        if per_agent > cap:
            good(f"per-agent budgets total ${per_agent:.2f} but the ${cap:.2f} "
                 "ceiling binds first")
        else:
            caution(f"per-agent budgets total ${per_agent:.2f}, under the ceiling - "
                    "the ceiling will never trigger")
        print(f"        spent in the last 24h: ${spent:.4f}")
        model_names = {str((a or {}).get("model", "")) for a in agents.values()}
        from firm.llm import PRICES
        unknown = {m for m in model_names if m and not any(
            m.startswith(k) or k.startswith(m) for k in PRICES)
            and not m.endswith(":free") and not m.startswith("local/")}
        if unknown:
            caution(f"unpriced model(s) {sorted(unknown)} bill at Sonnet rates "
                    "($3/$15 per 1M) in the firm's own accounting. If your "
                    "endpoint is free or flat-rate, set llm.free_tier: true or "
                    "the firm will halt on spend that never happened")
    tick = float(cfg.get("schedule.tick_seconds", 10))
    if tick < 30:
        caution(f"tick_seconds={tick:g} is fast for a paid key; the ceiling protects "
                "you, but consider 60+ while testing")
    else:
        good(f"tick_seconds={tick:g}")

    # ---- 4. LLM wiring, without calling out ----
    print("\nLLM wiring")
    prov = cfg.llm_provider
    proto, default_url, env_var = PROVIDERS.get(prov, PROVIDERS["anthropic"])
    llm = LLM(api_key=cfg.llm_key, provider=prov, base_url=cfg.llm_base_url,
              model_prefix=cfg.llm_model_prefix, max_daily_usd=cap)
    print(f"        provider={prov} [{proto}]  endpoint={llm.base_url or '(unset)'}")
    if not llm.enabled:
        caution(f"no key found in {env_var} or LLM_API_KEY - the firm will run on "
                "its deterministic engine (this is a valid way to run)")
    else:
        k = cfg.llm_key
        good(f"key loaded from {env_var} ({k[:4]}...{k[-2:]}, {len(k)} chars)")
        model = cfg.get("agents.research.model", "claude-sonnet-4-5")
        resolved = llm.resolve_model(model)
        if prov == "openrouter" and "/" not in resolved:
            fail(f"OpenRouter needs a vendor prefix: '{resolved}' will 404. "
                 "Set llm.model_prefix: anthropic")
        else:
            good(f"model resolves to '{resolved}'")

    # ---- 5. brokers ----
    print("\nbrokers")
    try:
        from firm.orchestrator import Firm
        f = Firm(memory=mem, connect=True)
        for bid, br in f.ctx.brokers.items():
            try:
                acct = br.account()
                good(f"{bid}: connected, equity ${acct.equity:,.2f}")
            except Exception as e:
                fail(f"{bid}: {e}")
        if not f.ctx.brokers:
            fail("no brokers configured")
    except Exception as e:
        fail(f"could not build the firm: {e}")

    # ---- 6. supervision ----
    print("\nself-supervision")
    if cfg.get("supervision.auto_quarantine", True):
        good(f"auto-quarantine on, reviewing every "
             f"{float(cfg.get('supervision.interval_seconds', 3600))/60:.0f} min")
    else:
        caution("auto-quarantine is off - broken strategies are reported, not pulled")
    if cfg.get("risk.correlation_guard", True):
        good("correlation guard on")
    else:
        caution("correlation guard is off")

    # ---- 7. the one paid call ----
    if args.live_call and llm.enabled:
        print("\nlive call (this spends money)")
        r = llm.ask("preflight", cfg.get("agents.research.model", "claude-sonnet-4-5"),
                    "You are terse. Reply with STRICT JSON only.",
                    'Reply exactly: {"ok": true}', max_tokens=60)
        if r.error:
            fail(f"call failed: {r.error}")
        else:
            good(f"reply received, {r.input_tokens}+{r.output_tokens} tokens, "
                 f"${r.usd:.5f}")
            good("JSON parsed" if r.json() else "reply was not clean JSON - "
                 "agents will fall back to heuristics")
    elif llm.enabled:
        print("\n        (add --live-call to make one real request and prove the path)")

    print(f"\n{len(ok)} ok, {len(warn)} warnings, {len(bad)} failures")
    if bad:
        print("Fix the failures before running with a real key.")
    elif warn:
        print("Safe to run. Review the warnings above.")
    else:
        print("Ready.")


def cmd_strategies(args) -> None:
    """The roster: who is trading, who has been benched, and why."""
    from firm.config import load_config
    from firm.memory import Memory
    from firm import supervisor as sup

    cfg, mem = load_config(), Memory()

    if args.reinstate:
        ok = sup.reinstate(mem, args.reinstate, who="board")
        print(f"reinstated {args.reinstate}" if ok else
              f"could not reinstate {args.reinstate!r} - not found, or not quarantined")
        return

    if args.review or args.dry_run:
        out = sup.review(mem, cfg, dry_run=args.dry_run, actor="board")
        if not out["candidates"]:
            print("Review complete: no approved strategy has broken down.")
        for c in out["candidates"]:
            print(f"  {c['verdict']:12} {c['name']}")
            print(f"               {c['reason']}")
        if out["dry_run"]:
            print("\n  (dry run - nothing was changed)")
        elif out["quarantined"]:
            print("\n  quarantined: " + ", ".join(out["quarantined"]))
        return

    st = sup.status(mem)
    order = ["approved", "quarantined", "proposed", "rejected"]
    seen = set()
    for group in order + [k for k in st["by_status"] if k not in order]:
        rows = st["by_status"].get(group)
        if not rows or group in seen:
            continue
        seen.add(group)
        print(f"\n{group.upper()}  ({len(rows)})")
        for r in rows:
            score = f"{r['score']:.3f}" if isinstance(r["score"], (int, float)) else "  -  "
            print(f"  {score}  {r['name']}")
            if group == "quarantined" and r["notes"]:
                print(f"           {r['notes'][:100]}")
    if st["quarantined"]:
        print("\nReinstate with:  python cli.py strategies --reinstate 'name@SYMBOL'")


def cmd_drift(args) -> None:
    """Compare live fills against the backtest that approved each strategy."""
    from firm.config import load_config
    from firm.memory import Memory
    from firm.drift import report

    cfg = load_config()
    mem = Memory()
    rep = report(mem, include_demo=not args.live_only)

    print(f"Strategy health  ({rep['checked']} with a baseline, "
          f"min {rep['min_trades']} live trades for a verdict)")
    if rep["include_demo"]:
        print("  NOTE: seeded demo trades are included; pass --live-only to exclude them.")
    print()
    if not rep["rows"]:
        print("  Nothing to compare yet - no strategy has both a backtest and closed fills.")
    for r in rep["rows"]:
        d = r["delta_r"]
        dtxt = f"{d:+.3f}R" if d is not None else "   --"
        pv = f"p={r['p_value']:.3f}" if r["p_value"] is not None else "p=   -"
        print(f"  {r['status']:12} {r['strategy'][:26]:26} {r['symbol']:7} "
              f"live {str(r['live_expectancy_r']):>7} vs {r['baseline_expectancy_r']:>7} "
              f"{dtxt:>9}  {pv}  n={r['trades']}")
        print(f"      {r['reason']}")
    if rep["unmatched"]:
        print(f"\n  {len(rep['unmatched'])} live strategies have NO backtest baseline:")
        for u in rep["unmatched"]:
            print(f"      {u['strategy'][:26]:26} {u['symbol']:7} "
                  f"n={u['trades']:<3} {u['live_expectancy_r']:+.3f}R")
    t = rep["tally"]
    if t.get("BROKEN"):
        print(f"\n  {t['BROKEN']} strategy(ies) look BROKEN. Consider retiring or "
              "re-optimising before they cost more.")


def cmd_dashboard(args) -> None:
    import uvicorn
    from web.server import app
    _p(f"Dashboard: http://0.0.0.0:{args.port}")
    uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="warning")


# ---------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser(
        description="Agentic Trading Firm - a 6-agent autonomous trading team for MT4/MT5",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("setup", help="intake interview").set_defaults(fn=cmd_setup)
    sub.add_parser("status", help="firm snapshot").set_defaults(fn=cmd_status)

    a = sub.add_parser("ask", help="give the CEO a task")
    a.add_argument("text", nargs="+")
    a.add_argument("--wait", type=int, default=0, metavar="SEC",
                   help="work the task inline for N seconds")
    a.set_defaults(fn=cmd_ask)

    i = sub.add_parser("inbox", help="CEO reports")
    i.add_argument("--limit", type=int, default=5)
    i.set_defaults(fn=cmd_inbox)

    sub.add_parser("brokers", help="test MT4/MT5 connectivity").set_defaults(fn=cmd_brokers)

    hi = sub.add_parser("history", help="M1 history: download once, derive every timeframe")
    hi.add_argument("--list", action="store_true", help="show stored history")
    hi.add_argument("--download", nargs="+", metavar="SYMBOL",
                    help="pull M1 from the broker")
    hi.add_argument("--days", type=int, default=3650, help="how far back (default 10y)")
    hi.add_argument("--import-csv", nargs=2, metavar=("SYMBOL", "FILE"),
                    help="import a broker/HistData M1 CSV")
    hi.add_argument("--tz-offset", type=float, default=0.0,
                    help="hours to subtract from CSV timestamps to reach UTC")
    hi.add_argument("--from-ticks", nargs=2, metavar=("SYMBOL", "FILE"),
                    help="build M1 from a tick CSV (then the ticks are redundant)")
    hi.add_argument("--check-ticks", metavar="SYMBOL",
                    help="measure whether tick data would change your results")
    hi.add_argument("--resample", nargs=2, metavar=("SYMBOL", "TIMEFRAME"),
                    help="preview a derived timeframe")
    hi.set_defaults(fn=cmd_history)

    bt = sub.add_parser("bulktest",
                        help="mass-test strategies, ranked for survival not return")
    bt.add_argument("--symbols", nargs="+", help="default: config symbols")
    bt.add_argument("--timeframes", nargs="+", help="default: M15 H1 H4")
    bt.add_argument("--bars", type=int, default=2500)
    bt.add_argument("--max-combos", type=int, default=40)
    bt.add_argument("--fast", action="store_true",
                    help="skip per-strategy optimisation (much quicker)")
    bt.add_argument("--min-per-day", type=float, default=None,
                    help="minimum trades per day (default 0.5)")
    bt.add_argument("--max-dd", type=float, default=None,
                    help="maximum drawdown in R (default 12)")
    bt.add_argument("--top", type=int, default=20)
    bt.add_argument("--save", default="", help="write survivors to a JSON file")
    bt.set_defaults(fn=cmd_bulktest)

    pf = sub.add_parser("preflight", help="check everything before using a real API key")
    pf.add_argument("--live-call", action="store_true",
                    help="make exactly one real LLM request to prove the path works")
    pf.set_defaults(fn=cmd_preflight)

    st = sub.add_parser("strategies", help="roster: approved / quarantined / proposed")
    st.add_argument("--review", action="store_true",
                    help="run a supervision pass now and act on broken strategies")
    st.add_argument("--dry-run", action="store_true",
                    help="show what a review would quarantine, change nothing")
    st.add_argument("--reinstate", default="", metavar="NAME",
                    help="return a quarantined strategy to service")
    st.set_defaults(fn=cmd_strategies)

    dr = sub.add_parser("drift", help="live vs backtest strategy health")
    dr.add_argument("--live-only", action="store_true",
                    help="exclude seeded demo trades")
    dr.set_defaults(fn=cmd_drift)

    lm = sub.add_parser("llm", help="verify the LLM provider + key")
    lm.add_argument("--model", default="", help="override the model to test")
    lm.set_defaults(fn=cmd_llm)

    b = sub.add_parser("backtest", help="one-off backtest")
    b.add_argument("strategy"); b.add_argument("symbol", nargs="?")
    b.add_argument("--timeframe"); b.add_argument("--bars", type=int, default=1200)
    b.add_argument("--walk", action="store_true")
    b.set_defaults(fn=cmd_backtest)

    r = sub.add_parser("run", help="run the firm loop")
    r.add_argument("--cycles", type=int, default=0)
    r.set_defaults(fn=cmd_run)

    sub.add_parser("golive", help="arm live trading").set_defaults(fn=cmd_golive)
    sub.add_parser("paper", help="return to paper mode").set_defaults(fn=cmd_paper)

    h = sub.add_parser("halt", help="engage the kill switch")
    h.add_argument("--hours", type=float, default=12); h.add_argument("--reason", default="")
    h.set_defaults(fn=cmd_halt)
    sub.add_parser("resume", help="clear the kill switch").set_defaults(fn=cmd_resume)

    d = sub.add_parser("dashboard", help="web UI")
    d.add_argument("--port", type=int, default=8000)
    d.set_defaults(fn=cmd_dashboard)

    args = ap.parse_args()
    if not getattr(args, "fn", None):
        ap.print_help(); sys.exit(0)
    args.fn(args)


if __name__ == "__main__":
    main()
