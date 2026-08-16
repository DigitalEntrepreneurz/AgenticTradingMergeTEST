//+------------------------------------------------------------------+
//| Bollinger_Band_Mean_Reversion_EURUSD_H1.mq5
//| Compiled from a strategy spec by the Agentic Trading Firm
//| 2026-08-13 11:04 UTC
//|
//| Strategy   : Bollinger Band Mean Reversion
//| Source     : catalogue
//| Symbol/TF  : EURUSD H1
//| Entry rules: 2   Filters: 0   Agreement: 0.5
//|
//| Fade closes outside the bands when RSI confirms an extreme.
//|
//| BACKTEST (in-sample, spread-adjusted)
//|   trades 31 | win 70.97% | PF 6.037
//|   expectancy 1.4708R | maxDD 1.006R
//|   sharpe 5.17 | robustness score 2.541
//| WALK-FORWARD: {"folds": 3, "oos_total_r": 4.568, "oos_trades": 37, "oos_expectancy_r": 0.1235, "params_per_fold": [{"atr_mult": 3, "rr": 1.5, "agreement": 0.5}, {"a
//|
//| RULES: {"entry": [{"type": "bb_touch", "mode": "reversion"}, {"type": "rsi_extreme", "period": 14, "oversold": 30, "overbought": 70}], "filters": [], "exit": {"atr_mult": 3.0, "rr": 2.5}, "agreement": 0.5}
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
input double ATRMultiplier = 3.0;   // stop distance in ATR
input double RewardRatio   = 2.5;   // target = R x stop
input double Agreement     = 0.5;  // fraction of rules that must agree
input int    ATRPeriod     = 14;
input int    MagicNumber   = 349551;
input int    Slippage      = 20;
input int    MaxPositions  = 1;
input bool   TradeOnNewBarOnly = true;

#define N_ENTRY 2
#define N_FILTER 1

#include <Trade/Trade.mqh>
CTrade trade;
int hAtr;
int hRsi14;
int hBb_20_2p0;
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
   hRsi14=iRSI(_Symbol,PERIOD_CURRENT,14,PRICE_CLOSE);
   hBb_20_2p0=iBands(_Symbol,PERIOD_CURRENT,20,0,2.0,PRICE_CLOSE);
   if(hAtr==INVALID_HANDLE) return INIT_FAILED;
   Print("Bollinger_Band_Mean_Reversion_EURUSD_H1 ready. Risk ",RiskPercent,"% | ATRx",ATRMultiplier,
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

   // no filters

   // any filter may hard-veto the bar
   for(int i=0;i<0;i++)
      if(fv[i]==-99) return 0;

   // entry rule 1: bb_touch
   {
    double up=Buf(hBb_20_2p0,1,1), lo=Buf(hBb_20_2p0,2,1);
    double px=Cl(1);
    if(up<=0||lo<=0)   ev[0]=0;
    else if(px<lo)     ev[0]=1;
    else if(px>up)     ev[0]=-1;
    else               ev[0]=0;
   }
   // entry rule 2: rsi_extreme
   {
    double r=Buf(hRsi14,0,1);
    if(r<=0)        ev[1]=0;
    else if(r<30.0) ev[1]=1;
    else if(r>70.0)  ev[1]=-1;
    else            ev[1]=0;
   }

   int bulls=0, bears=0, total=2;
   for(int i=0;i<2;i++)
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
      trade.Buy(lots,_Symbol,ask,sl,tp,"Bollinger_Band_Mean_Reversion_EURUSD_H1");
     }
   else
     {
      double sl=NormalizeDouble(bid+stopDist,digits);
      double tp=NormalizeDouble(bid-stopDist*RewardRatio,digits);
      trade.Sell(lots,_Symbol,bid,sl,tp,"Bollinger_Band_Mean_Reversion_EURUSD_H1");
     }
  }
