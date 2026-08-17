//+------------------------------------------------------------------+
//| T_Both.mq5
//| Compiled from a strategy spec by the Agentic Trading Firm
//| 2026-08-17 01:04 UTC
//|
//| Strategy   : 3 EMA Ribbon Pullback
//| Source     : spec
//| Symbol/TF  : EURUSD H1
//| Entry rules: 3   Filters: 1   Agreement: 0.6
//|
//| Classic 9/21/50 EMA stack; enter on a pullback to the mid EMA while momentum stays constructive.
//|
//| BACKTEST (in-sample, spread-adjusted)
//|   trades 0 | win 0% | PF 0
//|   expectancy 0R | maxDD 0R
//|   sharpe 0 | robustness score 1.0
//| WALK-FORWARD: {}
//|
//| RULES: {"entry": [{"type": "ema_stack", "fast": 9, "mid": 21, "slow": 50}, {"type": "pullback", "to": "ema_mid", "mid": 21, "max_atr": 1.0}, {"type": "rsi_zone", "period": 14, "min": 45, "max": 75}], "filters": [{"type": "session", "from": 7, "to": 17}], "exit": {"atr_mult": 1.5, "rr": 2.0}, "agreement": 0.6}
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
input double ATRMultiplier = 1.5;   // stop distance in ATR
input double RewardRatio   = 2.0;   // target = R x stop
input double Agreement     = 0.6;  // fraction of rules that must agree
input int    ATRPeriod     = 14;
input int    MagicNumber   = 127299;
input int    Slippage      = 20;
input int    MaxPositions  = 1;
input bool   TradeOnNewBarOnly = true;

#define N_ENTRY 3
#define N_FILTER 1

#include <Trade/Trade.mqh>
CTrade trade;
int hAtr;
int hEma9;
int hEma21;
int hEma50;
int hRsi14;
datetime lastBar = 0;

double Buf(int h,int b,int s)
  {
   double v[];
   if(h==INVALID_HANDLE) return 0.0;
   if(CopyBuffer(h,b,s,1,v)<1) return 0.0;
   return v[0];
  }
double Op(int s){ return iOpen(_Symbol,PERIOD_CURRENT,s); }
double Hi(int s){ return iHigh(_Symbol,PERIOD_CURRENT,s); }
double Lo(int s){ return iLow(_Symbol,PERIOD_CURRENT,s); }
double Cl(int s){ return iClose(_Symbol,PERIOD_CURRENT,s); }
int BarHour(int s)
  {
   MqlDateTime dt;
   TimeToStruct(iTime(_Symbol,PERIOD_CURRENT,s),dt);
   return dt.hour;
  }
double HH(int n,int shift)
  {
   double h[];
   if(CopyHigh(_Symbol,PERIOD_CURRENT,shift,n,h)<n) return 0.0;
   return h[ArrayMaximum(h)];
  }
double LL(int n,int shift)
  {
   double l[];
   if(CopyLow(_Symbol,PERIOD_CURRENT,shift,n,l)<n) return 0.0;
   return l[ArrayMinimum(l)];
  }

int OnInit()
  {
   trade.SetExpertMagicNumber(MagicNumber);
   trade.SetDeviationInPoints(Slippage);
   hAtr=iATR(_Symbol,PERIOD_CURRENT,ATRPeriod);
   hEma9=iMA(_Symbol,PERIOD_CURRENT,9,0,MODE_EMA,PRICE_CLOSE);
   hEma21=iMA(_Symbol,PERIOD_CURRENT,21,0,MODE_EMA,PRICE_CLOSE);
   hEma50=iMA(_Symbol,PERIOD_CURRENT,50,0,MODE_EMA,PRICE_CLOSE);
   hRsi14=iRSI(_Symbol,PERIOD_CURRENT,14,PRICE_CLOSE);
   if(hAtr==INVALID_HANDLE) return INIT_FAILED;
   Print("T_Both ready. Risk ",RiskPercent,"% | ATRx",ATRMultiplier,
         " | RR ",RewardRatio," | agreement ",Agreement);
   return INIT_SUCCEEDED;
  }

//+------------------------------------------------------------------+
//| Majority vote - mirrors composite.evaluate_spec exactly           |
//+------------------------------------------------------------------+
int Decide(double atr)
  {
   int fv[N_FILTER]; int ev[N_ENTRY];
   for(int i=0;i<N_FILTER;i++) fv[i]=0;
   for(int i=0;i<N_ENTRY;i++)  ev[i]=0;

   // filter 1: session
   {
    int hr=BarHour(1);
    fv[0] = (hr>=7 && hr<17) ? 0 : -99;   // -99 = hard veto
   }

   // any filter may hard-veto the bar
   for(int i=0;i<1;i++)
      if(fv[i]==-99) return 0;

   // entry rule 1: ema_stack
   {
    double f=Buf(hEma9,0,1), m=Buf(hEma21,0,1), s=Buf(hEma50,0,1);
    if(f<=0||m<=0||s<=0) ev[0]=0;
    else if(f>m && m>s)  ev[0]=1;
    else if(f<m && m<s)  ev[0]=-1;
    else                 ev[0]=0;
   }
   // entry rule 2: pullback
   {
    double ma=Buf(hEma21,0,1), px=Cl(1);
    if(ma<=0||atr<=0) ev[1]=0;
    else              ev[1]=(MathAbs(px-ma)/atr<=1.0)?1:0;
   }
   // entry rule 3: rsi_zone
   {
    double r=Buf(hRsi14,0,1);
    if(r<=0)                    ev[2]=0;
    else if(r>=45.0 && r<=75.0) ev[2]=(r>=50?1:-1);
    else                        ev[2]=0;
   }

   int bulls=0, bears=0, total=3;
   for(int i=0;i<3;i++)
     {
      if(ev[i]==-99) return 0;      // veto inside an entry rule
      if(ev[i]>0) bulls++;
      if(ev[i]<0) bears++;
     }
   if(total<=0) return 0;

   double bu=(double)bulls/(double)total, be=(double)bears/(double)total;
   if(bu>=Agreement && bulls>bears) return 1;
   if(be>=Agreement && bears>bulls) return -1;
   return 0;
  }

int CountPositions()
  {
   int c=0;
   for(int i=PositionsTotal()-1;i>=0;i--)
     {
      ulong tk=PositionGetTicket(i);
      if(tk==0) continue;
      if(PositionSelectByTicket(tk) && PositionGetString(POSITION_SYMBOL)==_Symbol
         && PositionGetInteger(POSITION_MAGIC)==MagicNumber) c++;
     }
   return c;
  }

double LotsForRisk(double stopDistance)
  {
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
  }

void OnTick()
  {
   if(TradeOnNewBarOnly)
     {
      datetime t=iTime(_Symbol,PERIOD_CURRENT,0);
      if(t==lastBar) return;
      lastBar=t;
     }
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
     {
      double sl=NormalizeDouble(ask-stopDist,digits);
      double tp=NormalizeDouble(ask+stopDist*RewardRatio,digits);
      trade.Buy(lots,_Symbol,ask,sl,tp,"T_Both");
     }
   else
     {
      double sl=NormalizeDouble(bid+stopDist,digits);
      double tp=NormalizeDouble(bid-stopDist*RewardRatio,digits);
      trade.Sell(lots,_Symbol,bid,sl,tp,"T_Both");
     }
  }
