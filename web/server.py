"""FastAPI dashboard + control room + strategy lab for the firm."""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

from firm.analytics import blotter, full_report, monte_carlo
from firm.orchestrator import Firm

ROOT = Path(__file__).resolve().parent
EXPORT_DIR = ROOT.parent / "exports"
app = FastAPI(title="Agentic Trading Firm")

_firm: Firm | None = None
_lock = threading.Lock()
_loop_on = False

# background job registry (lab sweeps, ingestion)
JOBS: dict[str, dict[str, Any]] = {}


def firm() -> Firm:
    global _firm
    with _lock:
        if _firm is None:
            _firm = Firm()
    return _firm


def _background() -> None:
    f = firm()
    ts = float(f.cfg.get("schedule.tick_seconds", 10))
    while _loop_on:
        try:
            with _lock:
                f.tick()
        except Exception as e:
            f.memory.log("error", "firm", f"loop: {e}")
        time.sleep(ts)


def _job(kind: str, label: str) -> str:
    jid = uuid.uuid4().hex[:10]
    JOBS[jid] = {"id": jid, "kind": kind, "label": label, "status": "running",
                 "progress": 0, "total": 0, "current": "", "started": time.time(),
                 "results": [], "log": [], "error": ""}
    return jid


def _jlog(jid: str, msg: str) -> None:
    j = JOBS.get(jid)
    if j:
        j["log"].append({"t": time.time(), "msg": msg})
        j["log"] = j["log"][-80:]


# ---------------------------------------------------------------- models
class Ask(BaseModel):
    text: str


class SweepReq(BaseModel):
    symbols: list[str] = []
    timeframes: list[str] = ["H1"]
    strategies: list[str] = []
    max_combos: int = 40
    bars: int = 2000


class IngestReq(BaseModel):
    url: str = ""
    text: str = ""
    symbol: str = ""
    timeframe: str = ""


class ReinstateReq(BaseModel):
    name: str


class ExportReq(BaseModel):
    result: dict
    name: str = ""
    platform: str = "both"
    kind: str = "ea"          # "ea" | "indicator" | "both"


class ScanReq(BaseModel):
    symbols: list[str] = []
    timeframe: str = ""
    limit: int = 6
    max_combos: int = 20
    bars: int = 1600
    use_llm: bool = True
    tags: list[str] = []


class InstrReq(BaseModel):
    agent: str
    text: str


# ---------------------------------------------------------------- pages
@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (ROOT / "index.html").read_text()


# ---------------------------------------------------------------- core api
@app.get("/api/status")
def api_status():
    f = firm()
    with _lock:
        s = f.status()
    s["autopilot"] = _loop_on
    s["agents"] = [
        {"name": a.name, "title": a.title, "charter": a.charter,
         "enabled": a.enabled, "model": a.model,
         "spent": round(a.spent_today(), 4), "budget": a.daily_budget,
         "open": len(f.memory.open_issues(a.name))}
        for a in f.agents.values()]
    s["events"] = f.memory.events(60)
    s["issues"] = f.memory.q("SELECT * FROM issues ORDER BY id DESC LIMIT 25")
    s["signals"] = f.memory.q("SELECT * FROM signals ORDER BY id DESC LIMIT 15")
    s["trades"] = f.memory.q("SELECT * FROM trades ORDER BY id DESC LIMIT 20")
    return JSONResponse(s)


@app.get("/api/analytics")
def api_analytics():
    f = firm()
    trades = f.memory.all_trades()
    start = 10_000.0
    accs = f.cfg.accounts()
    if accs:
        start = float(accs[0].get("starting_balance", 10_000) or 10_000)
    rep = full_report(trades, start)
    rep["monte_carlo"] = monte_carlo(trades, runs=300, starting_equity=start)
    demo = sum(1 for t in trades if t.get("meta_demo"))
    rep["provenance"] = {
        "demo_trades": demo,
        "real_trades": len(trades) - demo,
        "is_demo": demo > 0,
        "demo_only": demo > 0 and demo == len(trades),
    }
    return JSONResponse(rep)


@app.get("/api/trades")
def api_trades(limit: int = 500, symbol: str = "", strategy: str = "",
               side: str = "", status: str = ""):
    """The trade blotter, with optional filtering."""
    f = firm()
    rows = blotter(f.memory.all_trades(), limit=5000)
    if symbol:
        rows = [r for r in rows if (r["symbol"] or "").upper() == symbol.upper()]
    if strategy:
        rows = [r for r in rows if r["strategy"] == strategy]
    if side:
        rows = [r for r in rows if (r["side"] or "").lower() == side.lower()]
    if status:
        rows = [r for r in rows if r["status"] == status]
    closed = [r for r in rows if r["status"] == "closed"]
    rs = [r["r"] for r in closed if r["r"] is not None]
    wins = [r for r in closed if (r["pnl"] or 0) > 0]
    return JSONResponse({
        "trades": rows[:limit],
        "total": len(rows),
        "shown": min(limit, len(rows)),
        "symbols": sorted({r["symbol"] for r in rows if r["symbol"]}),
        "strategies": sorted({r["strategy"] for r in rows if r["strategy"]}),
        "summary": {
            "closed": len(closed),
            "open": sum(1 for r in rows if r["status"] == "open"),
            "net": round(sum(r["pnl"] or 0 for r in closed), 2),
            "total_r": round(sum(rs), 2) if rs else 0.0,
            "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0.0,
        },
    })


@app.get("/api/portfolio")
def api_portfolio():
    """Correlation matrix, clustering and portfolio heat for open positions."""
    from firm.portfolio import assess
    f = firm()
    try:
        broker = f.ctx.primary_broker()
        positions = broker.positions()
        eq = broker.account().equity
    except Exception as e:
        return JSONResponse({"error": str(e), "matrix": {}, "symbols": [],
                             "heat": {}, "concentration": []})
    risk = f.agents.get("risk")
    rows = []
    for p in positions:
        r_usd = risk._position_risk(p, broker) if risk else 0.0
        rows.append({"symbol": p.symbol, "side": p.side, "risk_usd": round(r_usd, 2),
                     "lots": p.lots, "ticket": p.ticket})
    lim = risk.limits if risk else {}
    # Always show the correlation map for the symbols the firm trades, even
    # when flat - knowing which pairs move together is useful before entering.
    watch = list(dict.fromkeys([r["symbol"] for r in rows] + list(f.cfg.symbols)))
    view = assess(broker, rows, eq, timeframe=f.cfg.timeframe,
                  threshold=float(lim.get("corr_threshold", 0.7)),
                  max_cluster_pct=float(lim.get("max_cluster_pct", 2.0)))
    if not rows and watch:
        from firm.portfolio import correlation_matrix, returns_by_symbol, cluster
        rets = returns_by_symbol(broker, watch, f.cfg.timeframe)
        m = correlation_matrix(rets)
        view["matrix"] = m
        view["symbols"] = sorted(m)
        view["clusters"] = cluster(m, float(lim.get("corr_threshold", 0.7)))
    view["positions"] = rows
    view["guard_enabled"] = bool(lim.get("corr_enabled", True))
    return JSONResponse(view)


@app.get("/api/drift")
def api_drift(include_demo: bool = True):
    """Live-vs-backtest drift per strategy."""
    from firm.drift import report as drift_report
    f = firm()
    return JSONResponse(drift_report(f.memory, include_demo=include_demo))


@app.get("/api/strategies")
def api_strategies():
    """Roster + pending supervision verdicts (read-only; never acts)."""
    from firm import supervisor as sup
    f = firm()
    st = sup.status(f.memory)
    st["pending"] = sup.candidates(
        f.memory,
        include_demo=bool(f.cfg.get("supervision.include_demo", False)),
        min_trades=int(f.cfg.get("supervision.min_trades", 12) or 12))
    st["auto_quarantine"] = bool(f.cfg.get("supervision.auto_quarantine", True))
    return JSONResponse(st)


@app.post("/api/strategies/reinstate")
def api_reinstate(req: ReinstateReq):
    """Board override: return a quarantined strategy to service."""
    from firm import supervisor as sup
    name = req.name
    f = firm()
    ok = sup.reinstate(f.memory, name, who="board")
    return JSONResponse({"ok": ok, "name": name,
                         "error": None if ok else "not found or not quarantined"})


@app.get("/api/events")
def api_events(limit: int = 120):
    """Firm-wide activity log: what the agents have actually been doing."""
    f = firm()
    rows = f.memory.events(limit=limit)
    for r in rows:
        try:
            r["meta"] = json.loads(r.get("meta") or "{}")
        except (json.JSONDecodeError, TypeError):
            r["meta"] = {}
    return JSONResponse({"events": rows, "count": len(rows)})


@app.get("/api/inbox")
def api_inbox():
    return JSONResponse(firm().inbox(12))


@app.post("/api/ask")
def api_ask(a: Ask):
    text = a.text.strip()
    if not text:
        return JSONResponse({"error": "empty"}, status_code=400)
    return {"issue": firm().board_request(text[:70], text)}


@app.post("/api/tick")
def api_tick():
    f = firm()
    with _lock:
        return f.tick()


@app.post("/api/autopilot/{state}")
def api_autopilot(state: str):
    global _loop_on
    want = state == "on"
    if want and not _loop_on:
        _loop_on = True
        threading.Thread(target=_background, daemon=True).start()
    elif not want:
        _loop_on = False
    return {"autopilot": _loop_on}


@app.post("/api/halt")
def api_halt():
    firm().agents["risk"].halt("halted from dashboard", hours=12)
    return {"ok": True}


@app.post("/api/resume")
def api_resume():
    firm().agents["risk"].resume()
    return {"ok": True}


# ---------------------------------------------------------------- lab
@app.get("/api/lab/strategies")
def api_lab_strategies():
    from firm.strategies.library import all_strategies
    return {"strategies": [{"name": k, "description": v["description"],
                            "params": v["default_params"]}
                           for k, v in all_strategies().items()]}


@app.get("/api/lab/jobs")
def api_lab_jobs():
    return {"jobs": sorted(JOBS.values(), key=lambda j: -j["started"])[:12]}


@app.get("/api/lab/job/{jid}")
def api_lab_job(jid: str):
    j = JOBS.get(jid)
    return JSONResponse(j or {"error": "unknown job"}, status_code=200 if j else 404)


@app.post("/api/lab/sweep")
def api_lab_sweep(req: SweepReq):
    f = firm()
    from firm.lab import Lab
    from firm.strategies.library import all_strategies
    symbols = req.symbols or f.cfg.symbols
    strategies = req.strategies or list(all_strategies())
    jid = _job("sweep", f"{len(strategies)}x{len(symbols)}x{len(req.timeframes)} sweep")
    job = JOBS[jid]
    job["total"] = len(strategies) * len(symbols) * len(req.timeframes)

    def run():
        try:
            br = f.ctx.primary_broker()
            lab = Lab(br)
            _jlog(jid, f"Loading history for {', '.join(symbols)}")

            def prog(p):
                job["progress"] = p["done"]
                job["current"] = p["current"]
                r = p["result"]
                job["results"].append(r)
                job["results"].sort(key=lambda x: x.get("score", 0), reverse=True)
                _jlog(jid, f"{r['strategy']} {r['symbol']} {r['timeframe']}: "
                           f"score {r['score']} · {r['metrics'].get('trades', 0)} trades "
                           f"· {'PASS' if r['passed'] else 'reject'}")

            lab.sweep(symbols, req.timeframes, strategies,
                      max_combos=req.max_combos, bars_count=req.bars, progress=prog)
            job["status"] = "done"
            keep = [r for r in job["results"] if r.get("passed")]
            _jlog(jid, f"Sweep complete: {len(keep)} of {len(job['results'])} "
                       f"passed validation")
        except Exception as e:
            job["status"] = "error"
            job["error"] = f"{type(e).__name__}: {e}"
            _jlog(jid, f"ERROR {e}")

    threading.Thread(target=run, daemon=True).start()
    return {"job": jid}


@app.post("/api/lab/ingest")
def api_lab_ingest(req: IngestReq):
    """YouTube URL (or pasted text) -> strategy spec -> optimize -> score."""
    f = firm()
    jid = _job("ingest", req.url or "pasted strategy")
    job = JOBS[jid]
    job["total"] = 4

    def run():
        try:
            from firm.ingest import extract, fetch_transcript
            from firm.lab import Lab
            text, title = req.text.strip(), ""

            job["current"] = "Fetching transcript"
            if req.url and not text:
                _jlog(jid, f"Fetching transcript for {req.url}")
                tr = fetch_transcript(req.url)
                title = tr.get("title", "")
                if not tr.get("ok"):
                    job["status"] = "error"
                    job["error"] = tr.get("error", "no transcript")
                    _jlog(jid, job["error"])
                    return
                text = tr["text"]
                _jlog(jid, f"Got {len(text):,} characters via {tr['source']}")
            if not text:
                job["status"] = "error"
                job["error"] = "no transcript or text supplied"
                return
            job["progress"] = 1

            job["current"] = "Extracting rules"
            spec = extract(text, f.llm, title)
            job["spec"] = spec
            _jlog(jid, f"Extracted via {spec.get('method')}: "
                       f"{len(spec.get('entry', []))} entry rules, "
                       f"{len(spec.get('filters', []))} filters")
            for nnote in (spec.get("notes") or [])[:6]:
                _jlog(jid, f"  · {nnote}")
            job["progress"] = 2

            job["current"] = "Backtesting & optimizing"
            br = f.ctx.primary_broker()
            lab = Lab(br)
            sym = req.symbol or (spec.get("symbols") or f.cfg.symbols)[0]
            tf = req.timeframe or spec.get("timeframe") or f.cfg.timeframe
            _jlog(jid, f"Optimizing on {sym} {tf}")
            res = lab.optimize_spec(spec, sym, tf, max_combos=48, bars_count=2200)
            job["progress"] = 3

            d = res.dict()
            d["spec"] = spec
            d["name"] = spec.get("name", "Ingested strategy")
            d["summary"] = spec.get("summary", "")
            d["composite"] = True
            job["results"] = [d]
            m = res.metrics
            _jlog(jid, f"{res.tested} combos in {res.elapsed}s")
            _jlog(jid, f"Best: {m.get('trades', 0)} trades · "
                       f"PF {m.get('profit_factor', 0)} · "
                       f"expectancy {m.get('expectancy_r', 0)}R")
            _jlog(jid, f"Walk-forward: {res.walk_forward.get('oos_total_r', 0)}R over "
                       f"{res.walk_forward.get('oos_trades', 0)} OOS trades")
            _jlog(jid, f"VERDICT: {'ADOPT' if res.passed else 'IGNORE'} "
                       f"(score {res.score}) — {res.reason}")
            job["progress"] = 4
            job["status"] = "done"
        except Exception as e:
            job["status"] = "error"
            job["error"] = f"{type(e).__name__}: {e}"
            _jlog(jid, f"ERROR {e}")

    threading.Thread(target=run, daemon=True).start()
    return {"job": jid}


@app.post("/api/lab/export")
def api_lab_export(req: ExportReq):
    """Generate MQL4/MQL5 Expert Advisor source for a lab result."""
    from firm.lab import LabResult, render_ea
    d = dict(req.result or {})
    res = LabResult(
        strategy=d.get("strategy", "strategy"), symbol=d.get("symbol", "EURUSD"),
        timeframe=d.get("timeframe", "H1"), params=d.get("params", {}) or {},
        metrics=d.get("metrics", {}) or {}, walk_forward=d.get("walk_forward", {}) or {},
        score=float(d.get("score", 0) or 0), passed=bool(d.get("passed")))
    name = (req.name or f"{res.strategy}_{res.symbol}_{res.timeframe}")
    name = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in name)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = {}
    spec = d.get("spec") if d.get("composite") else None
    if spec:
        # rule pack stays useful on its own (readable, re-importable)
        p = EXPORT_DIR / f"{name}.json"
        p.write_text(json.dumps({"name": name, "spec": spec,
                                 "params": res.params, "metrics": res.metrics,
                                 "walk_forward": res.walk_forward,
                                 "score": res.score}, indent=2))
        out["json"] = p.name

    from firm.mql import can_compile, render_spec_ea, render_spec_indicator
    compiled = bool(spec and can_compile(spec))
    kind = (req.kind or "ea").lower()
    want_ea = kind in ("ea", "both")
    want_ind = kind in ("indicator", "both")

    # An indicator is compiled from the rule spec. A built-in strategy has no
    # spec to compile, so only the EA template is available for it.
    if want_ind and not compiled:
        want_ind = False
        out["indicator_note"] = ("Indicator export needs a rule spec; this "
                                 "built-in strategy only ships as an EA.")

    kw = dict(params=res.params, metrics=res.metrics,
              walk_forward=res.walk_forward, score=res.score,
              symbol=res.symbol, timeframe=res.timeframe)

    for mql, key in ((5, "mq5"), (4, "mq4")):
        if req.platform not in ("both", f"mt{mql}"):
            continue
        if want_ea:
            p = EXPORT_DIR / f"{name}.mq{mql}"
            if compiled:
                p.write_text(render_spec_ea(spec, name, mql, **kw))
            else:
                p.write_text(render_ea(res, name, mql))
            out[key] = p.name
        if want_ind:
            iname = f"{name}_Signals"
            q = EXPORT_DIR / f"{iname}.mq{mql}"
            q.write_text(render_spec_indicator(spec, iname, mql, **kw))
            out[f"ind_mq{mql}"] = q.name
    return {"files": out, "dir": str(EXPORT_DIR), "compiled": compiled,
            "kind": kind}


@app.get("/api/lab/download/{fname}")
def api_lab_download(fname: str):
    p = EXPORT_DIR / Path(fname).name
    if not p.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(str(p), filename=p.name, media_type="text/plain")


@app.get("/api/lab/exports")
def api_lab_exports():
    if not EXPORT_DIR.exists():
        return {"files": []}
    return {"files": sorted(
        [{"name": p.name, "size": p.stat().st_size, "mtime": p.stat().st_mtime}
         for p in EXPORT_DIR.iterdir() if p.is_file()],
        key=lambda x: -x["mtime"])[:40]}


# ---------------------------------------------------------------- auto-scan
@app.get("/api/scout/catalogue")
def api_scout_catalogue():
    """The archetypes the scout can test, before any scanning happens."""
    from firm.scout import CATALOGUE
    return {"strategies": [
        {"name": s["name"], "summary": s["summary"], "tags": s.get("tags", []),
         "popularity": s.get("popularity", 0),
         "rules": len(s.get("entry", [])) + len(s.get("filters", []))}
        for s in sorted(CATALOGUE, key=lambda x: -x.get("popularity", 0))]}


@app.get("/api/scout/history")
def api_scout_history():
    """Everything the scout has ever scanned, newest first."""
    f = firm()
    rows = f.memory.recall(kind="scan", limit=200)
    out = []
    for r in rows:
        # memories.meta is stored as a JSON string, not a dict
        meta = r.get("meta") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta) or {}
            except (ValueError, TypeError):
                meta = {}
        if not isinstance(meta, dict):
            meta = {}
        out.append({"key": r.get("key", ""), "content": r.get("content", "")[:200],
                    "verdict": meta.get("verdict", ""),
                    "score": meta.get("score", 0),
                    "at": r.get("created_at", 0)})
    return {"scans": out}


@app.post("/api/scout/scan")
def api_scout_scan(req: ScanReq):
    """Run the auto-scan suite as a background job."""
    f = firm()
    symbols = req.symbols or f.cfg.symbols[:2]
    tf = req.timeframe or f.cfg.timeframe
    jid = _job("scan", f"auto-scan {req.limit} strategies x {len(symbols)} symbols")
    job = JOBS[jid]
    job["total"] = req.limit * len(symbols)

    def run():
        try:
            from firm.scout import discover, summarise
            scout = f.agents.get("scout")
            _jlog(jid, "Discovering trending strategies")
            specs = discover(f.llm if req.use_llm else None,
                             tags=req.tags or None, limit=req.limit,
                             use_llm=req.use_llm)
            job["total"] = len(specs) * len(symbols)
            for s in specs:
                _jlog(jid, f"  · {s['name']} ({s.get('source', 'catalogue')}, "
                           f"popularity {s.get('popularity', 0)})")

            def prog(p):
                job["progress"] = p["done"]
                job["current"] = p["current"]
                r = p["result"]
                job["results"].append(r)
                job["results"].sort(key=lambda x: x.get("score", 0), reverse=True)
                m = r.get("metrics", {})
                _jlog(jid, f"[{r['verdict']}] {r['name']} {r['symbol']}: "
                           f"score {r.get('score', 0)} · {m.get('trades', 0)} trades "
                           f"· PF {m.get('profit_factor', 0)}")

            from firm.lab import Lab
            from firm.scout import scan as run_scan
            lab = Lab(f.ctx.primary_broker())
            results = run_scan(lab, specs, symbols, tf, max_combos=req.max_combos,
                               bars=req.bars, progress=prog, memory=f.memory)
            s = summarise(results)
            job["summary"] = s
            _jlog(jid, f"Scan complete: {s['adopt']} ADOPT · {s['watch']} WATCH · "
                       f"{s['ignore']} IGNORE · {s['errors']} error")
            if scout:
                scout.remember("cycle", f"scan:{int(time.time())}",
                               json.dumps(s, default=str)[:2000])
            job["status"] = "done"
        except Exception as e:
            job["status"] = "error"
            job["error"] = f"{type(e).__name__}: {e}"
            _jlog(jid, f"ERROR {e}")

    threading.Thread(target=run, daemon=True).start()
    return {"job": jid}


# ---------------------------------------------------------------- instructions
@app.get("/api/instructions")
def api_instructions():
    """Per-agent editable instruction docs."""
    f = firm()
    return {"agents": [
        {"name": a.name, "title": a.title,
         "file": a.instructions_path.name,
         "exists": a.instructions_path.exists(),
         "text": a.instructions()}
        for a in f.agents.values()]}


@app.post("/api/instructions")
def api_instructions_save(req: InstrReq):
    """Board edits an agent's instructions from the dashboard."""
    f = firm()
    agent = f.agents.get(req.agent)
    if not agent:
        return JSONResponse({"error": "unknown agent"}, status_code=404)
    text = (req.text or "").strip()
    if not text:
        return JSONResponse({"error": "empty instructions"}, status_code=400)
    p = agent.instructions_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text + "\n", encoding="utf-8")
    f.memory.log("info", "board", f"instructions updated for {req.agent}")
    return {"ok": True, "agent": req.agent, "chars": len(text)}


@app.post("/api/lab/adopt")
def api_lab_adopt(req: ExportReq):
    """Promote a validated lab result into the firm's approved strategy set."""
    f = firm()
    d = req.result or {}
    name = f"{d.get('strategy')}@{d.get('symbol')}"
    f.memory.upsert_strategy(
        name=name,
        spec={"strategy": d.get("strategy"), "symbol": d.get("symbol"),
              "params": d.get("params", {}),
              "timeframe": d.get("timeframe", "H1")},
        source="lab", status="approved" if d.get("passed") else "proposed",
        score=float(d.get("score", 0) or 0), metrics=d.get("metrics", {}),
        notes=f"Adopted from Strategy Lab. {d.get('reason', '')}")
    f.memory.log("info", "lab", f"adopted {name} from the lab")
    return {"ok": True, "name": name}
