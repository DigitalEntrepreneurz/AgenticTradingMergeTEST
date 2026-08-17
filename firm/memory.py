"""Durable firm memory: issues, agent messages, research, backtests, trades, costs.

Everything the agents learn is written here so the firm never repeats work
and gets better over time. SQLite = zero setup, survives restarts.
"""
from __future__ import annotations

import calendar
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "firm" / "data" / "firm.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL, updated_at REAL,
    title TEXT, body TEXT,
    assignee TEXT,          -- agent name
    author TEXT,            -- 'board' or agent name
    status TEXT,            -- open | in_progress | done | blocked
    parent_id INTEGER,
    result TEXT
);
CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id INTEGER, created_at REAL, author TEXT, body TEXT
);
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL, agent TEXT, kind TEXT, key TEXT,
    content TEXT, meta TEXT
);
CREATE TABLE IF NOT EXISTS strategies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL, updated_at REAL,
    name TEXT UNIQUE, spec TEXT, source TEXT,
    status TEXT,                -- proposed | backtested | approved | rejected | live
    score REAL, metrics TEXT, notes TEXT
);
CREATE TABLE IF NOT EXISTS backtests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL, strategy TEXT, symbol TEXT, timeframe TEXT,
    metrics TEXT, passed INTEGER, notes TEXT
);
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL, strategy TEXT, symbol TEXT, side TEXT,
    entry REAL, stop REAL, take REAL, confidence REAL,
    rationale TEXT, status TEXT, risk_note TEXT
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL, closed_at REAL,
    account TEXT, platform TEXT, ticket TEXT,
    symbol TEXT, side TEXT, lots REAL,
    entry REAL, stop REAL, take REAL, exit_price REAL,
    pnl REAL, status TEXT, mode TEXT, signal_id INTEGER, meta TEXT
);
CREATE TABLE IF NOT EXISTS costs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL, agent TEXT, model TEXT,
    input_tokens INTEGER, output_tokens INTEGER, usd REAL, note TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL, level TEXT, agent TEXT, message TEXT, meta TEXT
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE INDEX IF NOT EXISTS idx_issue_status ON issues(status);
CREATE INDEX IF NOT EXISTS idx_mem_agent ON memories(agent, kind);
CREATE INDEX IF NOT EXISTS idx_trade_status ON trades(status);
"""


@dataclass
class Memory:
    path: Path = DB_PATH

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # ---------------- low level ----------------
    def q(self, sql: str, args: Iterable[Any] = ()) -> list[dict]:
        cur = self._conn.execute(sql, tuple(args))
        return [dict(r) for r in cur.fetchall()]

    def x(self, sql: str, args: Iterable[Any] = ()) -> int:
        cur = self._conn.execute(sql, tuple(args))
        self._conn.commit()
        return int(cur.lastrowid or 0)

    # ---------------- issues (the CEO's task board) ----------------
    def create_issue(self, title: str, body: str, assignee: str,
                     author: str = "board", parent_id: int | None = None) -> int:
        now = time.time()
        iid = self.x(
            "INSERT INTO issues(created_at,updated_at,title,body,assignee,author,status,parent_id)"
            " VALUES(?,?,?,?,?,?,'open',?)",
            (now, now, title, body, assignee, author, parent_id),
        )
        self.log("info", author, f"issue #{iid} -> {assignee}: {title}")
        return iid

    def open_issues(self, assignee: str | None = None) -> list[dict]:
        if assignee:
            return self.q("SELECT * FROM issues WHERE status IN ('open','in_progress')"
                          " AND assignee=? ORDER BY id", (assignee,))
        return self.q("SELECT * FROM issues WHERE status IN ('open','in_progress') ORDER BY id")

    def set_issue_status(self, issue_id: int, status: str, result: str | None = None) -> None:
        self.x("UPDATE issues SET status=?, updated_at=?, result=COALESCE(?,result) WHERE id=?",
               (status, time.time(), result, issue_id))

    def comment(self, issue_id: int, author: str, body: str) -> None:
        self.x("INSERT INTO comments(issue_id,created_at,author,body) VALUES(?,?,?,?)",
               (issue_id, time.time(), author, body))

    def issue(self, issue_id: int) -> dict | None:
        rows = self.q("SELECT * FROM issues WHERE id=?", (issue_id,))
        return rows[0] if rows else None

    def issue_comments(self, issue_id: int) -> list[dict]:
        return self.q("SELECT * FROM comments WHERE issue_id=? ORDER BY id", (issue_id,))

    # ---------------- memories ----------------
    def remember(self, agent: str, kind: str, key: str, content: str,
                 meta: dict | None = None) -> int:
        return self.x(
            "INSERT INTO memories(created_at,agent,kind,key,content,meta) VALUES(?,?,?,?,?,?)",
            (time.time(), agent, kind, key, content, json.dumps(meta or {})),
        )

    def recall(self, agent: str | None = None, kind: str | None = None,
               limit: int = 20) -> list[dict]:
        sql, args = "SELECT * FROM memories WHERE 1=1", []
        if agent:
            sql += " AND agent=?"; args.append(agent)
        if kind:
            sql += " AND kind=?"; args.append(kind)
        sql += " ORDER BY id DESC LIMIT ?"; args.append(limit)
        return self.q(sql, args)

    def has_seen(self, kind: str, key: str) -> bool:
        return bool(self.q("SELECT 1 FROM memories WHERE kind=? AND key=? LIMIT 1", (kind, key)))

    # ---------------- strategies ----------------
    def upsert_strategy(self, name: str, spec: dict, source: str, status: str = "proposed",
                        score: float = 0.0, metrics: dict | None = None,
                        notes: str = "") -> None:
        now = time.time()
        existing = self.q("SELECT id FROM strategies WHERE name=?", (name,))
        if existing:
            self.x("UPDATE strategies SET updated_at=?,spec=?,status=?,score=?,metrics=?,notes=?"
                   " WHERE name=?",
                   (now, json.dumps(spec), status, score, json.dumps(metrics or {}), notes, name))
        else:
            self.x("INSERT INTO strategies(created_at,updated_at,name,spec,source,status,score,"
                   "metrics,notes) VALUES(?,?,?,?,?,?,?,?,?)",
                   (now, now, name, json.dumps(spec), source, status, score,
                    json.dumps(metrics or {}), notes))

    def strategies(self, status: str | None = None) -> list[dict]:
        rows = (self.q("SELECT * FROM strategies WHERE status=? ORDER BY score DESC", (status,))
                if status else self.q("SELECT * FROM strategies ORDER BY score DESC"))
        for r in rows:
            r["spec"] = json.loads(r["spec"] or "{}")
            r["metrics"] = json.loads(r["metrics"] or "{}")
        return rows

    # ---------------- signals & trades ----------------
    def add_signal(self, **kw: Any) -> int:
        return self.x(
            "INSERT INTO signals(created_at,strategy,symbol,side,entry,stop,take,confidence,"
            "rationale,status,risk_note) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), kw.get("strategy"), kw.get("symbol"), kw.get("side"),
             kw.get("entry"), kw.get("stop"), kw.get("take"), kw.get("confidence", 0.5),
             kw.get("rationale", ""), kw.get("status", "pending"), kw.get("risk_note", "")),
        )

    def pending_signals(self) -> list[dict]:
        return self.q("SELECT * FROM signals WHERE status='pending' ORDER BY id")

    def set_signal_status(self, sid: int, status: str, risk_note: str = "") -> None:
        self.x("UPDATE signals SET status=?, risk_note=? WHERE id=?", (status, risk_note, sid))

    def add_trade(self, **kw: Any) -> int:
        return self.x(
            "INSERT INTO trades(created_at,account,platform,ticket,symbol,side,lots,entry,stop,"
            "take,status,mode,signal_id,meta) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), kw.get("account"), kw.get("platform"), str(kw.get("ticket")),
             kw.get("symbol"), kw.get("side"), kw.get("lots"), kw.get("entry"),
             kw.get("stop"), kw.get("take"), kw.get("status", "open"),
             kw.get("mode", "paper"), kw.get("signal_id"), json.dumps(kw.get("meta", {}))),
        )

    def close_trade(self, trade_id: int, exit_price: float, pnl: float) -> None:
        self.x("UPDATE trades SET closed_at=?, exit_price=?, pnl=?, status='closed' WHERE id=?",
               (time.time(), exit_price, pnl, trade_id))

    def open_trades(self) -> list[dict]:
        return self.q("SELECT * FROM trades WHERE status='open' ORDER BY id")

    def closed_trades(self, limit: int = 200) -> list[dict]:
        return self.q("SELECT * FROM trades WHERE status='closed' ORDER BY id DESC LIMIT ?",
                      (limit,))

    def all_trades(self, limit: int = 5000) -> list[dict]:
        """Every trade with meta flattened to meta_* keys, for analytics."""
        rows = self.q("SELECT * FROM trades ORDER BY id LIMIT ?", (limit,))
        for r in rows:
            try:
                meta = json.loads(r.get("meta") or "{}")
            except (json.JSONDecodeError, TypeError):
                meta = {}
            for k, v in meta.items():
                r[f"meta_{k}"] = v
            r.setdefault("meta_strategy", "unknown")
        return rows

    # ---------------- costs / events ----------------
    def add_cost(self, agent: str, model: str, itok: int, otok: int, usd: float,
                 note: str = "") -> None:
        self.x("INSERT INTO costs(created_at,agent,model,input_tokens,output_tokens,usd,note)"
               " VALUES(?,?,?,?,?,?,?)", (time.time(), agent, model, itok, otok, usd, note))

    def cost_today(self) -> float:
        cutoff = time.time() - 86400
        r = self.q("SELECT COALESCE(SUM(usd),0) s FROM costs WHERE created_at>?", (cutoff,))
        return float(r[0]["s"])

    def tokens_today(self) -> int:
        """Total tokens in the last 24h - the real unit on a free/quota plan."""
        cutoff = time.time() - 86400
        r = self.q("SELECT COALESCE(SUM(input_tokens+output_tokens),0) t FROM costs"
                   " WHERE created_at>?", (cutoff,))
        return int(r[0]["t"])

    def tokens_this_month(self) -> int:
        """Tokens since the start of the current calendar month (UTC).

        Provider quotas reset on the calendar month, not a rolling window, so
        this deliberately does not use `now - 30 days`.
        """
        now = time.gmtime()
        start = calendar.timegm((now.tm_year, now.tm_mon, 1, 0, 0, 0, 0, 0, 0))
        r = self.q("SELECT COALESCE(SUM(input_tokens+output_tokens),0) t FROM costs"
                   " WHERE created_at>=?", (start,))
        return int(r[0]["t"])

    def token_usage(self) -> dict:
        """Token counters for the dashboard and the cost agent."""
        return {"today": self.tokens_today(), "month": self.tokens_this_month(),
                "usd_today": self.cost_today()}

    def cost_by_agent(self) -> list[dict]:
        cutoff = time.time() - 86400
        return self.q("SELECT agent, COALESCE(SUM(usd),0) usd, COUNT(*) calls FROM costs"
                      " WHERE created_at>? GROUP BY agent ORDER BY usd DESC", (cutoff,))

    def log(self, level: str, agent: str, message: str, meta: dict | None = None) -> None:
        self.x("INSERT INTO events(created_at,level,agent,message,meta) VALUES(?,?,?,?,?)",
               (time.time(), level, agent, message, json.dumps(meta or {})))

    def events(self, limit: int = 100) -> list[dict]:
        return self.q("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))

    # ---------------- kv ----------------
    def put(self, k: str, v: Any) -> None:
        self.x("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
               (k, json.dumps(v)))

    def get(self, k: str, default: Any = None) -> Any:
        r = self.q("SELECT v FROM kv WHERE k=?", (k,))
        return json.loads(r[0]["v"]) if r else default
