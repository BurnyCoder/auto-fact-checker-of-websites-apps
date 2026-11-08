"""Settings loaded from `.env` (pydantic-settings) plus derived project paths.

Local context: one typed `Settings` object; secrets are `SecretStr` so they never print by accident.
Global context: every phase in app.py and eval/ reads configuration only from here.
pydantic-settings reads `.env` into this object but does NOT copy values into `os.environ`
(https://docs.pydantic.dev/latest/concepts/pydantic_settings/#dotenv-env-support), which is what keeps
ANTHROPIC_API_KEY away from the Agent SDK subprocess unless the API-key fallback is active.
"""

from __future__ import annotations

import secrets
from functools import cached_property
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]    # src/factcheck/config.py -> project root


class Settings(BaseSettings):
    """All tunables; names match the keys documented in .env.example."""

    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore")

    # --- auth ---
    auth_mode: Literal["auto", "subscription", "api_key"] = "auto"
    anthropic_api_key: SecretStr | None = None
    anthropic_workspace_id: str | None = None          # documentation only; the SDK does not use it

    # --- model / agent ---
    model: str = "claude-haiku-5-5"
    max_turns: int = 12
    max_web_searches: int = 2
    max_web_fetches: int = 3
    effort: Literal["low", "medium", "high"] | None = "low"
    verify_timeout_s: float = 240.0                   # wall-clock cap for one verification
    extract_timeout_s: float = 90.0

    # --- budget (API-equivalent USD) ---
    total_budget_usd: float = 10.0
    daily_app_budget_usd: float = 1.0

    # --- app behaviour ---
    factcheck_mode: Literal["continuous", "hotkey", "both"] = "both"
    factcheck_display: Literal["overlay", "panel", "both"] = "both"
    capture_interval_s: float = 2.0
    hotkey: str = "ctrl+alt+f"
    blocked_apps: str = "1password.exe,keepass.exe,bitwarden.exe,keepassxc.exe"
    server_port: int = 8765
    keep_agent_work: bool = False
    factcheck_fake_llm: bool = False
    max_auto_checks_per_batch: int = 3
    app_concurrency: int = 3

    # --- derived paths (not from .env) ---
    @cached_property
    def data_dir(self) -> Path:
        path = PROJECT_ROOT / "data"
        path.mkdir(exist_ok=True)
        return path

    @cached_property
    def logs_dir(self) -> Path:
        path = PROJECT_ROOT / "logs"
        path.mkdir(exist_ok=True)
        return path

    @cached_property
    def work_root(self) -> Path:
        """Parent folder of every per-claim /work workspace (gitignored)."""
        path = PROJECT_ROOT / ".agent-work"
        path.mkdir(exist_ok=True)
        return path

    @property
    def ledger_path(self) -> Path:
        """Committed spend record: every model call ever made by this project."""
        return PROJECT_ROOT / "budget_ledger.jsonl"

    @property
    def blocked_app_list(self) -> list[str]:
        return [a.strip().lower() for a in self.blocked_apps.split(",") if a.strip()]

    @cached_property
    def server_token(self) -> str:
        """Random bearer token shared with the Chrome extension; created once, stored in data/ (gitignored)."""
        token_file = self.data_dir / "server_token.txt"
        if not token_file.exists():
            token_file.write_text(secrets.token_urlsafe(32), encoding="utf-8")
        return token_file.read_text(encoding="utf-8").strip()


def load_settings(**overrides) -> Settings:
    """Build Settings from .env, with keyword overrides (used by tests and the eval runner)."""
    return Settings(**overrides)
