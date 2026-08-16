"""Compile a declarative composite spec into a real MQL4/MQL5 Expert Advisor.

Until now, ingested and auto-scanned strategies exported as a JSON rule pack
plus a generic EA shell: readable, but not tradeable. This module closes that
gap. It walks the same spec that `composite.evaluate_spec` executes in Python
and emits equivalent MQL, so the EA you drop on a chart implements the strategy
that was actually backtested.

Fidelity is the whole point, so the generated code mirrors the Python engine
exactly:

  * every rule votes +1 / -1 / 0, filters can hard-block the bar (the -99 veto)
  * a majority vote against `agreement` decides direction
  * the ATR stop and R-multiple target are computed identically
  * everything reads the LAST CLOSED BAR (shift 1), never the forming bar

Two honest caveats are baked into the generated header rather than hidden:

  1. `session` filters use BROKER SERVER TIME in MQL but UTC in the backtest.
     If your broker is not UTC, shift the hours.
  2. The backtest models a half-spread entry and resolves stop-vs-target ties
     against you. A real terminal will not match tick for tick.
"""
from __future__ import annotations

import json
import time
from typing import Any

from .strategies.composite import RULE_TYPES

# ---------------------------------------------------------------- helpers
_DEF = {"fast": 9, "mid": 21, "slow": 50, "period": 14, "to_period": 21}


def _i(rule: dict, key: str, default: Any) -> int:
    try:
        return int(rule.get(key, default))
    except (TypeError, ValueError):
        return int(default)


def _f(rule: dict, key: str, default: Any) -> float:
    try:
        return float(rule.get(key, default))
    except (TypeError, ValueError):
        return float(default)


class _Ind:
    """Collects the indicators a spec needs so MQL5 can pre-create handles."""

    def __init__(self) -> None:
        self.ema: set[int] = set()
        self.sma: set[int] = set()
        self.rsi: set[int] = set()
        self.macd: set[tuple[int, int, int]] = set()
        self.bands: set[tuple[int, float]] = set()

    def ema_h(self, n: int) -> str:
        self.ema.add(n)
        return f"hEma{n}"

    def sma_h(self, n: int) -> str:
        self.sma.add(n)
        return f"hSma{n}"

    def rsi_h(self, n: int) -> str:
        self.rsi.add(n)
        return f"hRsi{n}"

    def macd_h(self, f: int, s: int, g: int) -> str:
        self.macd.add((f, s, g))
        return f"hMacd_{f}_{s}_{g}"

    def bands_h(self, n: int, m: float) -> str:
        self.bands.add((n, m))
        return f"hBb_{n}_{str(m).replace('.', 'p')}"


def _ema(ind: _Ind, n: int, shift: Any, v: int) -> str:
    if v == 4:
        return f"iMA(NULL,0,{n},0,MODE_EMA,PRICE_CLOSE,{shift})"
    return f"Buf({ind.ema_h(n)},0,{shift})"


def _sma(ind: _Ind, n: int, shift: Any, v: int) -> str:
    if v == 4:
        return f"iMA(NULL,0,{n},0,MODE_SMA,PRICE_CLOSE,{shift})"
    return f"Buf({ind.sma_h(n)},0,{shift})"


def _rsi(ind: _Ind, n: int, shift: Any, v: int) -> str:
    if v == 4:
        return f"iRSI(NULL,0,{n},PRICE_CLOSE,{shift})"
    return f"Buf({ind.rsi_h(n)},0,{shift})"


def _macd(ind: _Ind, f: int, s: int, g: int, buf: int, shift: Any, v: int) -> str:
    if v == 4:
        mode = "MODE_MAIN" if buf == 0 else "MODE_SIGNAL"
        return f"iMACD(NULL,0,{f},{s},{g},PRICE_CLOSE,{mode},{shift})"
    return f"Buf({ind.macd_h(f, s, g)},{buf},{shift})"


def _band(ind: _Ind, n: int, m: float, which: str, shift: Any, v: int) -> str:
    if v == 4:
        mode = {"upper": "MODE_UPPER", "lower": "MODE_LOWER"}[which]
        return f"iBands(NULL,0,{n},{m},0,PRICE_CLOSE,{mode},{shift})"
    buf = {"upper": 1, "lower": 2}[which]
    return f"Buf({ind.bands_h(n, m)},{buf},{shift})"


# ---------------------------------------------------------------- rule → MQL
def _rule_code(rule: dict, ind: _Ind, v: int, var: str,
               s1: Any = 1, s2: Any = 2) -> str:
    """Emit MQL that assigns a vote (+1/-1/0, or -99 veto) to `var`.

    Mirrors `composite._rule_vote` case for case.

    `s1`/`s2` are the bar shifts for "last closed bar" and "the one before".
    An EA hardcodes 1 and 2; the indicator passes expressions like "sh" and
    "sh+1" so the same rules can be replayed over history.
    """
    t = rule.get("type")
    c1 = f"Cl({s1})"

    if t == "ema_stack":
        f, m, s = (_i(rule, "fast", 9), _i(rule, "mid", 21), _i(rule, "slow", 50))
        return f"""
   {{
    double f={_ema(ind, f, s1, v)}, m={_ema(ind, m, s1, v)}, s={_ema(ind, s, s1, v)};
    if(f<=0||m<=0||s<=0) {var}=0;
    else if(f>m && m>s)  {var}=1;
    else if(f<m && m<s)  {var}=-1;
    else                 {var}=0;
   }}"""

    if t in ("ema_cross", "sma_cross"):
        fn, sn = _i(rule, "fast", 9), _i(rule, "slow", 21)
        g = _ema if t == "ema_cross" else _sma
        return f"""
   {{
    double f0={g(ind, fn, s1, v)}, s0={g(ind, sn, s1, v)};
    double f1={g(ind, fn, s2, v)}, s1={g(ind, sn, s2, v)};
    if(f0<=0||s0<=0||f1<=0||s1<=0)   {var}=0;
    else if(f1<=s1 && f0>s0)         {var}=1;
    else if(f1>=s1 && f0<s0)         {var}=-1;
    else                             {var}=0;
   }}"""

    if t == "rsi_zone":
        n = _i(rule, "period", 14)
        lo, hi = _f(rule, "min", 40), _f(rule, "max", 70)
        return f"""
   {{
    double r={_rsi(ind, n, s1, v)};
    if(r<=0)                    {var}=0;
    else if(r>={lo} && r<={hi}) {var}=(r>=50?1:-1);
    else                        {var}=0;
   }}"""

    if t == "rsi_extreme":
        n = _i(rule, "period", 14)
        os_, ob = _f(rule, "oversold", 30), _f(rule, "overbought", 70)
        return f"""
   {{
    double r={_rsi(ind, n, s1, v)};
    if(r<=0)        {var}=0;
    else if(r<{os_}) {var}=1;
    else if(r>{ob})  {var}=-1;
    else            {var}=0;
   }}"""

    if t == "macd_cross":
        f, s, g = (_i(rule, "fast", 12), _i(rule, "slow", 26), _i(rule, "signal", 9))
        return f"""
   {{
    double m0={_macd(ind, f, s, g, 0, s1, v)}, g0={_macd(ind, f, s, g, 1, s1, v)};
    double m1={_macd(ind, f, s, g, 0, s2, v)}, g1={_macd(ind, f, s, g, 1, s2, v)};
    if(m1<=g1 && m0>g0)      {var}=1;
    else if(m1>=g1 && m0<g0) {var}=-1;
    else                     {var}=0;
   }}"""

    if t == "bb_touch":
        n, m = _i(rule, "period", 20), _f(rule, "mult", 2.0)
        rev = rule.get("mode", "reversion") == "reversion"
        below, above = (1, -1) if rev else (-1, 1)
        return f"""
   {{
    double up={_band(ind, n, m, 'upper', s1, v)}, lo={_band(ind, n, m, 'lower', s1, v)};
    double px={c1};
    if(up<=0||lo<=0)   {var}=0;
    else if(px<lo)     {var}={below};
    else if(px>up)     {var}={above};
    else               {var}=0;
   }}"""

    if t == "breakout":
        n = _i(rule, "period", rule.get("channel", 20))
        return f"""
   {{
    double hh=HH({n},{s2}), ll=LL({n},{s2}), px={c1};
    if(hh<=0||ll<=0) {var}=0;
    else if(px>hh)   {var}=1;
    else if(px<ll)   {var}=-1;
    else             {var}=0;
   }}"""

    if t == "pullback":
        ref = rule.get("to", "ema_mid")
        n = {"ema_fast": _i(rule, "fast", 9),
             "ema_mid": _i(rule, "mid", 21),
             "ema_slow": _i(rule, "slow", 50)}.get(ref, _i(rule, "to_period", 21))
        mx = _f(rule, "max_atr", 1.0)
        # NOTE: mirrors Python - this rule votes 1 or 0, never -1.
        return f"""
   {{
    double ma={_ema(ind, n, s1, v)}, px={c1};
    if(ma<=0||atr<=0) {var}=0;
    else              {var}=(MathAbs(px-ma)/atr<={mx})?1:0;
   }}"""

    if t == "candle":
        return f"""
   {{
    double o1=Op({s1}),h1=Hi({s1}),l1=Lo({s1}),c1=Cl({s1});
    double o2=Op({s2}),c2=Cl({s2});
    double body=MathAbs(c1-o1), rng=MathMax(h1-l1,1e-12), pbody=MathAbs(c2-o2);
    bool bullEng = c1>o1 && c2<o2 && body>pbody;
    bool bearEng = c1<o1 && c2>o2 && body>pbody;
    double lw = MathMin(o1,c1)-l1, uw = h1-MathMax(o1,c1);
    bool pinBull = lw>body*2 && body/rng<0.4;
    bool pinBear = uw>body*2 && body/rng<0.4;
    if(bullEng||pinBull)      {var}=1;
    else if(bearEng||pinBear) {var}=-1;
    else                      {var}=0;
   }}"""

    if t == "session":
        f_, t_ = _i(rule, "from", 0), _i(rule, "to", 24)
        return f"""
   {{
    int hr=BarHour({s1});
    {var} = (hr>={f_} && hr<{t_}) ? 0 : -99;   // -99 = hard veto
   }}"""

    if t == "atr_filter":
        lo, hi = _f(rule, "min_pct", 0.0), _f(rule, "max_pct", 99.0)
        return f"""
   {{
    double px={c1};
    double pct = (px>0 && atr>0) ? atr/px*100.0 : -1.0;
    if(pct<0) {var}=0;
    else      {var} = (pct>={lo} && pct<={hi}) ? 0 : -99;
   }}"""

    return f"\n   {var}=0;   // unknown rule type, ignored"


def can_compile(spec: dict) -> bool:
    """True when every rule in the spec has an MQL implementation."""
    if not isinstance(spec, dict):
        return False
    rules = list(spec.get("entry") or []) + list(spec.get("filters") or [])
    if not rules:
        return False
    return all(isinstance(r, dict) and r.get("type") in RULE_TYPES for r in rules)


# ---------------------------------------------------------------- renderer
def render_spec_ea(spec: dict, name: str, mql: int = 5, params: dict | None = None,
                   metrics: dict | None = None, walk_forward: dict | None = None,
                   score: float = 0.0, symbol: str = "", timeframe: str = "") -> str:
    """Compile a composite spec into a complete, compilable Expert Advisor."""
    params = params or {}
    metrics = metrics or {}
    ex = dict(spec.get("exit") or {})
    atr_mult = float(params.get("atr_mult", ex.get("atr_mult", 2.0)))
    rr = float(params.get("rr", ex.get("rr", 2.0)))
    agreement = float(params.get("agreement", spec.get("agreement", 0.6)))
    atr_n = int((spec.get("params") or {}).get("atr_n", 14))

    entry = [r for r in (spec.get("entry") or []) if isinstance(r, dict)]
    filters = [r for r in (spec.get("filters") or []) if isinstance(r, dict)]

    ind = _Ind()
    ebody, fbody = [], []
    for k, r in enumerate(entry):
        ebody.append(f"   // entry rule {k + 1}: {r.get('type')}"
                     + _rule_code(r, ind, mql, f"ev[{k}]"))
    for k, r in enumerate(filters):
        fbody.append(f"   // filter {k + 1}: {r.get('type')}"
                     + _rule_code(r, ind, mql, f"fv[{k}]"))

    m = metrics
    rules_json = json.dumps({"entry": entry, "filters": filters,
                             "exit": {"atr_mult": atr_mult, "rr": rr},
                             "agreement": agreement})
    header = f"""//+------------------------------------------------------------------+
//| {name}.mq{mql}
//| Compiled from a strategy spec by the Agentic Trading Firm
//| {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}
//|
//| Strategy   : {spec.get('name', name)}
//| Source     : {spec.get('source', spec.get('method', 'spec'))}
//| Symbol/TF  : {symbol or 'any'} {timeframe or 'chart'}
//| Entry rules: {len(entry)}   Filters: {len(filters)}   Agreement: {agreement}
//|
//| {str(spec.get('summary', ''))[:110]}
//|
//| BACKTEST (in-sample, spread-adjusted)
//|   trades {m.get('trades', 0)} | win {m.get('win_rate', 0)}% | PF {m.get('profit_factor', 0)}
//|   expectancy {m.get('expectancy_r', 0)}R | maxDD {m.get('max_drawdown_r', 0)}R
//|   sharpe {m.get('sharpe', 0)} | robustness score {score}
//| WALK-FORWARD: {json.dumps(walk_forward or {})[:150]}
//|
//| RULES: {rules_json[:600]}
//|
//| IMPORTANT
//|  * Rules are evaluated on the LAST CLOSED BAR, never the forming bar.
//|  * Any `session` filter below uses BROKER SERVER TIME here, but UTC in the
//|    backtest. If your broker is not UTC, shift the hours accordingly.
//|  * The backtest assumed a half-spread entry and resolved stop-vs-target
//|    ties against the trade. Live results will differ.
//|  * Past performance does not predict future results. Demo test first.
//+------------------------------------------------------------------+
#property copyright "Agentic Trading Firm"
#property version   "1.10"
#property strict

input double RiskPercent   = 0.75;    // % equity risked per trade
input double ATRMultiplier = {atr_mult};   // stop distance in ATR
input double RewardRatio   = {rr};   // target = R x stop
input double Agreement     = {agreement};  // fraction of rules that must agree
input int    ATRPeriod     = {atr_n};
input int    MagicNumber   = {abs(hash(name)) % 900000 + 100000};
input int    Slippage      = 20;
input int    MaxPositions  = 1;
input bool   TradeOnNewBarOnly = true;

#define N_ENTRY {max(len(entry), 1)}
#define N_FILTER {max(len(filters), 1)}
"""

    # ---- shared decision logic, identical in both dialects ----
    decide = f"""
//+------------------------------------------------------------------+
//| Majority vote - mirrors composite.evaluate_spec exactly           |
//+------------------------------------------------------------------+
int Decide(double atr)
  {{
   int fv[N_FILTER]; int ev[N_ENTRY];
   for(int i=0;i<N_FILTER;i++) fv[i]=0;
   for(int i=0;i<N_ENTRY;i++)  ev[i]=0;

{chr(10).join(fbody) if fbody else '   // no filters'}

   // any filter may hard-veto the bar
   for(int i=0;i<{len(filters) if filters else 0};i++)
      if(fv[i]==-99) return 0;

{chr(10).join(ebody) if ebody else '   // no entry rules'}

   int bulls=0, bears=0, total={len(entry)};
   for(int i=0;i<{len(entry)};i++)
     {{
      if(ev[i]==-99) return 0;      // veto inside an entry rule
      if(ev[i]>0) bulls++;
      if(ev[i]<0) bears++;
     }}
   if(total<=0) return 0;

   double bu=(double)bulls/(double)total, be=(double)bears/(double)total;
   if(bu>=Agreement && bulls>bears) return 1;
   if(be>=Agreement && bears>bulls) return -1;
   return 0;
  }}
"""

    if mql == 5:
        handles = []
        creates = []
        for n in sorted(ind.ema):
            handles.append(f"int hEma{n};")
            creates.append(f"   hEma{n}=iMA(_Symbol,PERIOD_CURRENT,{n},0,MODE_EMA,PRICE_CLOSE);")
        for n in sorted(ind.sma):
            handles.append(f"int hSma{n};")
            creates.append(f"   hSma{n}=iMA(_Symbol,PERIOD_CURRENT,{n},0,MODE_SMA,PRICE_CLOSE);")
        for n in sorted(ind.rsi):
            handles.append(f"int hRsi{n};")
            creates.append(f"   hRsi{n}=iRSI(_Symbol,PERIOD_CURRENT,{n},PRICE_CLOSE);")
        for f, s, g in sorted(ind.macd):
            handles.append(f"int hMacd_{f}_{s}_{g};")
            creates.append(f"   hMacd_{f}_{s}_{g}=iMACD(_Symbol,PERIOD_CURRENT,"
                           f"{f},{s},{g},PRICE_CLOSE);")
        for n, mm in sorted(ind.bands):
            hn = f"hBb_{n}_{str(mm).replace('.', 'p')}"
            handles.append(f"int {hn};")
            creates.append(f"   {hn}=iBands(_Symbol,PERIOD_CURRENT,{n},0,{mm},PRICE_CLOSE);")

        return header + f"""
#include <Trade/Trade.mqh>
CTrade trade;
int hAtr;
{chr(10).join(handles)}
datetime lastBar = 0;

double Buf(int h,int b,int s)
  {{
   double v[];
   if(h==INVALID_HANDLE) return 0.0;
   if(CopyBuffer(h,b,s,1,v)<1) return 0.0;
   return v[0];
  }}
double Op(int s){{ return iOpen(_Symbol,PERIOD_CURRENT,s); }}
double Hi(int s){{ return iHigh(_Symbol,PERIOD_CURRENT,s); }}
double Lo(int s){{ return iLow(_Symbol,PERIOD_CURRENT,s); }}
double Cl(int s){{ return iClose(_Symbol,PERIOD_CURRENT,s); }}
int BarHour(int s)
  {{
   MqlDateTime dt;
   TimeToStruct(iTime(_Symbol,PERIOD_CURRENT,s),dt);
   return dt.hour;
  }}
double HH(int n,int shift)
  {{
   double h[];
   if(CopyHigh(_Symbol,PERIOD_CURRENT,shift,n,h)<n) return 0.0;
   return h[ArrayMaximum(h)];
  }}
double LL(int n,int shift)
  {{
   double l[];
   if(CopyLow(_Symbol,PERIOD_CURRENT,shift,n,l)<n) return 0.0;
   return l[ArrayMinimum(l)];
  }}

int OnInit()
  {{
   trade.SetExpertMagicNumber(MagicNumber);
   trade.SetDeviationInPoints(Slippage);
   hAtr=iATR(_Symbol,PERIOD_CURRENT,ATRPeriod);
{chr(10).join(creates)}
   if(hAtr==INVALID_HANDLE) return INIT_FAILED;
   Print("{name} ready. Risk ",RiskPercent,"% | ATRx",ATRMultiplier,
         " | RR ",RewardRatio," | agreement ",Agreement);
   return INIT_SUCCEEDED;
  }}
{decide}
int CountPositions()
  {{
   int c=0;
   for(int i=PositionsTotal()-1;i>=0;i--)
     {{
      ulong tk=PositionGetTicket(i);
      if(tk==0) continue;
      if(PositionSelectByTicket(tk) && PositionGetString(POSITION_SYMBOL)==_Symbol
         && PositionGetInteger(POSITION_MAGIC)==MagicNumber) c++;
     }}
   return c;
  }}

double LotsForRisk(double stopDistance)
  {{
   double eq=AccountInfoDouble(ACCOUNT_EQUITY);
   double risk=eq*RiskPercent/100.0;
   double tv=SymbolInfoDouble(_Symbol,SYMBOL_TRADE_TICK_VALUE);
   double ts=SymbolInfoDouble(_Symbol,SYMBOL_TRADE_TICK_SIZE);
   if(ts<=0||tv<=0||stopDistance<=0) return 0.0;
   double lossPerLot=stopDistance/ts*tv;
   if(lossPerLot<=0) return 0.0;
   double lots=risk/lossPerLot;
   double step=SymbolInfoDouble(_Symbol,SYMBOL_VOLUME_STEP);
   double vmin=SymbolInfoDouble(_Symbol,SYMBOL_VOLUME_MIN);
   double vmax=SymbolInfoDouble(_Symbol,SYMBOL_VOLUME_MAX);
   if(step>0) lots=MathFloor(lots/step)*step;
   if(lots<vmin) return 0.0;
   return MathMin(lots,vmax);
  }}

void OnTick()
  {{
   if(TradeOnNewBarOnly)
     {{
      datetime t=iTime(_Symbol,PERIOD_CURRENT,0);
      if(t==lastBar) return;
      lastBar=t;
     }}
   if(CountPositions()>=MaxPositions) return;

   double atr=Buf(hAtr,0,1);
   if(atr<=0) return;

   int dir=Decide(atr);
   if(dir==0) return;

   double stopDist=atr*ATRMultiplier;
   double lots=LotsForRisk(stopDist);
   if(lots<=0) return;

   int digits=(int)SymbolInfoInteger(_Symbol,SYMBOL_DIGITS);
   double ask=SymbolInfoDouble(_Symbol,SYMBOL_ASK);
   double bid=SymbolInfoDouble(_Symbol,SYMBOL_BID);

   if(dir>0)
     {{
      double sl=NormalizeDouble(ask-stopDist,digits);
      double tp=NormalizeDouble(ask+stopDist*RewardRatio,digits);
      trade.Buy(lots,_Symbol,ask,sl,tp,"{name}");
     }}
   else
     {{
      double sl=NormalizeDouble(bid+stopDist,digits);
      double tp=NormalizeDouble(bid-stopDist*RewardRatio,digits);
      trade.Sell(lots,_Symbol,bid,sl,tp,"{name}");
     }}
  }}
"""

    # ---------------- MQL4 ----------------
    return header + f"""
datetime lastBar = 0;

double Op(int s){{ return iOpen(NULL,0,s); }}
double Hi(int s){{ return iHigh(NULL,0,s); }}
double Lo(int s){{ return iLow(NULL,0,s); }}
double Cl(int s){{ return iClose(NULL,0,s); }}
int BarHour(int s){{ return TimeHour(iTime(NULL,0,s)); }}
double HH(int n,int shift){{ return High[iHighest(NULL,0,MODE_HIGH,n,shift)]; }}
double LL(int n,int shift){{ return Low[iLowest(NULL,0,MODE_LOW,n,shift)]; }}

int OnInit()
  {{
   Print("{name} ready. Risk ",RiskPercent,"% | ATRx",ATRMultiplier,
         " | RR ",RewardRatio," | agreement ",Agreement);
   return INIT_SUCCEEDED;
  }}
{decide}
int CountPositions()
  {{
   int c=0;
   for(int i=OrdersTotal()-1;i>=0;i--)
     {{
      if(!OrderSelect(i,SELECT_BY_POS,MODE_TRADES)) continue;
      if(OrderSymbol()==Symbol() && OrderMagicNumber()==MagicNumber
         && (OrderType()==OP_BUY||OrderType()==OP_SELL)) c++;
     }}
   return c;
  }}

double LotsForRisk(double stopDistance)
  {{
   double eq=AccountEquity();
   double risk=eq*RiskPercent/100.0;
   double tv=MarketInfo(Symbol(),MODE_TICKVALUE);
   double ts=MarketInfo(Symbol(),MODE_TICKSIZE);
   if(ts<=0||tv<=0||stopDistance<=0) return 0.0;
   double lossPerLot=stopDistance/ts*tv;
   if(lossPerLot<=0) return 0.0;
   double lots=risk/lossPerLot;
   double step=MarketInfo(Symbol(),MODE_LOTSTEP);
   double vmin=MarketInfo(Symbol(),MODE_MINLOT);
   double vmax=MarketInfo(Symbol(),MODE_MAXLOT);
   if(step>0) lots=MathFloor(lots/step)*step;
   if(lots<vmin) return 0.0;
   return MathMin(lots,vmax);
  }}

void OnTick()
  {{
   if(TradeOnNewBarOnly)
     {{
      if(Time[0]==lastBar) return;
      lastBar=Time[0];
     }}
   if(CountPositions()>=MaxPositions) return;

   double atr=iATR(NULL,0,ATRPeriod,1);
   if(atr<=0) return;

   int dir=Decide(atr);
   if(dir==0) return;

   double stopDist=atr*ATRMultiplier;
   double lots=LotsForRisk(stopDist);
   if(lots<=0) return;

   int digits=(int)MarketInfo(Symbol(),MODE_DIGITS);
   double stopLvl=MarketInfo(Symbol(),MODE_STOPLEVEL)*Point;
   if(stopDist<stopLvl) stopDist=stopLvl;

   if(dir>0)
     {{
      double sl=NormalizeDouble(Ask-stopDist,digits);
      double tp=NormalizeDouble(Ask+stopDist*RewardRatio,digits);
      int t1=OrderSend(Symbol(),OP_BUY,lots,NormalizeDouble(Ask,digits),Slippage,
                       sl,tp,"{name}",MagicNumber,0,clrDodgerBlue);
      if(t1<0) Print("Buy failed err=",GetLastError());
     }}
   else
     {{
      double sl=NormalizeDouble(Bid+stopDist,digits);
      double tp=NormalizeDouble(Bid-stopDist*RewardRatio,digits);
      int t2=OrderSend(Symbol(),OP_SELL,lots,NormalizeDouble(Bid,digits),Slippage,
                       sl,tp,"{name}",MagicNumber,0,clrOrangeRed);
      if(t2<0) Print("Sell failed err=",GetLastError());
     }}
  }}
"""


# ---------------------------------------------------------------- indicator
def render_spec_indicator(spec: dict, name: str, mql: int = 5,
                          params: dict | None = None, metrics: dict | None = None,
                          walk_forward: dict | None = None, score: float = 0.0,
                          symbol: str = "", timeframe: str = "") -> str:
    """Compile a composite spec into a NON-TRADING chart indicator.

    Same rules, same votes, same agreement threshold as `render_spec_ea` - but
    it places no orders. It draws a buy/sell arrow on every bar the strategy
    would have signalled and prints a live rule-by-rule breakdown in the corner,
    so a human can eyeball the logic on a chart before ever risking money.

    The rules are replayed over history at an arbitrary shift, which is why
    `_rule_code` takes `s1`/`s2` expressions rather than hardcoded 1 and 2.
    """
    params = params or {}
    metrics = metrics or {}
    ex = dict(spec.get("exit") or {})
    atr_mult = float(params.get("atr_mult", ex.get("atr_mult", 2.0)))
    rr = float(params.get("rr", ex.get("rr", 2.0)))
    agreement = float(params.get("agreement", spec.get("agreement", 0.6)))
    atr_n = int((spec.get("params") or {}).get("atr_n", 14))

    entry = [r for r in (spec.get("entry") or []) if isinstance(r, dict)]
    filters = [r for r in (spec.get("filters") or []) if isinstance(r, dict)]

    ind = _Ind()
    ebody, fbody = [], []
    for k, r in enumerate(entry):
        ebody.append(f"      // entry rule {k + 1}: {r.get('type')}"
                     + _rule_code(r, ind, mql, f"ev[{k}]", s1="sh", s2="sh+1"))
    for k, r in enumerate(filters):
        fbody.append(f"      // filter {k + 1}: {r.get('type')}"
                     + _rule_code(r, ind, mql, f"fv[{k}]", s1="sh", s2="sh+1"))

    entry_names = ", ".join(str(r.get("type")) for r in entry) or "none"
    filter_names = ", ".join(str(r.get("type")) for r in filters) or "none"
    m = metrics
    header = f"""//+------------------------------------------------------------------+
//| {name}.mq{mql}  --  SIGNAL INDICATOR (does not trade)
//| Compiled from a strategy spec by the Agentic Trading Firm
//| {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}
//|
//| Strategy   : {spec.get('name', name)}
//| Symbol/TF  : {symbol or 'any'} {timeframe or 'chart'}
//| Entry rules: {entry_names}
//| Filters    : {filter_names}
//| Agreement  : {agreement}
//|
//| {str(spec.get('summary', ''))[:110]}
//|
//| BACKTEST (in-sample, spread-adjusted)
//|   trades {m.get('trades', 0)} | win {m.get('win_rate', 0)}% | PF {m.get('profit_factor', 0)}
//|   expectancy {m.get('expectancy_r', 0)}R | robustness score {score}
//|
//| This indicator PLOTS the same decision the EA would act on. It places no
//| orders. Arrows appear on the bar AFTER the signal bar closes, matching the
//| backtest, so what you see is what was actually measured.
//| A `session` filter uses BROKER SERVER TIME here but UTC in the backtest.
//+------------------------------------------------------------------+
#property copyright "Agentic Trading Firm"
#property version   "1.10"
#property strict
#property indicator_chart_window
#property indicator_buffers 2
#property indicator_plots   2

#property indicator_label1  "Buy"
#property indicator_type1   DRAW_ARROW
#property indicator_color1  clrLimeGreen
#property indicator_width1  2
#property indicator_label2  "Sell"
#property indicator_type2   DRAW_ARROW
#property indicator_color2  clrOrangeRed
#property indicator_width2  2

input double Agreement   = {agreement};   // fraction of entry rules that must agree
input int    ATRPeriod   = {atr_n};
input double ATRMultiplier = {atr_mult};  // stop distance shown on the panel
input double RewardRatio = {rr};          // target = R x stop
input bool   ShowPanel   = true;          // live rule breakdown, top-left
input bool   AlertOnSignal = false;       // popup alert on a fresh signal

#define N_ENTRY {max(len(entry), 1)}
#define N_FILTER {max(len(filters), 1)}

double BufUp[];
double BufDn[];
"""

    # Decide() takes a shift so history can be replayed.
    decide = f"""
//+------------------------------------------------------------------+
//| Vote at bar `sh` - identical logic to the EA and to Python        |
//+------------------------------------------------------------------+
int DecideAt(int sh, double atr)
  {{
   int fv[N_FILTER]; int ev[N_ENTRY];
   for(int i=0;i<N_FILTER;i++) fv[i]=0;
   for(int i=0;i<N_ENTRY;i++)  ev[i]=0;

   {{
{chr(10).join(fbody) if fbody else '      // no filters'}
   }}

   for(int i=0;i<{len(filters) if filters else 0};i++)
      if(fv[i]==-99) return 0;

   {{
{chr(10).join(ebody) if ebody else '      // no entry rules'}
   }}

   int bulls=0, bears=0, total={len(entry)};
   for(int i=0;i<{len(entry)};i++)
     {{
      if(ev[i]==-99) return 0;
      if(ev[i]>0) bulls++;
      if(ev[i]<0) bears++;
     }}
   if(total<=0) return 0;

   gBulls=bulls; gBears=bears;
   double bu=(double)bulls/(double)total, be=(double)bears/(double)total;
   if(bu>=Agreement && bulls>bears) return 1;
   if(be>=Agreement && bears>bulls) return -1;
   return 0;
  }}
"""

    panel = f"""
void Panel(int dir, double atr)
  {{
   if(!ShowPanel) return;
   string nm="{name}_panel";
   string verdict = (dir>0 ? "BUY setup" : (dir<0 ? "SELL setup" : "no setup"));
   color  col     = (dir>0 ? clrLimeGreen : (dir<0 ? clrOrangeRed : clrSilver));
   string txt = "{spec.get('name', name)}"
              + "\\n" + verdict
              + "\\nvotes  " + IntegerToString(gBulls) + " bull / "
              + IntegerToString(gBears) + " bear  of {len(entry)}"
              + "\\nagree  " + DoubleToString(Agreement*100,0) + "%"
              + "\\nATR    " + DoubleToString(atr,(int)SymbolInfoInteger(_Symbol,SYMBOL_DIGITS))
              + "\\nstop   " + DoubleToString(atr*ATRMultiplier,(int)SymbolInfoInteger(_Symbol,SYMBOL_DIGITS))
              + "\\ntarget " + DoubleToString(atr*ATRMultiplier*RewardRatio,(int)SymbolInfoInteger(_Symbol,SYMBOL_DIGITS));
   if(ObjectFind(0,nm)<0)
     {{
      ObjectCreate(0,nm,OBJ_LABEL,0,0,0);
      ObjectSetInteger(0,nm,OBJPROP_CORNER,CORNER_LEFT_UPPER);
      ObjectSetInteger(0,nm,OBJPROP_XDISTANCE,12);
      ObjectSetInteger(0,nm,OBJPROP_YDISTANCE,20);
      ObjectSetInteger(0,nm,OBJPROP_FONTSIZE,9);
      ObjectSetString(0,nm,OBJPROP_FONT,"Consolas");
     }}
   ObjectSetString(0,nm,OBJPROP_TEXT,txt);
   ObjectSetInteger(0,nm,OBJPROP_COLOR,col);
  }}
"""

    if mql == 5:
        handles, creates = [], []
        for n in sorted(ind.ema):
            handles.append(f"int hEma{n};")
            creates.append(f"   hEma{n}=iMA(_Symbol,PERIOD_CURRENT,{n},0,MODE_EMA,PRICE_CLOSE);")
        for n in sorted(ind.sma):
            handles.append(f"int hSma{n};")
            creates.append(f"   hSma{n}=iMA(_Symbol,PERIOD_CURRENT,{n},0,MODE_SMA,PRICE_CLOSE);")
        for n in sorted(ind.rsi):
            handles.append(f"int hRsi{n};")
            creates.append(f"   hRsi{n}=iRSI(_Symbol,PERIOD_CURRENT,{n},PRICE_CLOSE);")
        for f, s, g in sorted(ind.macd):
            handles.append(f"int hMacd_{f}_{s}_{g};")
            creates.append(f"   hMacd_{f}_{s}_{g}=iMACD(_Symbol,PERIOD_CURRENT,"
                           f"{f},{s},{g},PRICE_CLOSE);")
        for n, mm in sorted(ind.bands):
            hn = f"hBb_{n}_{str(mm).replace('.', 'p')}"
            handles.append(f"int {hn};")
            creates.append(f"   {hn}=iBands(_Symbol,PERIOD_CURRENT,{n},0,{mm},PRICE_CLOSE);")

        return header + f"""
int hAtr;
{chr(10).join(handles)}
int gBulls=0, gBears=0;
datetime lastAlert=0;

double Buf(int h,int b,int s)
  {{
   double v[];
   if(h==INVALID_HANDLE) return 0.0;
   if(CopyBuffer(h,b,s,1,v)<1) return 0.0;
   return v[0];
  }}
double Op(int s){{ return iOpen(_Symbol,PERIOD_CURRENT,s); }}
double Hi(int s){{ return iHigh(_Symbol,PERIOD_CURRENT,s); }}
double Lo(int s){{ return iLow(_Symbol,PERIOD_CURRENT,s); }}
double Cl(int s){{ return iClose(_Symbol,PERIOD_CURRENT,s); }}
int BarHour(int s)
  {{
   MqlDateTime dt;
   TimeToStruct(iTime(_Symbol,PERIOD_CURRENT,s),dt);
   return dt.hour;
  }}
double HH(int n,int shift)
  {{
   double h[];
   if(CopyHigh(_Symbol,PERIOD_CURRENT,shift,n,h)<n) return 0.0;
   return h[ArrayMaximum(h)];
  }}
double LL(int n,int shift)
  {{
   double l[];
   if(CopyLow(_Symbol,PERIOD_CURRENT,shift,n,l)<n) return 0.0;
   return l[ArrayMinimum(l)];
  }}
{decide}{panel}
int OnInit()
  {{
   SetIndexBuffer(0,BufUp,INDICATOR_DATA);
   SetIndexBuffer(1,BufDn,INDICATOR_DATA);
   PlotIndexSetInteger(0,PLOT_ARROW,233);
   PlotIndexSetInteger(1,PLOT_ARROW,234);
   PlotIndexSetDouble(0,PLOT_EMPTY_VALUE,0.0);
   PlotIndexSetDouble(1,PLOT_EMPTY_VALUE,0.0);
   ArraySetAsSeries(BufUp,false);
   ArraySetAsSeries(BufDn,false);
   hAtr=iATR(_Symbol,PERIOD_CURRENT,ATRPeriod);
{chr(10).join(creates)}
   if(hAtr==INVALID_HANDLE) return INIT_FAILED;
   IndicatorSetString(INDICATOR_SHORTNAME,"{name}");
   return INIT_SUCCEEDED;
  }}

void OnDeinit(const int reason) {{ ObjectDelete(0,"{name}_panel"); }}

int OnCalculate(const int rates_total,
                const int prev_calculated,
                const datetime &time[],
                const double &open[],
                const double &high[],
                const double &low[],
                const double &close[],
                const long &tick_volume[],
                const long &volume[],
                const int &spread[])
  {{
   int warmup = 260;
   if(rates_total < warmup+5) return 0;
   int start = (prev_calculated>warmup) ? prev_calculated-1 : warmup;

   for(int i=start; i<rates_total; i++)
     {{
      BufUp[i]=0.0; BufDn[i]=0.0;
      int sh = rates_total-1-i;        // series shift of bar i
      if(sh<1) continue;               // never evaluate the forming bar
      double atr = Buf(hAtr,0,sh);
      if(atr<=0) continue;
      int dir = DecideAt(sh,atr);
      // the signal is actionable on the NEXT bar, matching the backtest
      if(dir>0 && i+1<rates_total) BufUp[i+1] = low[i+1]  - atr*0.6;
      if(dir<0 && i+1<rates_total) BufDn[i+1] = high[i+1] + atr*0.6;
     }}

   double atrNow = Buf(hAtr,0,1);
   int dirNow = (atrNow>0) ? DecideAt(1,atrNow) : 0;
   Panel(dirNow,atrNow);
   if(AlertOnSignal && dirNow!=0)
     {{
      datetime bt = iTime(_Symbol,PERIOD_CURRENT,0);
      if(bt!=lastAlert)
        {{
         lastAlert=bt;
         Alert("{name}: ",(dirNow>0?"BUY":"SELL")," setup on ",_Symbol," ",EnumToString(_Period));
        }}
     }}
   return rates_total;
  }}
"""

    # ---------------- MQL4 ----------------
    return header + f"""
int gBulls=0, gBears=0;
datetime lastAlert=0;

double Op(int s){{ return iOpen(NULL,0,s); }}
double Hi(int s){{ return iHigh(NULL,0,s); }}
double Lo(int s){{ return iLow(NULL,0,s); }}
double Cl(int s){{ return iClose(NULL,0,s); }}
int BarHour(int s){{ return TimeHour(iTime(NULL,0,s)); }}
double HH(int n,int shift){{ return High[iHighest(NULL,0,MODE_HIGH,n,shift)]; }}
double LL(int n,int shift){{ return Low[iLowest(NULL,0,MODE_LOW,n,shift)]; }}
{decide}
void Panel(int dir, double atr)
  {{
   if(!ShowPanel) return;
   string nm="{name}_panel";
   string verdict = (dir>0 ? "BUY setup" : (dir<0 ? "SELL setup" : "no setup"));
   color  col     = (dir>0 ? clrLimeGreen : (dir<0 ? clrOrangeRed : clrSilver));
   string txt = "{spec.get('name', name)}"
              + "\\n" + verdict
              + "\\nvotes  " + IntegerToString(gBulls) + " bull / "
              + IntegerToString(gBears) + " bear  of {len(entry)}"
              + "\\nagree  " + DoubleToString(Agreement*100,0) + "%"
              + "\\nATR    " + DoubleToString(atr,Digits)
              + "\\nstop   " + DoubleToString(atr*ATRMultiplier,Digits)
              + "\\ntarget " + DoubleToString(atr*ATRMultiplier*RewardRatio,Digits);
   if(ObjectFind(nm)<0)
     {{
      ObjectCreate(nm,OBJ_LABEL,0,0,0);
      ObjectSet(nm,OBJPROP_CORNER,0);
      ObjectSet(nm,OBJPROP_XDISTANCE,12);
      ObjectSet(nm,OBJPROP_YDISTANCE,20);
     }}
   ObjectSetText(nm,txt,9,"Consolas",col);
  }}

int OnInit()
  {{
   SetIndexBuffer(0,BufUp);
   SetIndexBuffer(1,BufDn);
   SetIndexStyle(0,DRAW_ARROW,EMPTY,2,clrLimeGreen); SetIndexArrow(0,233);
   SetIndexStyle(1,DRAW_ARROW,EMPTY,2,clrOrangeRed); SetIndexArrow(1,234);
   SetIndexEmptyValue(0,0.0);
   SetIndexEmptyValue(1,0.0);
   IndicatorShortName("{name}");
   return INIT_SUCCEEDED;
  }}

void OnDeinit(const int reason) {{ ObjectDelete("{name}_panel"); }}

int OnCalculate(const int rates_total,
                const int prev_calculated,
                const datetime &time[],
                const double &open[],
                const double &high[],
                const double &low[],
                const double &close[],
                const long &tick_volume[],
                const long &volume[],
                const int &spread[])
  {{
   int warmup = 260;
   if(rates_total < warmup+5) return 0;
   int limit = rates_total - prev_calculated + 1;
   if(limit > rates_total-warmup) limit = rates_total-warmup;

   for(int sh=limit; sh>=1; sh--)
     {{
      BufUp[sh]=0.0; BufDn[sh]=0.0;
      double atr = iATR(NULL,0,ATRPeriod,sh);
      if(atr<=0) continue;
      int dir = DecideAt(sh,atr);
      // actionable on the NEXT bar (smaller shift), matching the backtest
      if(sh-1>=0)
        {{
         if(dir>0) BufUp[sh-1] = Low[sh-1]  - atr*0.6;
         if(dir<0) BufDn[sh-1] = High[sh-1] + atr*0.6;
        }}
     }}

   double atrNow = iATR(NULL,0,ATRPeriod,1);
   int dirNow = (atrNow>0) ? DecideAt(1,atrNow) : 0;
   Panel(dirNow,atrNow);
   if(AlertOnSignal && dirNow!=0)
     {{
      if(Time[0]!=lastAlert)
        {{
         lastAlert=Time[0];
         Alert("{name}: ",(dirNow>0?"BUY":"SELL")," setup on ",Symbol());
        }}
     }}
   return rates_total;
  }}
"""
