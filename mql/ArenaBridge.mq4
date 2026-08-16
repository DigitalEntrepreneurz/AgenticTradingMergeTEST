//+------------------------------------------------------------------+
//|                                              ArenaBridge.mq4     |
//|   JSON file bridge between MetaTrader 4 and the Agentic Firm     |
//|                                                                  |
//|  INSTALL                                                         |
//|   1. MT4 -> File -> Open Data Folder -> MQL4 -> Experts          |
//|      copy this file there                                        |
//|   2. MetaEditor -> Compile (F7)                                  |
//|   3. Tools -> Options -> Expert Advisors ->                      |
//|      tick "Allow automated trading"                              |
//|   4. Drag ArenaBridge onto any chart, allow live trading         |
//|   5. Point config broker.bridge.files_dir at                     |
//|      <Data Folder>/MQL4/Files                                    |
//+------------------------------------------------------------------+
#property copyright "Agentic Trading Firm"
#property version   "1.10"
#property strict

extern int    PollMs       = 200;      // request scan interval (ms)
extern int    Magic        = 770420;   // magic number for firm trades
extern int    MaxBars      = 1500;
extern int    Slippage     = 20;
extern bool   VerboseLog   = true;
extern double MaxLotsGuard = 5.0;

string REQ_PREFIX = "request_";
string RSP_PREFIX = "response_";

//============================ tiny JSON helpers =====================
string JEsc(string s)
  {
   string r = "";
   for(int i = 0; i < StringLen(s); i++)
     {
      int c = StringGetChar(s, i);
      if(c == '"')       r = r + "\\\"";
      else if(c == '\\') r = r + "\\\\";
      else if(c == '\n') r = r + "\\n";
      else if(c == '\r') r = r + "";
      else if(c == '\t') r = r + "\\t";
      else               r = r + CharToStr(c);
     }
   return r;
  }

string JStr(string k, string v)  { return "\"" + k + "\":\"" + JEsc(v) + "\""; }
string JNum(string k, double v, int d = 8) { return "\"" + k + "\":" + DoubleToStr(v, d); }
string JInt(string k, long v)    { return "\"" + k + "\":" + IntegerToString(v); }
string JBool(string k, bool v)   { return "\"" + k + "\":" + (v ? "true" : "false"); }

string GetStr(string json, string key, string def = "")
  {
   string pat = "\"" + key + "\"";
   int p = StringFind(json, pat);
   if(p < 0) return def;
   p = StringFind(json, ":", p + StringLen(pat));
   if(p < 0) return def;
   int i = p + 1;
   while(i < StringLen(json))
     {
      int c = StringGetChar(json, i);
      if(c != ' ' && c != '\n' && c != '\r' && c != '\t') break;
      i++;
     }
   if(i >= StringLen(json)) return def;
   if(StringGetChar(json, i) == '"')
     {
      int e = i + 1; string outv = "";
      while(e < StringLen(json))
        {
         int c = StringGetChar(json, e);
         if(c == '\\' && e + 1 < StringLen(json))
           { outv = outv + CharToStr(StringGetChar(json, e + 1)); e += 2; continue; }
         if(c == '"') break;
         outv = outv + CharToStr(c); e++;
        }
      return outv;
     }
   int e2 = i;
   while(e2 < StringLen(json))
     {
      int c = StringGetChar(json, e2);
      if(c == ',' || c == '}' || c == ']') break;
      e2++;
     }
   string raw = StringSubstr(json, i, e2 - i);
   StringTrimLeft(raw); StringTrimRight(raw);
   return raw;
  }

double GetNum(string json, string key, double def = 0)
  {
   string s = GetStr(json, key, "");
   if(s == "" || s == "null") return def;
   return StrToDouble(s);
  }

//============================ file io ===============================
bool WriteTextFile(string name, string content)
  {
   int h = FileOpen(name, FILE_WRITE | FILE_TXT | FILE_ANSI);
   if(h == INVALID_HANDLE) { Print("write fail ", name, " err=", GetLastError()); return false; }
   FileWriteString(h, content);
   FileClose(h);
   return true;
  }

string ReadTextFile(string name)
  {
   int h = FileOpen(name, FILE_READ | FILE_TXT | FILE_ANSI | FILE_SHARE_READ | FILE_SHARE_WRITE);
   if(h == INVALID_HANDLE) return "";
   string s = "";
   while(!FileIsEnding(h)) s = s + FileReadString(h);
   FileClose(h);
   return s;
  }

int TFfromString(string tf)
  {
   StringToUpper(tf);
   if(tf == "M1")  return PERIOD_M1;   if(tf == "M5")  return PERIOD_M5;
   if(tf == "M15") return PERIOD_M15;  if(tf == "M30") return PERIOD_M30;
   if(tf == "H1")  return PERIOD_H1;   if(tf == "H4")  return PERIOD_H4;
   if(tf == "D1")  return PERIOD_D1;   if(tf == "W1")  return PERIOD_W1;
   return PERIOD_H1;
  }

//============================ payload builders ======================
string AccountJson()
  {
   return "{" + JInt("login", AccountNumber()) + ","
        + JStr("currency", AccountCurrency()) + ","
        + JNum("balance", AccountBalance(), 2) + ","
        + JNum("equity", AccountEquity(), 2) + ","
        + JNum("margin", AccountMargin(), 2) + ","
        + JNum("free_margin", AccountFreeMargin(), 2) + ","
        + JInt("leverage", AccountLeverage()) + ","
        + JStr("server", AccountServer()) + ","
        + JStr("company", AccountCompany()) + "}";
  }

string PositionsJson()
  {
   string arr = "";
   for(int i = 0; i < OrdersTotal(); i++)
     {
      if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES)) continue;
      if(Magic != 0 && OrderMagicNumber() != Magic) continue;
      if(OrderType() != OP_BUY && OrderType() != OP_SELL) continue;   // market only
      string side = (OrderType() == OP_BUY) ? "buy" : "sell";
      if(arr != "") arr = arr + ",";
      arr = arr + "{" + JStr("ticket", IntegerToString(OrderTicket())) + ","
          + JStr("symbol", OrderSymbol()) + ","
          + JStr("side", side) + ","
          + JNum("lots", OrderLots(), 2) + ","
          + JNum("entry", OrderOpenPrice()) + ","
          + JNum("stop", OrderStopLoss()) + ","
          + JNum("take", OrderTakeProfit()) + ","
          + JNum("profit", OrderProfit() + OrderSwap() + OrderCommission(), 2) + ","
          + JInt("open_time", OrderOpenTime()) + ","
          + JStr("comment", OrderComment()) + "}";
     }
   return "[" + arr + "]";
  }

string SymbolJson(string sym)
  {
   double point = MarketInfo(sym, MODE_POINT);
   if(point <= 0) return "";
   double ticksize = MarketInfo(sym, MODE_TICKSIZE);
   if(ticksize <= 0) ticksize = point;
   return "{" + JStr("symbol", sym) + ","
        + JInt("digits", (long)MarketInfo(sym, MODE_DIGITS)) + ","
        + JNum("point", point) + ","
        + JNum("contract_size", MarketInfo(sym, MODE_LOTSIZE), 2) + ","
        + JNum("tick_value", MarketInfo(sym, MODE_TICKVALUE), 6) + ","
        + JNum("tick_size", ticksize) + ","
        + JNum("volume_min", MarketInfo(sym, MODE_MINLOT), 2) + ","
        + JNum("volume_max", MarketInfo(sym, MODE_MAXLOT), 2) + ","
        + JNum("volume_step", MarketInfo(sym, MODE_LOTSTEP), 2) + ","
        + JInt("stops_level", (long)MarketInfo(sym, MODE_STOPLEVEL)) + "}";
  }

//============================ trading ===============================
string DoOrder(string p)
  {
   string sym  = GetStr(p, "symbol");
   string side = GetStr(p, "side");
   double lots = GetNum(p, "lots", 0.01);
   double sl   = GetNum(p, "stop", 0);
   double tp   = GetNum(p, "take", 0);
   string cmt  = GetStr(p, "comment", "agentic");

   if(MarketInfo(sym, MODE_POINT) <= 0)
      return "{" + JBool("ok", false) + "," + JStr("message", "unknown symbol") + "}";
   if(lots > MaxLotsGuard) lots = MaxLotsGuard;

   double vmin = MarketInfo(sym, MODE_MINLOT);
   double vmax = MarketInfo(sym, MODE_MAXLOT);
   double vstp = MarketInfo(sym, MODE_LOTSTEP);
   if(vstp > 0) lots = MathRound(lots / vstp) * vstp;
   lots = MathMax(vmin, MathMin(vmax, lots));

   int    digits = (int)MarketInfo(sym, MODE_DIGITS);
   int    cmd    = (side == "buy") ? OP_BUY : OP_SELL;
   double price  = (side == "buy") ? MarketInfo(sym, MODE_ASK) : MarketInfo(sym, MODE_BID);

   // MT4 rejects SL/TP inside the stop level - clamp defensively
   double stopLvl = MarketInfo(sym, MODE_STOPLEVEL) * MarketInfo(sym, MODE_POINT);
   if(sl > 0)
     {
      if(cmd == OP_BUY  && price - sl < stopLvl) sl = price - stopLvl;
      if(cmd == OP_SELL && sl - price < stopLvl) sl = price + stopLvl;
      sl = NormalizeDouble(sl, digits);
     }
   if(tp > 0)
     {
      if(cmd == OP_BUY  && tp - price < stopLvl) tp = price + stopLvl;
      if(cmd == OP_SELL && price - tp < stopLvl) tp = price - stopLvl;
      tp = NormalizeDouble(tp, digits);
     }

   int ticket = OrderSend(sym, cmd, lots, NormalizeDouble(price, digits), Slippage,
                          sl, tp, cmt, Magic, 0, clrNONE);
   if(ticket < 0)
     {
      int err = GetLastError();
      // some brokers refuse SL/TP at entry: open bare, then modify
      ticket = OrderSend(sym, cmd, lots, NormalizeDouble(price, digits), Slippage,
                         0, 0, cmt, Magic, 0, clrNONE);
      if(ticket >= 0 && (sl > 0 || tp > 0))
         if(OrderSelect(ticket, SELECT_BY_TICKET))
            OrderModify(ticket, OrderOpenPrice(), sl, tp, 0, clrNONE);
      if(ticket < 0)
         return "{" + JBool("ok", false) + "," + JInt("retcode", err) + ","
              + JStr("message", "OrderSend failed err=" + IntegerToString(err)) + "}";
     }
   double fill = price;
   if(OrderSelect(ticket, SELECT_BY_TICKET)) fill = OrderOpenPrice();
   return "{" + JBool("ok", true) + "," + JStr("ticket", IntegerToString(ticket)) + ","
        + JNum("price", fill) + "," + JStr("message", "filled") + "}";
  }

string DoClose(string p)
  {
   int ticket = (int)StrToInteger(GetStr(p, "ticket", "0"));
   if(!OrderSelect(ticket, SELECT_BY_TICKET))
      return "{" + JBool("ok", false) + "," + JStr("message", "position not found") + "}";
   string sym = OrderSymbol();
   double lots = OrderLots();
   int    digits = (int)MarketInfo(sym, MODE_DIGITS);
   double price = (OrderType() == OP_BUY) ? MarketInfo(sym, MODE_BID)
                                          : MarketInfo(sym, MODE_ASK);
   bool ok = OrderClose(ticket, lots, NormalizeDouble(price, digits), Slippage, clrNONE);
   int err = ok ? 0 : GetLastError();
   return "{" + JBool("ok", ok) + "," + JNum("price", price) + ","
        + JInt("retcode", err) + ","
        + JStr("message", ok ? "closed" : "close failed err=" + IntegerToString(err)) + "}";
  }

string DoModify(string p)
  {
   int ticket = (int)StrToInteger(GetStr(p, "ticket", "0"));
   if(!OrderSelect(ticket, SELECT_BY_TICKET))
      return "{" + JBool("ok", false) + "," + JStr("message", "position not found") + "}";
   int digits = (int)MarketInfo(OrderSymbol(), MODE_DIGITS);
   double sl = NormalizeDouble(GetNum(p, "stop", 0), digits);
   double tp = NormalizeDouble(GetNum(p, "take", 0), digits);
   bool ok = OrderModify(ticket, OrderOpenPrice(), sl, tp, 0, clrNONE);
   int err = ok ? 0 : GetLastError();
   return "{" + JBool("ok", ok) + "," + JInt("retcode", err) + ","
        + JStr("message", ok ? "modified" : "modify failed") + "}";
  }

string DoBars(string p)
  {
   string sym = GetStr(p, "symbol");
   string tfs = GetStr(p, "timeframe", "H1");
   int count  = (int)GetNum(p, "count", 300);
   if(count > MaxBars) count = MaxBars;
   int tf = TFfromString(tfs);
   int avail = iBars(sym, tf);
   if(avail <= 0) return "{\"bars\":[]}";
   if(count > avail) count = avail;
   int digits = (int)MarketInfo(sym, MODE_DIGITS);

   string arr = "";
   for(int i = count - 1; i >= 0; i--)     // oldest -> newest
     {
      if(arr != "") arr = arr + ",";
      arr = arr + "{\"t\":" + IntegerToString((long)iTime(sym, tf, i))
          + ",\"o\":" + DoubleToStr(iOpen(sym, tf, i), digits)
          + ",\"h\":" + DoubleToStr(iHigh(sym, tf, i), digits)
          + ",\"l\":" + DoubleToStr(iLow(sym, tf, i), digits)
          + ",\"c\":" + DoubleToStr(iClose(sym, tf, i), digits)
          + ",\"v\":" + IntegerToString((long)iVolume(sym, tf, i)) + "}";
     }
   return "{\"bars\":[" + arr + "]}";
  }

//============================ dispatch ==============================
void HandleRequest(string fname)
  {
   string body = ReadTextFile(fname);
   if(body == "") return;
   string id  = GetStr(body, "id");
   string cmd = GetStr(body, "cmd");
   if(id == "") { FileDelete(fname); return; }

   string data = ""; bool ok = true; string err = "";

   if(cmd == "ping")
      data = "{" + JStr("platform", "MT4") + ","
           + JStr("build", IntegerToString(TerminalInfoInteger(TERMINAL_BUILD))) + ","
           + JBool("algo_allowed", IsTradeAllowed()) + "}";
   else if(cmd == "account")   data = AccountJson();
   else if(cmd == "positions") data = "{\"positions\":" + PositionsJson() + "}";
   else if(cmd == "symbol")
     {
      data = SymbolJson(GetStr(body, "symbol"));
      if(data == "") { ok = false; err = "unknown symbol"; }
     }
   else if(cmd == "tick")
     {
      string sym = GetStr(body, "symbol");
      double bid = MarketInfo(sym, MODE_BID);
      double ask = MarketInfo(sym, MODE_ASK);
      if(bid <= 0) { ok = false; err = "no tick"; }
      else data = "{" + JNum("bid", bid) + "," + JNum("ask", ask) + ","
                + JInt("time", (long)TimeCurrent()) + "}";
     }
   else if(cmd == "bars")   data = DoBars(body);
   else if(cmd == "order")  data = DoOrder(body);
   else if(cmd == "close")  data = DoClose(body);
   else if(cmd == "modify") data = DoModify(body);
   else { ok = false; err = "unknown cmd " + cmd; }

   if(data == "" && ok) { ok = false; err = "empty result"; }

   string resp = "{" + JStr("id", id) + "," + JBool("ok", ok) + ","
               + "\"data\":" + (ok ? data : "{}") + "," + JStr("error", err) + "}";
   WriteTextFile(RSP_PREFIX + id + ".json", resp);
   FileDelete(fname);
   if(VerboseLog) Print("ArenaBridge ", cmd, " -> ", (ok ? "ok" : "ERR " + err));
  }

void WriteState()
  {
   string s = "{" + JNum("ts", (double)TimeCurrent(), 0) + ","
            + JStr("platform", "MT4") + ","
            + "\"account\":" + AccountJson() + ","
            + "\"positions\":" + PositionsJson() + ","
            + JBool("algo_allowed", IsTradeAllowed()) + "}";
   WriteTextFile("state.json", s);
  }

int OnInit()
  {
   EventSetMillisecondTimer(PollMs);
   Print("ArenaBridge MT4 started. Files dir: ", TerminalInfoString(TERMINAL_DATA_PATH),
         "\\MQL4\\Files  magic=", Magic);
   WriteState();
   return INIT_SUCCEEDED;
  }

void OnDeinit(const int reason) { EventKillTimer(); FileDelete("state.json"); }

void OnTimer()
  {
   string fname;
   long h = FileFindFirst(REQ_PREFIX + "*.json", fname);
   if(h != INVALID_HANDLE)
     {
      do { HandleRequest(fname); } while(FileFindNext(h, fname));
      FileFindClose(h);
     }
   static datetime last = 0;
   if(TimeCurrent() - last >= 2) { WriteState(); last = TimeCurrent(); }
  }

void OnTick() { }
