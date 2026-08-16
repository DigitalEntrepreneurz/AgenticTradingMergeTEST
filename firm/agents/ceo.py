"""CEO agent - the single point of contact for the board (you).

You raise an issue for the CEO. The CEO decides which departments to involve,
creates sub-issues for them, collects their results and reports back. It never
places a trade itself and cannot override the risk envelope.
"""
from __future__ import annotations

import json
import time

from .base import Agent

SYSTEM = """You are the CEO of an autonomous trading firm. You never trade
yourself: you delegate to six departments - scout, research, backtest, risk,
execution, cost_optimizer.

Rules you cannot break:
- Nothing reaches execution unless the backtest department approved the
  strategy and the risk department sized it.
- You cannot loosen risk limits; only the human board can, in config.
- Prefer the fewest delegations that accomplish the request (cost discipline).

Reply with STRICT JSON only:
{"plan":"one paragraph","delegations":[{"agent":"research","title":"","body":""}],
 "reply_to_board":"what you would tell the board now"}"""

DEPARTMENTS = ["scout", "research", "backtest", "risk", "execution",
               "cost_optimizer"]

# Keyword routing used when no LLM key is configured
ROUTES = [
    (("scan", "scout", "trending", "discover", "popular", "youtube", "autoscan",
      "auto-scan", "sweep the internet"),
     "scout", "Auto-scan trending strategies",
     "Run the auto-scan suite: discover trending forex strategies, backtest and "
     "walk-forward each one, and file only the survivors as proposals."),
    (("research", "idea", "regime", "analy", "market", "news", "strateg", "find"),
     "research", "Research cycle",
     "Analyse the current regime for all configured symbols and propose "
     "strategies from the library. Skip anything already proposed."),
    (("backtest", "validate", "test", "historical", "walk"),
     "backtest", "Validate proposals",
     "Backtest and walk-forward validate every proposed strategy. Approve only "
     "what survives out-of-sample."),
    (("risk", "exposure", "drawdown", "limit", "safe"),
     "risk", "Risk review",
     "Produce a full risk report: equity, day P&L, drawdown, open exposure and "
     "kill-switch state."),
    (("execut", "trade", "order", "position", "buy", "sell", "deploy"),
     "execution", "Execution cycle",
     "Scan approved strategies for signals, obtain risk approval and route any "
     "approved orders to the terminal."),
    (("cost", "spend", "budget", "cheap", "token"),
     "cost_optimizer", "Cost review",
     "Report LLM spend per agent and recommend savings."),
]


class CEOAgent(Agent):
    name = "ceo"
    title = "Chief Executive Officer"
    charter = ("Single interface to the board. Translate board intent into "
               "departmental assignments, synthesise results, report back.")

    # ---------------- planning ----------------
    def _heuristic_plan(self, text: str) -> list[dict]:
        t = text.lower()
        picked: list[dict] = []
        for keys, agent, title, body in ROUTES:
            if any(k in t for k in keys):
                picked.append({"agent": agent, "title": title, "body": body})
        if not picked:
            # default full cycle, in dependency order
            picked = [
                {"agent": "research", "title": "Research cycle",
                 "body": ROUTES[1][3]},
                {"agent": "backtest", "title": "Validate proposals",
                 "body": ROUTES[2][3]},
                {"agent": "risk", "title": "Risk review", "body": ROUTES[3][3]},
                {"agent": "execution", "title": "Execution cycle",
                 "body": ROUTES[4][3]},
            ]
        order = {a: i for i, a in enumerate(DEPARTMENTS)}
        picked.sort(key=lambda d: order.get(d["agent"], 9))
        return picked

    def _firm_state(self) -> dict:
        strat = self.mem.strategies()
        return {
            "mode": "LIVE" if self.cfg.live_enabled else "PAPER",
            "symbols": self.cfg.symbols,
            "timeframe": self.cfg.timeframe,
            "accounts": [{"id": bid, "platform": getattr(b, "platform", "SIM")}
                         for bid, b in self.ctx.brokers.items()],
            "strategies": {"proposed": sum(s["status"] == "proposed" for s in strat),
                           "approved": sum(s["status"] == "approved" for s in strat),
                           "rejected": sum(s["status"] == "rejected" for s in strat)},
            "open_trades": len(self.mem.open_trades()),
            "llm_spend_24h": round(self.mem.cost_today(), 4),
            "recent_work": [m["content"][:160] for m in self.mem.recall(kind="cycle", limit=3)],
        }

    # ---------------- board issues ----------------
    def handle(self, issue: dict) -> str:
        """Handle a board request by delegating; children are worked next tick."""
        text = f"{issue['title']}\n{issue['body']}"
        plan_note = "keyword routing (no LLM key configured)"
        delegations = self._heuristic_plan(text)
        reply = ""

        if self.llm.available and self.within_budget():
            r = self.think(self.system_prompt(SYSTEM), (
                f"{self.firm_context()}\n\n"
                f"Firm state:\n{json.dumps(self._firm_state(), indent=2)}\n\n"
                f"Board request:\nTitle: {issue['title']}\nBody: {issue['body']}\n\n"
                f"Departments available: {', '.join(DEPARTMENTS)}. "
                f"Delegate only what is needed."), max_tokens=1200)
            data = r.json()
            if isinstance(data, dict) and isinstance(data.get("delegations"), list):
                valid = [d for d in data["delegations"] if d.get("agent") in DEPARTMENTS]
                if valid:
                    delegations = valid
                    plan_note = f"CEO plan via {r.model} (${r.usd:.4f})"
                    reply = data.get("reply_to_board", "") or data.get("plan", "")
            elif r.error:
                plan_note = f"keyword routing (LLM unavailable: {r.error})"

        # Departments run in DEPENDENCY ORDER, one at a time: research must finish
        # before backtest can validate it, and backtest must approve before
        # execution may trade it. Firing them in parallel would let execution run
        # against stale (or empty) approvals.
        order = {a: i for i, a in enumerate(DEPARTMENTS)}
        delegations.sort(key=lambda d: order.get(d["agent"], 9))
        self.mem.put(f"plan:{issue['id']}", delegations[1:])

        first = delegations[0]
        cid = self.mem.create_issue(
            title=first.get("title", f"{first['agent']} assignment")[:120],
            body=first.get("body", text), assignee=first["agent"],
            author="ceo", parent_id=issue["id"])

        queue = " -> ".join(d["agent"] for d in delegations)
        self.mem.put("last_board_issue", issue["id"])
        summary = (f"Plan [{plan_note}]: {queue}\n"
                   f"Delegating in dependency order, one department at a time.\n"
                   f"  #{cid} -> {first['agent']}: {first.get('title', '')} (started)\n"
                   f"  queued: {', '.join(d['agent'] for d in delegations[1:]) or 'none'}"
                   + (f"\n\nCEO note: {reply}" if reply else "") +
                   "\n\nI will report back here as each department completes.")
        # stays in_progress until children finish
        self.mem.comment(issue["id"], "ceo", summary)
        self.mem.set_issue_status(issue["id"], "in_progress")
        return summary

    def work(self) -> int:
        """Override: delegate new board issues, then close finished ones."""
        if not self.enabled:
            return 0
        n = 0
        for issue in self.mem.open_issues("ceo"):
            if issue["status"] == "open":
                try:
                    self.handle(issue)
                    n += 1
                except Exception as e:
                    self.mem.comment(issue["id"], "ceo", f"FAILED: {e}")
                    self.mem.set_issue_status(issue["id"], "blocked", str(e)[:500])
        n += self.collect()
        return n

    def collect(self) -> int:
        """Advance each board issue's plan; close it when the pipeline is done."""
        closed = 0
        for issue in self.mem.open_issues("ceo"):
            kids = self.mem.q("SELECT * FROM issues WHERE parent_id=?", (issue["id"],))
            if not kids:
                continue
            if any(k["status"] in ("open", "in_progress") for k in kids):
                continue

            # current step finished - release the next department in the plan
            remaining = self.mem.get(f"plan:{issue['id']}") or []
            if remaining:
                nxt = remaining[0]
                self.mem.put(f"plan:{issue['id']}", remaining[1:])
                cid = self.mem.create_issue(
                    title=nxt.get("title", f"{nxt['agent']} assignment")[:120],
                    body=nxt.get("body", issue["body"]), assignee=nxt["agent"],
                    author="ceo", parent_id=issue["id"])
                self.log(f"pipeline #{issue['id']}: handing off to {nxt['agent']} (#{cid})")
                continue
            parts = [f"### {k['assignee']} ({k['status']})\n{(k['result'] or '').strip()}"
                     for k in kids]
            body = "\n\n".join(parts)

            digest = body
            if self.llm.available and self.within_budget():
                r = self.think(
                    self.system_prompt(
                    "You are the CEO reporting to the board. Summarise the department "
                    "reports into a crisp board update: what was done, what it means, "
                    "what happens next, any risk flags. Plain text, under 250 words. "
                    "Never promise returns."),
                    f"{self.firm_context()}\n\nBoard asked: {issue['title']}\n"
                    f"{issue['body']}\n\nDepartment reports:\n{body[:6000]}",
                    max_tokens=700)
                if r.text.strip():
                    digest = r.text.strip() + "\n\n--- department detail ---\n" + body

            self.mem.comment(issue["id"], "ceo", digest)
            self.mem.set_issue_status(issue["id"], "done", digest[:8000])
            self.mem.remember("ceo", "board_report", f"issue:{issue['id']}", digest[:4000])
            self.log(f"board issue #{issue['id']} complete")
            closed += 1
        return closed

    # ---------------- autonomous cadence ----------------
    def autonomous_cycle(self, kind: str) -> int:
        """Scheduler asks the CEO to run a standing cycle."""
        titles = {
            "scout": ("Scheduled auto-scan", ROUTES[0][3], "scout"),
            "research": ("Scheduled research cycle", ROUTES[1][3], "research"),
            "backtest": ("Scheduled validation cycle", ROUTES[2][3], "backtest"),
            "risk": ("Scheduled risk review", ROUTES[3][3], "risk"),
            "execution": ("Scheduled execution cycle", ROUTES[4][3], "execution"),
            "cost": ("Scheduled cost review", ROUTES[5][3], "cost_optimizer"),
        }
        if kind not in titles:
            return 0
        title, body, agent = titles[kind]
        if self.mem.q("SELECT 1 FROM issues WHERE assignee=? AND title=? AND "
                      "status IN ('open','in_progress')", (agent, title)):
            return 0          # already queued, do not pile up
        return self.mem.create_issue(title, body, agent, author="ceo")
