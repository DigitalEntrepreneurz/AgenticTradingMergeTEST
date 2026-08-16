//+------------------------------------------------------------------+
//|                                              ArenaBridge.mq5     |
//|   JSON file bridge between MetaTrader 5 and the Agentic Firm     |
//|                                                                  |
//|  INSTALL                                                         |
//|   1. MT5 -> File -> Open Data Folder -> MQL5 -> Experts          |
//|      copy this file there                                        |
//|   2. MetaEditor -> Compile (F7)                                  |
//|   3. Tools -> Options -> Expert Advisors ->                      |
//|      tick "Allow algorithmic trading"                            |
//|   4. Drag ArenaBridge onto any chart, allow algo trading         |
//|   5. Point config broker.bridge.files_dir at                     |
//|      <Data Folder>/MQL5/Files                                    |
//+------------------------------------------------------------------+
#property copyright "Agentic Trading Firm"
#property version   "1.10"
#property strict

input int    PollMs        = 200;      // request scan interval (ms)
input int    Magic         = 770420;   // magic number for firm trades
input int    MaxBars       = 1500;     // cap on bars per request
input bool   VerboseLog    = true;
input double MaxLotsGuard  = 5.0;      // hard lot ceiling, EA side

string REQ_PREFIX = "request_";
string RSP_PREFIX = "response_";

//============================ tiny JSON helpers =====================
string JEsc(string s)
  {
   string r = "";
   for(int i = 0; i < StringLen(s); i++)
     {
      ushort c = StringGetCharacter(s, i);
      if(c == '"')       r += "\\\"";
      else if(c == '\\') r += "\\\\";
      else if(c == '\n') r += "\\n";
      else if(c == '\r') r += "";
      else if(c == '\t') r += "\\t";
      else               r += ShortToString(c);
     }
   return r;
  }

string JStr(string k, string v)  { return "\"" + k + "\":\"" + JEsc(v) + "\""; }
string JNum(string k, double v, int d = 8) { return "\"" + k + "\":" + DoubleToString(v, d); }
string JInt(string k, long v)    { return "\"" + k + "\":" + IntegerToString(v); }
string JBool(string k, bool v)   { return "\"" + k + "\":" + (v ? "true" : "false"); }

//| naive extraction - the python side writes flat, predictable JSON  |
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
      ushort c = StringGetCharacter(json, i);
      if(c != ' ' && c != '\n' && c != '\r' && c != '\t') break;
      i++;
     }
   if(i >= StringLen(json)) return def;
   if(StringGetCharacter(json, i) == '"')
     {
      int e = i + 1; string out = "";
      while(e < StringLen(json))
        {
         ushort c = StringGetCharacter(json, e);
         if(c == '\\' && e + 1 < StringLen(json))
           { out += ShortToString(StringGetCharacter(json, e + 1)); e += 2; continue; }
         if(c == '"') break;
         out += ShortToString(c); e++;
        }
      return out;
     }
   int e2 = i;
   while(e2 < StringLen(json))
     {
      ushort c = StringGetCharacter(json, e2);
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
   return StringToDouble(s);
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
   while(!FileIsEnding(h)) s += FileReadString(h);
   FileClose(h);
   return s;
  }

//============================ timeframe =============================
ENUM_TIMEFRAMES TFfromString(string tf)
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
   return "{" + JInt("login", AccountInfoInteger(ACCOUNT_LOGIN)) + ","
        + JStr("currency", AccountInfoString(ACCOUNT_CURRENCY)) + ","
        + JNum("balance", AccountInfoDouble(ACCOUNT_BALANCE), 2) + ","
        + JNum("equity", AccountInfoDouble(ACCOUNT_EQUITY), 2) + ","
        + JNum("margin", AccountInfoDouble(ACCOUNT_MARGIN), 2) + ","
        + JNum("free_margin", AccountInfoDouble(ACCOUNT_MARGIN_FREE), 2) + ","
        + JInt("leverage", AccountInfoInteger(ACCOUNT_LEVERAGE)) + ","
        + JStr("server", AccountInfoString(ACCOUNT_SERVER)) + ","
        + JStr("company", AccountInfoString(ACCOUNT_COMPANY)) + "}";
  }

string PositionsJson()
  {
   string arr = "";
   int n = PositionsTotal();
   for(int i = 0; i < n; i++)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if(!PositionSelectByTicket(ticket)) continue;
      if(Magic != 0 && PositionGetInteger(POSITION_MAGIC) != Magic) continue;
      string side = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY) ? "buy" : "sell";
      if(arr != "") arr += ",";
      arr += "{" + JStr("ticket", IntegerToString((long)ticket)) + ","
           + JStr("symbol", PositionGetString(POSITION_SYMBOL)) + ","
           + JStr("side", side) + ","
           + JNum("lots", PositionGetDouble(POSITION_VOLUME), 2) + ","
           + JNum("entry", PositionGetDouble(POSITION_PRICE_OPEN)) + ","
           + JNum("stop", PositionGetDouble(POSITION_SL)) + ","
           + JNum("take", PositionGetDouble(POSITION_TP)) + ","
           + JNum("profit", PositionGetDouble(POSITION_PROFIT), 2) + ","
           + JInt("open_time", PositionGetInteger(POSITION_TIME)) + ","
           + JStr("comment", PositionGetString(POSITION_COMMENT)) + "}";
     }
   return "[" + arr + "]";
  }

string SymbolJson(string sym)
  {
   if(!SymbolSelect(sym, true)) return "";
   return "{" + JStr("symbol", sym) + ","
        + JInt("digits", (long)SymbolInfoInteger(sym, SYMBOL_DIGITS)) + ","
        + JNum("point", SymbolInfoDouble(sym, SYMBOL_POINT)) + ","
        + JNum("contract_size", SymbolInfoDouble(sym, SYMBOL_TRADE_CONTRACT_SIZE), 2) + ","
        + JNum("tick_value", SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_VALUE), 6) + ","
        + JNum("tick_size", SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_SIZE)) + ","
        + JNum("volume_min", SymbolInfoDouble(sym, SYMBOL_VOLUME_MIN), 2) + ","
        + JNum("volume_max", SymbolInfoDouble(sym, SYMBOL_VOLUME_MAX), 2) + ","
        + JNum("volume_step", SymbolInfoDouble(sym, SYMBOL_VOLUME_STEP), 2) + ","
        + JInt("stops_level", (long)SymbolInfoInteger(sym, SYMBOL_TRADE_STOPS_LEVEL)) + "}";
  }

//============================ trading ===============================
string DoOrder(string p)
  {
   string sym  = GetStr(p, "symbol");
   string side = GetStr(p, "side");
   double lots = GetNum(p, "lots", 0.01);
   double sl   = GetNum(p, "stop", 0);
   double tp   = GetNum(p, "take", 0);
   int    dev  = (int)GetNum(p, "deviation", 20);
   string cmt  = GetStr(p, "comment", "agentic");

   if(!SymbolSelect(sym, true)) return "";
   if(lots > MaxLotsGuard) lots = MaxLotsGuard;

   double vmin = SymbolInfoDouble(sym, SYMBOL_VOLUME_MIN);
   double vmax = SymbolInfoDouble(sym, SYMBOL_VOLUME_MAX);
   double vstp = SymbolInfoDouble(sym, SYMBOL_VOLUME_STEP);
   if(vstp > 0) lots = MathRound(lots / vstp) * vstp;
   lots = MathMax(vmin, MathMin(vmax, lots));

   int    digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
   double price  = (side == "buy") ? SymbolInfoDouble(sym, SYMBOL_ASK)
                                   : SymbolInfoDouble(sym, SYMBOL_BID);

   MqlTradeRequest req; ZeroMemory(req);
   MqlTradeResult  res; ZeroMemory(res);
   req.action       = TRADE_ACTION_DEAL;
   req.symbol       = sym;
   req.volume       = lots;
   req.type         = (side == "buy") ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   req.price        = price;
   req.deviation    = dev;
   req.magic        = Magic;
   req.comment      = cmt;
   req.type_time    = ORDER_TIME_GTC;
   req.type_filling = ORDER_FILLING_IOC;
   if(sl > 0) req.sl = NormalizeDouble(sl, digits);
   if(tp > 0) req.tp = NormalizeDouble(tp, digits);

   bool sent = OrderSend(req, res);
   if(!sent || res.retcode != TRADE_RETCODE_DONE)
     {
      req.type_filling = ORDER_FILLING_FOK;      // retry other filling mode
      ZeroMemory(res);
      sent = OrderSend(req, res);
     }
   bool ok = (sent && res.retcode == TRADE_RETCODE_DONE);
   return "{" + JBool("ok", ok) + ","
        + JStr("ticket", IntegerToString((long)res.order)) + ","
        + JNum("price", res.price) + ","
        + JInt("retcode", res.retcode) + ","
        + JStr("message", res.comment) + "}";
  }

string DoClose(string p)
  {
   ulong ticket = (ulong)StringToInteger(GetStr(p, "ticket", "0"));
   if(!PositionSelectByTicket(ticket))
      return "{" + JBool("ok", false) + "," + JStr("message", "position not found") + "}";

   string sym   = PositionGetString(POSITION_SYMBOL);
   double lots  = PositionGetDouble(POSITION_VOLUME);
   bool   isBuy = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY);

   MqlTradeRequest req; ZeroMemory(req);
   MqlTradeResult  res; ZeroMemory(res);
   req.action       = TRADE_ACTION_DEAL;
   req.symbol       = sym;
   req.volume       = lots;
   req.type         = isBuy ? ORDER_TYPE_SELL : ORDER_TYPE_BUY;
   req.position     = ticket;
   req.price        = isBuy ? SymbolInfoDouble(sym, SYMBOL_BID)
                            : SymbolInfoDouble(sym, SYMBOL_ASK);
   req.deviation    = 20;
   req.magic        = Magic;
   req.comment      = "agentic close";
   req.type_time    = ORDER_TIME_GTC;
   req.type_filling = ORDER_FILLING_IOC;

   bool sent = OrderSend(req, res);
   if(!sent || res.retcode != TRADE_RETCODE_DONE)
     { req.type_filling = ORDER_FILLING_FOK; ZeroMemory(res); sent = OrderSend(req, res); }
   bool ok = (sent && res.retcode == TRADE_RETCODE_DONE);
   return "{" + JBool("ok", ok) + "," + JNum("price", res.price) + ","
        + JInt("retcode", res.retcode) + "," + JStr("message", res.comment) + "}";
  }

string DoModify(string p)
  {
   ulong ticket = (ulong)StringToInteger(GetStr(p, "ticket", "0"));
   if(!PositionSelectByTicket(ticket))
      return "{" + JBool("ok", false) + "," + JStr("message", "position not found") + "}";
   string sym = PositionGetString(POSITION_SYMBOL);
   int digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);

   MqlTradeRequest req; ZeroMemory(req);
   MqlTradeResult  res; ZeroMemory(res);
   req.action   = TRADE_ACTION_SLTP;
   req.symbol   = sym;
   req.position = ticket;
   req.sl       = NormalizeDouble(GetNum(p, "stop", 0), digits);
   req.tp       = NormalizeDouble(GetNum(p, "take", 0), digits);
   req.magic    = Magic;

   bool sent = OrderSend(req, res);
   bool ok = (sent && res.retcode == TRADE_RETCODE_DONE);
   return "{" + JBool("ok", ok) + "," + JInt("retcode", res.retcode) + ","
        + JStr("message", res.comment) + "}";
  }

string DoBars(string p)
  {
   string sym = GetStr(p, "symbol");
   string tfs = GetStr(p, "timeframe", "H1");
   int count  = (int)GetNum(p, "count", 300);
   if(count > MaxBars) count = MaxBars;
   if(!SymbolSelect(sym, true)) return "";

   MqlRates rates[];
   ArraySetAsSeries(rates, false);
   int got = CopyRates(sym, TFfromString(tfs), 0, count, rates);
   if(got <= 0) return "{\"bars\":[]}";

   int digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
   string arr = "";
   for(int i = 0; i < got; i++)
     {
      if(arr != "") arr += ",";
      arr += "{\"t\":" + IntegerToString((long)rates[i].time)
           + ",\"o\":" + DoubleToString(rates[i].open, digits)
           + ",\"h\":" + DoubleToString(rates[i].high, digits)
           + ",\"l\":" + DoubleToString(rates[i].low, digits)
           + ",\"c\":" + DoubleToString(rates[i].close, digits)
           + ",\"v\":" + IntegerToString((long)rates[i].tick_volume) + "}";
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

   string data = "";
   bool   ok   = true;
   string err  = "";

   if(cmd == "ping")
      data = "{" + JStr("platform", "MT5") + "," + JStr("build", IntegerToString(
             (long)TerminalInfoInteger(TERMINAL_BUILD))) + ","
           + JBool("algo_allowed", (bool)TerminalInfoInteger(TERMINAL_TRADE_ALLOWED)) + "}";
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
      if(!SymbolSelect(sym, true)) { ok = false; err = "unknown symbol"; }
      else
        {
         MqlTick t;
         if(SymbolInfoTick(sym, t))
            data = "{" + JNum("bid", t.bid) + "," + JNum("ask", t.ask) + ","
                 + JInt("time", (long)t.time) + "}";
         else { ok = false; err = "no tick"; }
        }
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
            + JStr("platform", "MT5") + ","
            + "\"account\":" + AccountJson() + ","
            + "\"positions\":" + PositionsJson() + ","
            + JBool("algo_allowed", (bool)TerminalInfoInteger(TERMINAL_TRADE_ALLOWED)) + "}";
   WriteTextFile("state.json", s);
  }

//============================ lifecycle =============================
int OnInit()
  {
   EventSetMillisecondTimer(PollMs);
   Print("ArenaBridge MT5 started. Files dir: ", TerminalInfoString(TERMINAL_DATA_PATH),
         "\\MQL5\\Files  magic=", Magic);
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
