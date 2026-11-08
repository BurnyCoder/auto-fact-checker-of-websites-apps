"""Shared fixtures: isolated settings/ledger in a temp dir and a scripted runner for $0 pipeline tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from factcheck.config import Settings
from factcheck.pipeline.agent import AgentRun
from factcheck.pipeline.budget import BudgetLedger


class TmpSettings(Settings):
    """Settings whose paths point into a temp folder (never the real ledger, logs or .env key)."""

    tmp_root: Path = Path(".")

    @property
    def ledger_path(self) -> Path:
        return self.tmp_root / "ledger.jsonl"


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Settings:
    s = TmpSettings(_env_file=None, tmp_root=tmp_path, anthropic_api_key=None, auth_mode="auto")
    s.__dict__["work_root"] = tmp_path / "work"        # override cached_property values
    s.__dict__["data_dir"] = tmp_path / "data"
    s.__dict__["logs_dir"] = tmp_path / "logs"
    for p in (s.work_root, s.data_dir, s.logs_dir):
        p.mkdir(exist_ok=True)
    return s


@pytest.fixture
def ledger(settings) -> BudgetLedger:
    return BudgetLedger(settings.ledger_path, total_cap_usd=10.0, daily_app_cap_usd=1.0)


class ScriptedRunner:
    """Returns pre-baked AgentRuns in order and records the calls it received."""

    def __init__(self, settings: Settings, runs: list[AgentRun]):
        self.settings = settings
        self.runs = list(runs)
        self.calls: list[dict] = []

    async def run(self, prompt: str, **kwargs) -> AgentRun:
        self.calls.append({"prompt": prompt, **kwargs})
        return self.runs.pop(0)


@pytest.fixture
def scripted(settings):
    return lambda runs: ScriptedRunner(settings, runs)
