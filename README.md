# Agentic Trading Firm — MT4 + MT5

A zero-human trading team, built as the video describes: **a CEO agent you talk to,
delegating across six departments** — strategy discovery, research, backtesting,
risk, execution and cost optimization. Persistent memory so the firm never repeats
work. Paper trading on by default.

The difference from the video's setup: this is **real, self-contained code** that
connects to **MetaTrader 4 and MetaTrader 5** accounts, rather than an orchestration
tool driving TradingView. No Paperclip, no MCP server, no vendor lock-in. An
LLM key is *optional* — without one the whole firm runs on a deterministic
rule engine at $0 cost.

**Bring your own provider.** `llm.provider` accepts `anthropic`, `openrouter`,
`groq`, `openai`, `together`, `deepseek`, `custom` (any OpenAI-compatible
`/v1/chat/completions` URL, including LM Studio and Ollama) or `none`. Put the
key in `.env` — never in `config/firm.yaml`. Verify it with:

```bash
python cli.py llm            # prints provider, endpoint, masked key, live test
```

```
                    YOU (the board)
                          │  one conversation, one agent
                    ┌─────▼─────┐
                    │    CEO    │  plans, delegates, reports back
                    └─────┬─────┘
   ┌─────────┬──────┬─────┴─────┬────────────┬──────────────┐
┌──▼───┐ ┌───▼────┐ ┌───▼────┐ ┌──▼───┐ ┌─────▼─────┐ ┌──────▼──────┐
│Scout │ │Research│ │Backtest│ │ Risk │ │ Execution │ │    Cost     │
│trend │ │ regime │ │ + walk │ │size, │ │ MT4 / MT5 │ │ optimizer   │
│ scan │ │ ideas  │ │forward │ │vetoes│ │  orders   │ │ LLM budget  │
└──────┘ └────────┘ └────────┘ └──────┘ └─────┬─────┘ └─────────────┘
                                             │
                             ┌───────────────┼───────────────┐
                        ┌────▼────┐    ┌─────▼─────┐   ┌─────▼─────┐
                        │Simulated│    │ MT5 native│   │ArenaBridge│
                        │  paper  │    │  (python) │   │ EA  MT4/5 │
                        └─────────┘    └───────────┘   └───────────┘
```

---

## Quick start

```bash
pip install -r requirements.txt
python cli.py setup                    # 9-question intake interview
python cli.py brokers                  # verify the terminal link
python cli.py ask "Get to work" --wait 120
python cli.py dashboard                # web UI on :8000
```

Out of the box it runs on the **simulated paper engine** — no terminal, no broker,
no network required. Point it at MT4/MT5 when you're ready.

---

## Before you use a real API key

```bash
python cli.py preflight              # checks everything, spends nothing
python cli.py preflight --live-call  # + exactly one real request to prove the path
```

It verifies the safety locks, that `.env` exists and is gitignored and private,
that no key is hard-coded in `config/firm.yaml`, that the firm-wide spend
ceiling actually binds, that your model name resolves for the chosen provider
(the OpenRouter vendor-prefix 404 is a specific check), broker connectivity, and
that self-supervision is on. Failures are things to fix before spending money;
warnings are judgement calls.

Set your key up like this - never paste it into a chat, a config file, or a
command that ends up in shell history:

```bash
cp .env.example .env
chmod 600 .env
${EDITOR:-nano} .env        # paste the key into the file yourself
```

Two spend controls exist and both are enforced:

* **per-agent** `agents.<name>.budget_usd_per_day` - stops one agent running away
* **firm-wide** `llm.max_daily_usd` - a hard ceiling across every agent, checked
  in `LLM.available` before each call. It ships at **$2.00/day**; raise it once
  you have seen real numbers. The per-agent budgets total more than the ceiling
  on purpose, so the ceiling is what actually binds.

**Secrets never reach a log.** Some gateways echo your `Authorization` header
back inside an error body, and those errors are written to the event log and
rendered on the dashboard. Every provider error is passed through `redact()`
first, which masks the configured key and any key-shaped token it has never
seen. This is tested against a server that deliberately echoes the header back.

## Connecting MT4 and MT5

Three back ends, chosen per account in `config/firm.yaml`:

| `kind` | Platform | OS | Needs |
|---|---|---|---|
| `simulated` | — | any | nothing (default) |
| `mt5_native` | MT5 | Windows / Wine | `pip install MetaTrader5` |
| `mt5_bridge` | MT5 | **any** | `ArenaBridge.mq5` on a chart |
| `mt4_bridge` | MT4 | **any** | `ArenaBridge.mq4` on a chart |

MT4 has no Python API and MT5's is Windows-only, so the bridge back end talks to
an Expert Advisor through JSON files in the terminal's sandboxed `Files` folder.
That's what makes **MT4 support real** and lets everything work on macOS/Linux.

### Installing the bridge EA

1. Terminal → **File → Open Data Folder** → `MQL5/Experts` (or `MQL4/Experts`)
2. Copy `mql/ArenaBridge.mq5` (or `.mq4`) there
3. MetaEditor → **Compile (F7)**
4. Tools → Options → Expert Advisors → tick **Allow algorithmic trading**
5. Drag **ArenaBridge** onto any chart (one chart is enough for all symbols)
6. Set `broker.bridge.files_dir` to that Data Folder's `MQL5/Files` (or `MQL4/Files`)
7. `python cli.py brokers` → you should see your login, balance and live ticks

## Portfolio correlation (one bet, not several)

Counting open positions is not measuring exposure. Long EURUSD, long XAUUSD and
short USDCHF can be three tickets expressing a single view - and a single loss.
Before any entry, the risk gate now correlates the candidate against the open
book using Pearson correlation on log returns of the broker's own bars:

```
GET /api/portfolio          # matrix, clusters, heat, concentration
```

* symbols with `|r| >= 0.7` are merged into one **cluster** (union-find, so a
  strong *negative* correlation counts as the same bet, hedged)
* **portfolio heat** is `sqrt(SUM s_i s_j r_i r_j C_ij)` with `s = +1 buy / -1 sell`,
  so a short in a correlated pair genuinely reduces concentration
* **effective bets** = `(naive / adjusted)^2` - two fully correlated longs report
  1.0 bets, two uncorrelated report 2.0, a perfect hedge reports 0.0
* if a cluster's *net* risk exceeds `max_correlated_cluster_pct`, the trade is
  refused:

```
correlation limit: EURUSD+XAUUSD would carry $300.00 net risk
(3.00% of equity, cap 2.0%) - these move together, so it is one bet,
not several
```

Approvals carry the same telemetry (`..., 2.0 effective bets across 2 positions`),
and the Analytics tab draws the matrix as a heatmap. Tune or disable it with
`risk.correlation_guard`, `risk.correlation_threshold` and
`risk.max_correlated_cluster_pct`.

### The gate fails closed

`RiskAgent.vet()` never raises. A dead broker connection, a malformed signal or
a bug inside the correlation check itself all resolve to a **refusal** plus a
logged warning - never an exception that a caller might swallow, and never an
approval by accident. When the gate cannot verify a trade, the answer is no.

## The firm can fire its own strategies

Promotion was always automatic - a strategy that clears `verdict()` and
walk-forward is moved to `approved`, and `execution.scan()` trades everything
approved. Until now nothing could reverse that, so **a strategy that passed
once traded forever**, no matter how badly it did live. The drift report could
see the decay and had no way to act on it.

`firm/supervisor.py` closes the loop. The risk agent runs a review on its tick
(hourly by default). When a strategy is statistically **BROKEN** against its own
backtest, it is moved to `quarantined`:

```bash
python cli.py strategies              # the roster: trading / benched / proposed
python cli.py strategies --dry-run    # what a review would pull, changes nothing
python cli.py strategies --review     # run one now
python cli.py strategies --reinstate 'ema_trend_pullback@XAUUSD'
```

Because `execution.scan()` only ever reads `strategies("approved")`, that one
status change stops new entries immediately - no other code path has to know
quarantine exists.

Four deliberate limits on that power:

* **Demo trades can never fire a strategy.** Auto-action runs on real fills
  only, so the seeded dataset cannot retire anything. The dashboard may still
  *display* demo-inclusive drift.
* **A minimum of 12 real closed trades** before any demotion, on top of the
  Welch test - an unlucky week is not evidence.
* **Open positions are left alone.** Quarantine stops new risk; liquidating an
  existing book belongs to the kill switch, not to a statistics module.
* **Recovery is manual.** A benched strategy places no trades, so it can never
  generate the evidence that would clear it. Only the board (or a fresh passing
  backtest) brings it back.

Tune it under `supervision:` in `config/firm.yaml` - set `auto_quarantine: false`
to have the firm report broken strategies without acting on them.

## Strategy health (live vs backtest)

A strategy that passed walk-forward can still stop working. `python cli.py drift`
compares every strategy's **live** expectancy in R against the **backtest** that
approved it:

```
DRIFT  ema_trend_pullback  XAUUSD  live 0.4306 vs 0.5906  -0.160R  p=0.379  n=10
```

Verdicts, worst first: `BROKEN` (significantly worse, p < 0.05, and the gap is
material) · `DRIFT` (materially worse, not yet significant) · `WATCH` (below
baseline but inside noise) · `OK` · `INSUFFICIENT` (fewer than 8 closed trades —
no verdict is offered rather than a bad one).

Significance uses a one-sided **Welch t-test** (unequal variance, since the live
sample is small and its spread has nothing to do with the backtest's). A
**CUSUM** tracks cumulative shortfall in parallel, which catches slow bleed that
a test on the whole sample would average away. Strategies trading live with *no*
recorded backtest are listed separately — that is its own kind of risk.

### EA or Indicator

Any strategy backed by a rule spec — ingested from a video, auto-scanned, or
swept in the lab — exports two ways:

* **Expert Advisor** (`Name.mq4/.mq5`) → `MQL5/Experts`. Trades automatically.
* **Indicator** (`Name_Signals.mq4/.mq5`) → `MQL5/Indicators`. Draws a buy/sell
  arrow on every bar the strategy would have signalled and shows a live
  rule-by-rule vote panel. **It places no orders** — use it to eyeball the logic
  on a real chart before risking anything.

Both are generated from the same rule emitter, so the arrows you see are the
decisions that were backtested. A built-in (non-spec) strategy has no rules to
translate and will only export as an EA.

### Running MT4 and MT5 side by side

```yaml
broker:
  accounts:
    - id: "mt5-prop"
      kind: "mt5_bridge"
      platform: "MT5"
      files_dir: "/Users/me/.../Terminal/ABC123/MQL5/Files"
      enabled: true
    - id: "mt4-legacy"
      kind: "mt4_bridge"
      platform: "MT4"
      files_dir: "/Users/me/.../Terminal/DEF456/MQL4/Files"
      enabled: true
```

The execution agent routes each symbol to whichever account can trade it, and the
risk agent aggregates equity and drawdown **across both terminals**.

The EA implements 9 commands — `ping, account, symbol, tick, bars, positions,
order, close, modify` — and handles the platform quirks for you: MT4's stop-level
clamping and open-then-modify fallback, MT5's IOC/FOK filling-mode retry, lot-step
rounding and magic-number filtering on both.

---

## Talking to the firm

You only ever talk to the CEO, exactly as in the video:

```bash
python cli.py ask "Find something that works on gold, prove it, then trade it"
python cli.py inbox        # the CEO's reports back to you
python cli.py status       # equity, positions, strategies, kill switch
python cli.py run          # autonomous loop
```

The CEO builds a plan and delegates **in dependency order, one department at a
time** — research → backtest → execution. Nothing reaches the market that the
backtest agent hasn't approved and the risk agent hasn't sized.

---

## Safety (the part the video hand-waves)

**Two independent switches** must both flip before a real order can leave the
machine. Default config fails closed:

```yaml
trading:
  paper_trading: true        # switch 1
  allow_live_orders: false   # switch 2
```

`python cli.py golive` requires typing `I ACCEPT THE RISK` *and* your firm's name.
`python cli.py paper` reverts instantly. A simulated account is forced to paper
even when live is armed.

**The risk agent is deterministic code, never an LLM decision.** An agent cannot
talk its way past it:

- position sizing from real contract specs, rounded **down**, capped by `max_lots`
- per-trade risk %, daily-loss and max-drawdown **kill switches** that flatten everything
- stop loss mandatory; rejects stops that are inverted, inside the broker's stop
  level, or tighter than 1×ATR
- max open positions, one position per symbol, no hedging against yourself
- trading-hours window and a Friday weekend-gap guard
- rejects any trade whose risk exceeds what's left of the daily budget

**Backtests must survive walk-forward validation** (optimise in-sample, verify
out-of-sample) before a strategy is ever marked `approved`. Spread is charged on
every fill and stops win ties against targets.

**Cost control** — the video's author burned $40 in one minute by fanning out
every agent at once. Here departments run sequentially, each agent has its own
daily USD budget, the cost optimizer throttles schedules at 75% of cap, and
everything falls back to the free rule engine when the cap is hit.

---

## What's in the box

```
cli.py                      setup · status · ask · inbox · run · brokers · llm · drift
                            backtest · golive · paper · halt · resume · dashboard
firm/
  orchestrator.py           the clock; wires brokers + memory + agents
  memory.py                 SQLite: issues, strategies, trades, costs, memories
  agents/                   ceo · scout · research · backtest · risk ·
                            execution · cost_optimizer
  agents/instructions/      per-agent editable .md docs (edit behaviour,
                            no code changes — also editable in the dashboard)
  scout.py                  auto-scan suite: trending-strategy catalogue,
                            discovery, ranked ADOPT / WATCH / IGNORE verdicts
  mql.py                    compiles a rule spec into a real MQL4/MQL5 EA;
                            every discovered strategy becomes tradeable code
  lab.py                    mass backtesting, optimization, MQL EA + indicator export
  drift.py                  live-vs-backtest drift detection (Welch t-test + CUSUM)
  supervisor.py             auto-quarantine: pulls strategies that have decayed
  portfolio.py              correlation clustering + portfolio heat; stops the
                            firm taking the same bet three times
  ingest.py                 YouTube URL / transcript -> testable rule spec
  analytics.py              equity, drawdown, bootstrap Monte Carlo, 24 metrics
  strategies/composite.py   declarative rule engine (12 rule types)
  brokers/                  simulated · mt5_native · mt_bridge (MT4+MT5)
  strategies/library.py     4 strategies: EMA pullback, Donchian breakout,
                            Bollinger reversion, MACD momentum
  backtester.py             event-driven engine, grid search, walk-forward
  indicators.py             SMA/EMA/RSI/ATR/MACD/Bollinger/Donchian, no numpy
mql/ArenaBridge.mq4/.mq5    the Expert Advisors
web/                        live dashboard
tests/test_firm.py          642 checks, all passing
tests/mql_equivalence.py    proves generated MQL fires on the same bars,
                            in the same direction, as the Python backtest
```

## Tests

```bash
python tests/test_firm.py            # 642 passed, 0 failed
PYTHONPATH=. python tests/mql_equivalence.py   # 0 mismatches
```

Covers broker mechanics, indicator correctness, signal geometry, backtest metric
coherence, every risk veto, the full signal→risk→order→reconcile path, both live
switches, a simulated Expert Advisor exercising the MT4 bridge protocol end to
end, the vectorized fast paths, analytics, video ingestion, the strategy lab,
the auto-scan suite (including malformed-spec isolation), the per-agent
instruction docs, the spec->MQL compiler (all 12 rule types, both dialects,
dialect purity, handle hygiene) and every web endpoint.

**Strategy → EA fidelity.** `tests/mql_equivalence.py` re-implements the emitted
MQL expressions independently — without touching the Python rule engine — and
replays both over the same 1,500 bars. Latest run: **13,400 bar decisions,
1,836 signals, 0 mismatches.** The EA you drop on a chart trades what was
actually backtested.

---

## Adding your own strategy

```python
from firm.strategies.library import register, Signal

@register("my_edge", "what it does", {"atr_mult": 2.0, "rr": 2.0})
def my_edge(bars, p):
    ...
    return Signal("buy", entry, stop, take, confidence=0.6,
                  rationale="why", meta={"atr": atr_value})
```

It's picked up automatically — research can propose it, backtest will validate it,
and only then can execution trade it.

---

## Disclaimer

Educational software, not financial advice. Backtested results are not indicative
of future performance. Leveraged FX and CFD trading can lose you more than you
deposit. Run it on a **demo account** for a long time before you even think about
real money, and never risk capital you can't afford to lose. You are responsible
for every order this system sends.
