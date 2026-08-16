from .backtest import BacktestAgent
from .base import Agent, AgentContext
from .ceo import CEOAgent
from .cost import CostAgent
from .execution import ExecutionAgent
from .research import ResearchAgent
from .risk import RiskAgent
from .scout import ScoutAgent

__all__ = ["Agent", "AgentContext", "CEOAgent", "ResearchAgent", "BacktestAgent",
           "RiskAgent", "ExecutionAgent", "CostAgent", "ScoutAgent", "build_firm"]


def build_firm(ctx: AgentContext) -> dict[str, Agent]:
    risk = RiskAgent(ctx)
    return {
        "ceo": CEOAgent(ctx),
        "research": ResearchAgent(ctx),
        "backtest": BacktestAgent(ctx),
        "risk": risk,
        "execution": ExecutionAgent(ctx, risk=risk),
        "cost_optimizer": CostAgent(ctx),
        "scout": ScoutAgent(ctx),
    }
