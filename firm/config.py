"""Configuration loading and validated access."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "firm.yaml"
EXAMPLE_PATH = ROOT / "config" / "firm.example.yaml"


def _load_dotenv() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


@dataclass
class Config:
    raw: dict[str, Any] = field(default_factory=dict)
    path: Path = CONFIG_PATH

    # ---- generic access -------------------------------------------------
    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def __getitem__(self, dotted: str) -> Any:
        return self.get(dotted)

    # ---- frequently used ------------------------------------------------
    @property
    def firm_name(self) -> str:
        return self.get("firm.name", "Unnamed Firm")

    @property
    def paper_trading(self) -> bool:
        return bool(self.get("trading.paper_trading", True))

    @property
    def allow_live_orders(self) -> bool:
        return bool(self.get("trading.allow_live_orders", False))

    @property
    def live_enabled(self) -> bool:
        """Real money orders require BOTH switches. Fail closed."""
        return (not self.paper_trading) and self.allow_live_orders

    @property
    def symbols(self) -> list[str]:
        return list(self.get("trading.symbols", ["EURUSD"]))

    @property
    def timeframe(self) -> str:
        return str(self.get("trading.timeframe", "H1"))

    @property
    def anthropic_key(self) -> str:
        """Back-compat alias for the active provider's key."""
        return self.llm_key

    @property
    def llm_provider(self) -> str:
        return str(self.get("llm.provider", "anthropic") or "anthropic").strip().lower()

    @property
    def llm_key(self) -> str:
        """Key for the configured provider: config first, then its env var."""
        from .llm import PROVIDERS
        explicit = str(self.get("llm.api_key") or "").strip()
        if explicit:
            return explicit
        _, _, env_var = PROVIDERS.get(self.llm_provider, PROVIDERS["anthropic"])
        return (os.environ.get(env_var, "")
                or os.environ.get("LLM_API_KEY", "")).strip()

    @property
    def llm_base_url(self) -> str:
        return str(self.get("llm.base_url", "") or "").strip()

    @property
    def llm_model_prefix(self) -> str:
        return str(self.get("llm.model_prefix", "") or "").strip()

    def accounts(self) -> list[dict[str, Any]]:
        accs = self.get("broker.accounts") or []
        out = [a for a in accs if a.get("enabled", True)]
        if not out:  # single-account fallback
            out = [{
                "id": "default",
                "kind": self.get("broker.kind", "simulated"),
                "platform": "MT5",
                "starting_balance": 10000,
            }]
        return out

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(yaml.safe_dump(self.raw, sort_keys=False, allow_unicode=True))


def load_config(path: str | Path | None = None) -> Config:
    _load_dotenv()
    p = Path(path) if path else CONFIG_PATH
    if not p.exists():
        p = EXAMPLE_PATH
    data = yaml.safe_load(p.read_text()) or {}
    return Config(raw=data, path=Path(path) if path else CONFIG_PATH)
