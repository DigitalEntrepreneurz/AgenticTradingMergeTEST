//+------------------------------------------------------------------+
//| T_Both.mq4
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
    double f=iMA(NULL,0,9,0,MODE_EMA,PRICE_CLOSE,1), m=iMA(NULL,0,21,0,MODE_EMA,PRICE_CLOSE,1), s=iMA(NULL,0,50,0,MODE_EMA,PRICE_CLOSE,1);
    if(f<=0||m<=0||s<=0) ev[0]=0;
    else if(f>m && m>s)  ev[0]=1;
    else if(f<m && m<s)  ev[0]=-1;
    else                 ev[0]=0;
   }
   // entry rule 2: pullback
   {
    double ma=iMA(NULL,0,21,0,MODE_EMA,PRICE_CLOSE,1), px=Cl(1);
    if(ma<=0||atr<=0) ev[1]=0;
    else              ev[1]=(MathAbs(px-ma)/atr<=1.0)?1:0;
   }
   // entry rule 3: rsi_zone
   {
    double r=iRSI(NULL,0,14,PRICE_CLOSE,1);
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
                       sl,tp,"T_Both",MagicNumber,0,clrDodgerBlue);
      if(t1<0) Print("Buy failed err=",GetLastError());
     }
   else
     {
      double sl=NormalizeDouble(Bid+stopDist,digits);
      double tp=NormalizeDouble(Bid-stopDist*RewardRatio,digits);
      int t2=OrderSend(Symbol(),OP_SELL,lots,NormalizeDouble(Bid,digits),Slippage,
                       sl,tp,"T_Both",MagicNumber,0,clrOrangeRed);
      if(t2<0) Print("Sell failed err=",GetLastError());
     }
  }
