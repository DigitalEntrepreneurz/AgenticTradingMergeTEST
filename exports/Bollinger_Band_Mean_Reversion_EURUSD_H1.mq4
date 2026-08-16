//+------------------------------------------------------------------+
//| Bollinger_Band_Mean_Reversion_EURUSD_H1.mq4
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

datetime lastBar = 0;

double Op(int s){ return iOpen(NULL,0,s); }
double Hi(int s){ return iHigh(NULL,0,s); }
double Lo(int s){ return iLow(NULL,0,s); }
double Cl(int s){ return iClose(NULL,0,s); }
int BarHour(int s){ return TimeHour(iTime(NULL,0,s)); }
double HH(int n,int shift){ return High[iHighest(NULL,0,MODE_HIGH,n,shift)]; }
double LL(int n,int shift){ return Low[iLowest(NULL,0,MODE_LOW,n,shift)]; }

int OnInit()
  {
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
    double up=iBands(NULL,0,20,2.0,0,PRICE_CLOSE,MODE_UPPER,1), lo=iBands(NULL,0,20,2.0,0,PRICE_CLOSE,MODE_LOWER,1);
    double px=Cl(1);
    if(up<=0||lo<=0)   ev[0]=0;
    else if(px<lo)     ev[0]=1;
    else if(px>up)     ev[0]=-1;
    else               ev[0]=0;
   }
   // entry rule 2: rsi_extreme
   {
    double r=iRSI(NULL,0,14,PRICE_CLOSE,1);
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
   for(int i=OrdersTotal()-1;i>=0;i--)
     {
      if(!OrderSelect(i,SELECT_BY_POS,MODE_TRADES)) continue;
      if(OrderSymbol()==Symbol() && OrderMagicNumber()==MagicNumber
         && (OrderType()==OP_BUY||OrderType()==OP_SELL)) c++;
     }
   return c;
  }

double LotsForRisk(double stopDistance)
  {
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
  }

void OnTick()
  {
   if(TradeOnNewBarOnly)
     {
      if(Time[0]==lastBar) return;
      lastBar=Time[0];
     }
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
     {
      double sl=NormalizeDouble(Ask-stopDist,digits);
      double tp=NormalizeDouble(Ask+stopDist*RewardRatio,digits);
      int t1=OrderSend(Symbol(),OP_BUY,lots,NormalizeDouble(Ask,digits),Slippage,
                       sl,tp,"Bollinger_Band_Mean_Reversion_EURUSD_H1",MagicNumber,0,clrDodgerBlue);
      if(t1<0) Print("Buy failed err=",GetLastError());
     }
   else
     {
      double sl=NormalizeDouble(Bid+stopDist,digits);
      double tp=NormalizeDouble(Bid-stopDist*RewardRatio,digits);
      int t2=OrderSend(Symbol(),OP_SELL,lots,NormalizeDouble(Bid,digits),Slippage,
                       sl,tp,"Bollinger_Band_Mean_Reversion_EURUSD_H1",MagicNumber,0,clrOrangeRed);
      if(t2<0) Print("Sell failed err=",GetLastError());
     }
  }
