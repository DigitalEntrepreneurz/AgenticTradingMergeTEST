//+------------------------------------------------------------------+
//| Bollinger_Band_Mean_Reversion_EURUSD_H1_Signals.mq5  --  SIGNAL INDICATOR (does not trade)
//| Compiled from a strategy spec by the Agentic Trading Firm
//| 2026-08-16 19:02 UTC
//|
//| Strategy   : Bollinger Band Mean Reversion
//| Symbol/TF  : EURUSD H1
//| Entry rules: bb_touch, rsi_extreme
//| Filters    : none
//| Agreement  : 0.5
//|
//| Fade closes outside the bands when RSI confirms an extreme.
//|
//| BACKTEST (in-sample, spread-adjusted)
//|   trades 20 | win 75% | PF 7.409
//|   expectancy 1.6115R | robustness score 2.6059
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

input double Agreement   = 0.5;   // fraction of entry rules that must agree
input int    ATRPeriod   = 14;
input double ATRMultiplier = 3.0;  // stop distance shown on the panel
input double RewardRatio = 2.5;          // target = R x stop
input bool   ShowPanel   = true;          // live rule breakdown, top-left
input bool   AlertOnSignal = false;       // popup alert on a fresh signal

#define N_ENTRY 2
#define N_FILTER 1

double BufUp[];
double BufDn[];

int hAtr;
int hRsi14;
int hBb_20_2p0;
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
      // no filters
   }

   for(int i=0;i<0;i++)
      if(fv[i]==-99) return 0;

   {
      // entry rule 1: bb_touch
   {
    double up=Buf(hBb_20_2p0,1,sh), lo=Buf(hBb_20_2p0,2,sh);
    double px=Cl(sh);
    if(up<=0||lo<=0)   ev[0]=0;
    else if(px<lo)     ev[0]=1;
    else if(px>up)     ev[0]=-1;
    else               ev[0]=0;
   }
      // entry rule 2: rsi_extreme
   {
    double r=Buf(hRsi14,0,sh);
    if(r<=0)        ev[1]=0;
    else if(r<30.0) ev[1]=1;
    else if(r>70.0)  ev[1]=-1;
    else            ev[1]=0;
   }
   }

   int bulls=0, bears=0, total=2;
   for(int i=0;i<2;i++)
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
   string nm="Bollinger_Band_Mean_Reversion_EURUSD_H1_Signals_panel";
   string verdict = (dir>0 ? "BUY setup" : (dir<0 ? "SELL setup" : "no setup"));
   color  col     = (dir>0 ? clrLimeGreen : (dir<0 ? clrOrangeRed : clrSilver));
   string txt = "Bollinger Band Mean Reversion"
              + "\n" + verdict
              + "\nvotes  " + IntegerToString(gBulls) + " bull / "
              + IntegerToString(gBears) + " bear  of 2"
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
   hRsi14=iRSI(_Symbol,PERIOD_CURRENT,14,PRICE_CLOSE);
   hBb_20_2p0=iBands(_Symbol,PERIOD_CURRENT,20,0,2.0,PRICE_CLOSE);
   if(hAtr==INVALID_HANDLE) return INIT_FAILED;
   IndicatorSetString(INDICATOR_SHORTNAME,"Bollinger_Band_Mean_Reversion_EURUSD_H1_Signals");
   return INIT_SUCCEEDED;
  }

void OnDeinit(const int reason) { ObjectDelete(0,"Bollinger_Band_Mean_Reversion_EURUSD_H1_Signals_panel"); }

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
         Alert("Bollinger_Band_Mean_Reversion_EURUSD_H1_Signals: ",(dirNow>0?"BUY":"SELL")," setup on ",_Symbol," ",EnumToString(_Period));
        }
     }
   return rates_total;
  }
