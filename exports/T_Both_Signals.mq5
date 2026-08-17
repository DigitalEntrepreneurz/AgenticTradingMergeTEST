//+------------------------------------------------------------------+
//| T_Both_Signals.mq5  --  SIGNAL INDICATOR (does not trade)
//| Compiled from a strategy spec by the Agentic Trading Firm
//| 2026-08-17 01:32 UTC
//|
//| Strategy   : 3 EMA Ribbon Pullback
//| Symbol/TF  : EURUSD H1
//| Entry rules: ema_stack, pullback, rsi_zone
//| Filters    : session
//| Agreement  : 0.6
//|
//| Classic 9/21/50 EMA stack; enter on a pullback to the mid EMA while momentum stays constructive.
//|
//| BACKTEST (in-sample, spread-adjusted)
//|   trades 0 | win 0% | PF 0
//|   expectancy 0R | robustness score 1.0
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

input double Agreement   = 0.6;   // fraction of entry rules that must agree
input int    ATRPeriod   = 14;
input double ATRMultiplier = 1.5;  // stop distance shown on the panel
input double RewardRatio = 2.0;          // target = R x stop
input bool   ShowPanel   = true;          // live rule breakdown, top-left
input bool   AlertOnSignal = false;       // popup alert on a fresh signal

#define N_ENTRY 3
#define N_FILTER 1

double BufUp[];
double BufDn[];

int hAtr;
int hEma9;
int hEma21;
int hEma50;
int hRsi14;
int gBulls=0, gBears=0;
datetime lastAlert=0;

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

//+------------------------------------------------------------------+
//| Vote at bar `sh` - identical logic to the EA and to Python        |
//+------------------------------------------------------------------+
int DecideAt(int sh, double atr)
  {
   int fv[N_FILTER]; int ev[N_ENTRY];
   for(int i=0;i<N_FILTER;i++) fv[i]=0;
   for(int i=0;i<N_ENTRY;i++)  ev[i]=0;

   {
      // filter 1: session
   {
    int hr=BarHour(sh);
    fv[0] = (hr>=7 && hr<17) ? 0 : -99;   // -99 = hard veto
   }
   }

   for(int i=0;i<1;i++)
      if(fv[i]==-99) return 0;

   {
      // entry rule 1: ema_stack
   {
    double f=Buf(hEma9,0,sh), m=Buf(hEma21,0,sh), s=Buf(hEma50,0,sh);
    if(f<=0||m<=0||s<=0) ev[0]=0;
    else if(f>m && m>s)  ev[0]=1;
    else if(f<m && m<s)  ev[0]=-1;
    else                 ev[0]=0;
   }
      // entry rule 2: pullback
   {
    double ma=Buf(hEma21,0,sh), px=Cl(sh);
    if(ma<=0||atr<=0) ev[1]=0;
    else              ev[1]=(MathAbs(px-ma)/atr<=1.0)?1:0;
   }
      // entry rule 3: rsi_zone
   {
    double r=Buf(hRsi14,0,sh);
    if(r<=0)                    ev[2]=0;
    else if(r>=45.0 && r<=75.0) ev[2]=(r>=50?1:-1);
    else                        ev[2]=0;
   }
   }

   int bulls=0, bears=0, total=3;
   for(int i=0;i<3;i++)
     {
      if(ev[i]==-99) return 0;
      if(ev[i]>0) bulls++;
      if(ev[i]<0) bears++;
     }
   if(total<=0) return 0;

   gBulls=bulls; gBears=bears;
   double bu=(double)bulls/(double)total, be=(double)bears/(double)total;
   if(bu>=Agreement && bulls>bears) return 1;
   if(be>=Agreement && bears>bulls) return -1;
   return 0;
  }

void Panel(int dir, double atr)
  {
   if(!ShowPanel) return;
   string nm="T_Both_Signals_panel";
   string verdict = (dir>0 ? "BUY setup" : (dir<0 ? "SELL setup" : "no setup"));
   color  col     = (dir>0 ? clrLimeGreen : (dir<0 ? clrOrangeRed : clrSilver));
   string txt = "3 EMA Ribbon Pullback"
              + "\n" + verdict
              + "\nvotes  " + IntegerToString(gBulls) + " bull / "
              + IntegerToString(gBears) + " bear  of 3"
              + "\nagree  " + DoubleToString(Agreement*100,0) + "%"
              + "\nATR    " + DoubleToString(atr,(int)SymbolInfoInteger(_Symbol,SYMBOL_DIGITS))
              + "\nstop   " + DoubleToString(atr*ATRMultiplier,(int)SymbolInfoInteger(_Symbol,SYMBOL_DIGITS))
              + "\ntarget " + DoubleToString(atr*ATRMultiplier*RewardRatio,(int)SymbolInfoInteger(_Symbol,SYMBOL_DIGITS));
   if(ObjectFind(0,nm)<0)
     {
      ObjectCreate(0,nm,OBJ_LABEL,0,0,0);
      ObjectSetInteger(0,nm,OBJPROP_CORNER,CORNER_LEFT_UPPER);
      ObjectSetInteger(0,nm,OBJPROP_XDISTANCE,12);
      ObjectSetInteger(0,nm,OBJPROP_YDISTANCE,20);
      ObjectSetInteger(0,nm,OBJPROP_FONTSIZE,9);
      ObjectSetString(0,nm,OBJPROP_FONT,"Consolas");
     }
   ObjectSetString(0,nm,OBJPROP_TEXT,txt);
   ObjectSetInteger(0,nm,OBJPROP_COLOR,col);
  }

int OnInit()
  {
   SetIndexBuffer(0,BufUp,INDICATOR_DATA);
   SetIndexBuffer(1,BufDn,INDICATOR_DATA);
   PlotIndexSetInteger(0,PLOT_ARROW,233);
   PlotIndexSetInteger(1,PLOT_ARROW,234);
   PlotIndexSetDouble(0,PLOT_EMPTY_VALUE,0.0);
   PlotIndexSetDouble(1,PLOT_EMPTY_VALUE,0.0);
   ArraySetAsSeries(BufUp,false);
   ArraySetAsSeries(BufDn,false);
   hAtr=iATR(_Symbol,PERIOD_CURRENT,ATRPeriod);
   hEma9=iMA(_Symbol,PERIOD_CURRENT,9,0,MODE_EMA,PRICE_CLOSE);
   hEma21=iMA(_Symbol,PERIOD_CURRENT,21,0,MODE_EMA,PRICE_CLOSE);
   hEma50=iMA(_Symbol,PERIOD_CURRENT,50,0,MODE_EMA,PRICE_CLOSE);
   hRsi14=iRSI(_Symbol,PERIOD_CURRENT,14,PRICE_CLOSE);
   if(hAtr==INVALID_HANDLE) return INIT_FAILED;
   IndicatorSetString(INDICATOR_SHORTNAME,"T_Both_Signals");
   return INIT_SUCCEEDED;
  }

void OnDeinit(const int reason) { ObjectDelete(0,"T_Both_Signals_panel"); }

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
  {
   int warmup = 260;
   if(rates_total < warmup+5) return 0;
   int start = (prev_calculated>warmup) ? prev_calculated-1 : warmup;

   for(int i=start; i<rates_total; i++)
     {
      BufUp[i]=0.0; BufDn[i]=0.0;
      int sh = rates_total-1-i;        // series shift of bar i
      if(sh<1) continue;               // never evaluate the forming bar
      double atr = Buf(hAtr,0,sh);
      if(atr<=0) continue;
      int dir = DecideAt(sh,atr);
      // the signal is actionable on the NEXT bar, matching the backtest
      if(dir>0 && i+1<rates_total) BufUp[i+1] = low[i+1]  - atr*0.6;
      if(dir<0 && i+1<rates_total) BufDn[i+1] = high[i+1] + atr*0.6;
     }

   double atrNow = Buf(hAtr,0,1);
   int dirNow = (atrNow>0) ? DecideAt(1,atrNow) : 0;
   Panel(dirNow,atrNow);
   if(AlertOnSignal && dirNow!=0)
     {
      datetime bt = iTime(_Symbol,PERIOD_CURRENT,0);
      if(bt!=lastAlert)
        {
         lastAlert=bt;
         Alert("T_Both_Signals: ",(dirNow>0?"BUY":"SELL")," setup on ",_Symbol," ",EnumToString(_Period));
        }
     }
   return rates_total;
  }
