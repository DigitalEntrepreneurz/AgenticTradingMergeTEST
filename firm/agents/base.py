"""Common agent scaffolding: identity, memory access, LLM budget, issue handling."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Config
from ..llm import LLM, LLMReply
from ..memory import Memory

INSTRUCTIONS_DIR = Path(__file__).resolve().parent / "instructions"


@dataclass
class AgentContext:
    """Everything an agent is allowed to touch."""
    cfg: Config
    memory: Memory
    llm: LLM
    brokers: dict[str, Any] = field(default_factory=dict)

    def primary_broker(self):
        return next(iter(self.brokers.values()), None)


class Agent:
    name: str = "agent"
    title: str = "Agent"
    charter: str = ""

    def __init__(self, ctx: AgentContext):
        self.ctx = ctx
        self.cfg = ctx.cfg
        self.mem = ctx.memory
        self.llm = ctx.llm

    # ---- config helpers -------------------------------------------------
    @property
    def model(self) -> str:
        return self.cfg.get(f"agents.{self.name}.model", "claude-sonnet-4-5")

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get(f"agents.{self.name}.enabled", True))

    @property
    def daily_budget(self) -> float:
        return float(self.cfg.get(f"agents.{self.name}.budget_usd_per_day", 1.0))

    def spent_today(self) -> float:
        rows = self.mem.q(
            "SELECT COALESCE(SUM(usd),0) s FROM costs WHERE agent=? AND created_at>?",
            (self.name, time.time() - 86400))
        return float(rows[0]["s"])

    def within_budget(self) -> bool:
        return self.spent_today() < self.daily_budget

    # ---- comms ----------------------------------------------------------
    def log(self, message: str, level: str = "info", meta: dict | None = None) -> None:
        self.mem.log(level, self.name, message, meta)

    def remember(self, kind: str, key: str, content: str, meta: dict | None = None) -> None:
        self.mem.remember(self.name, kind, key, content, meta)

    def report_to_ceo(self, issue_id: int, summary: str, status: str = "done") -> None:
        """Finish an assignment and notify the CEO on the issue thread."""
        self.mem.comment(issue_id, self.name, summary)
        self.mem.set_issue_status(issue_id, status, summary[:4000])
        self.log(f"reported on issue #{issue_id}: {summary[:120]}")

    def think(self, system: str, prompt: str, max_tokens: int = 1200,
              temperature: float = 0.3) -> LLMReply:
        """LLM call gated by this agent's own daily budget.

        When `llm.routing` is on, the model is chosen per call from this
        agent's preference list, skipping pools that are spent. On a
        multi-pool key an agent pinned to one model dies with that model's
        quota, which is a silent failure - routing turns that into a
        transparent hand-off to the next model.
        """
        if not self.within_budget():
            return LLMReply(text="", model=self.model, used_llm=False,
                            error=f"{self.name} daily budget "
                                  f"${self.daily_budget:.2f} exhausted")

        model = self.model
        if self.cfg.get("llm.routing", False):
            try:
                from ..router import pick
                chosen, why = pick(self.mem, self.name, need=max_tokens * 3)
                if not chosen:
                    # every pool for this role is empty: say so, fall back
                    return LLMReply(text="", model=self.model, used_llm=False,
                                    error=why)
                if chosen != model:
                    self.log(f"routed to {why}", "info")
                model = chosen
            except Exception as e:            # routing must never block work
                self.log(f"model routing unavailable: {e}", "warn")

        return self.llm.ask(self.name, model, system, prompt, max_tokens, temperature)

    # ---- editable instructions -----------------------------------------
    @property
    def instructions_path(self) -> Path:
        """The human-editable instruction doc that governs this agent."""
        return INSTRUCTIONS_DIR / f"{self.name}.md"

    def instructions(self) -> str:
        """Load this agent's instruction file.

        The board edits these .md files to change how an agent behaves without
        touching Python. Missing file -> fall back to the built-in charter, so
        deleting a doc degrades gracefully instead of crashing the firm.
        """
        try:
            text = self.instructions_path.read_text(encoding="utf-8").strip()
            if text:
                return text
        except (OSError, UnicodeDecodeError):
            pass
        return self.charter

    def system_prompt(self, fallback: str = "") -> str:
        """Instruction doc wins; built-in prompt is the safety net."""
        doc = self.instructions()
        base = fallback or self.charter
        if doc and doc != self.charter:
            return f"{doc}\n\n---\n{base}" if base else doc
        return base

    def firm_context(self) -> str:
        return (f"Firm: {self.cfg.firm_name}\n"
                f"Board goal: {self.cfg.get('firm.goal', '')}\n"
                f"Risk tolerance: {self.cfg.get('firm.risk_tolerance', 'moderate')}\n"
                f"Symbols: {', '.join(self.cfg.symbols)} on {self.cfg.timeframe}\n"
                f"Mode: {'LIVE' if self.cfg.live_enabled else 'PAPER'}")

    # ---- entry point ----------------------------------------------------
    def handle(self, issue: dict) -> str:
        raise NotImplementedError

    def tick(self) -> None:
        """Optional periodic work outside the issue system."""
        return None

    def work(self) -> int:
        """Process every open issue assigned to this agent. Returns count handled."""
        if not self.enabled:
            return 0
        done = 0
        for issue in self.mem.open_issues(self.name):
            try:
                self.mem.set_issue_status(issue["id"], "in_progress")
                summary = self.handle(issue)
                self.report_to_ceo(issue["id"], summary or "completed")
                done += 1
            except Exception as e:
                msg = f"{type(e).__name__}: {e}"
                self.mem.comment(issue["id"], self.name, f"FAILED: {msg}")
                self.mem.set_issue_status(issue["id"], "blocked", msg[:2000])
                self.log(f"issue #{issue['id']} failed: {msg}", "error")
        return done
