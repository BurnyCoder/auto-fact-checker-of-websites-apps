"""Budget ledger and Agent SDK wrapper tests (fake SDK message streams, $0)."""

from __future__ import annotations

import os

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from pydantic import SecretStr

from factcheck.pipeline.agent import AgentRunner
from factcheck.pipeline.budget import BudgetBusy, BudgetExceeded, BudgetLedger, estimate_cost

SMOKE_USAGE = {"input_tokens": 2, "cache_creation_input_tokens": 1166, "cache_read_input_tokens": 0, "output_tokens": 83,
               "server_tool_use": {"web_search_requests": 0},
               "cache_creation": {"ephemeral_1h_input_tokens": 1166, "ephemeral_5m_input_tokens": 0}}


def test_estimate_matches_live_sdk_cost():
    """Our formula reproduced the SDK's total_cost_usd (0.0002749) from the live smoke test exactly."""
    assert estimate_cost("claude-haiku-5-5", SMOKE_USAGE) == pytest.approx(0.0002749, abs=1e-9)


def test_web_searches_are_charged():
    usage = {"input_tokens": 0, "output_tokens": 0, "server_tool_use": {"web_search_requests": 3}}
    assert estimate_cost("claude-haiku-5-5", usage) == pytest.approx(0.03)


def test_ledger_hard_stop_and_daily_cap(ledger):
    ledger.record(purpose="eval", model="claude-haiku-5-5", auth="subscription",
                  usage={"server_tool_use": {"web_search_requests": 995}}, sdk_cost_usd=None, run_id="r1")
    assert ledger.spent_total() == pytest.approx(9.95)
    with pytest.raises(BudgetExceeded):
        ledger.reserve(0.10, "eval")                      # 9.95 + 0.10 > 10
    ledger.reserve(0.04, "eval")                          # fits
    with pytest.raises(BudgetBusy):
        ledger.reserve(0.02, "eval")                      # in-flight reservation counts too
    ledger.release(0.04)


def app_row(ledger, cost):
    ledger.record(purpose="app-verify", model="claude-haiku-5-5", auth="subscription", usage=None,
                  sdk_cost_usd=cost, run_id=f"r{cost}")


def test_daily_cap_busy_is_not_exhausted(ledger):
    """Regression (2026-11-13, live Chrome test): $0.63 spent of a $1 daily cap, worst case $0.21 per check. The second
    concurrent check used to fail with 'daily app budget reached' although only in-flight reservations blocked it."""
    app_row(ledger, 0.63)
    ledger.reserve(0.21, "app-verify")                    # 0.63 + 0.21 fits
    with pytest.raises(BudgetBusy):                       # fits once the first call finishes: wait, don't fail
        ledger.reserve(0.21, "app-verify")
    ledger.release(0.21)
    ledger.reserve(0.21, "app-verify")
    ledger.release(0.21)
    app_row(ledger, 0.20)                                 # 0.83 + 0.21 > 1: really exhausted
    with pytest.raises(BudgetExceeded) as err:
        ledger.reserve(0.21, "app-verify")
    assert not isinstance(err.value, BudgetBusy) and "DAILY_APP_BUDGET_USD" in str(err.value)


def test_ledger_uses_max_of_sdk_and_own_estimate(ledger):
    cost = ledger.record(purpose="dev", model="claude-haiku-5-5", auth="subscription", usage=SMOKE_USAGE,
                         sdk_cost_usd=0.5, run_id="r2")
    assert cost == 0.5


def make_stream(result: ResultMessage, with_tool: bool = True):
    """A fake `query` function yielding a realistic message sequence; records the options it got."""
    seen = {}

    async def fake_query(*, prompt, options):
        seen.setdefault("options", []).append(options)
        yield SystemMessage(subtype="init", data={"tools": ["WebSearch", "StructuredOutput"], "apiKeySource": "none"})
        if with_tool:
            yield AssistantMessage(content=[TextBlock(text="searching"),
                                            ToolUseBlock(id="t1", name="WebSearch", input={"query": "x"})], model="m")
            yield UserMessage(content=[ToolResultBlock(tool_use_id="t1", content=[{"type": "text", "text": "https://a.org/p result"}])])
        yield result

    return fake_query, seen


def ok_result(**kw):
    base = {"subtype": "success", "duration_ms": 10, "duration_api_ms": 5, "is_error": False, "num_turns": 3,
            "session_id": "s", "total_cost_usd": 0.001, "usage": SMOKE_USAGE, "structured_output": {"answer": 1}}
    base.update(kw)
    return ResultMessage(**base)


async def test_runner_collects_trace_cost_and_isolation(settings, ledger):
    fake_query, seen = make_stream(ok_result())
    runner = AgentRunner(settings, ledger, query_fn=fake_query)
    run = await runner.run("p", kind="verify", purpose="test", system_prompt="s", schema={"type": "object"},
                           tools=["WebSearch"], allowed_tools=["WebSearch"])
    assert run.ok and run.structured == {"answer": 1}
    assert [(e.name, e.output) for e in run.trace] == [("WebSearch", "https://a.org/p result")]
    # usage reports 0 searches but the trace has one WebSearch call -> our estimate adds $0.01 and wins the max()
    assert run.web_searches == 1
    assert ledger.spent_total() == pytest.approx(0.0002749 + 0.01, abs=1e-6)
    opts = seen["options"][0]
    assert opts.setting_sources == [] and opts.strict_mcp_config and opts.permission_mode == "dontAsk"
    assert opts.tools == ["WebSearch"] and opts.model == "claude-haiku-5-5"
    assert opts.env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] == "1" and "ANTHROPIC_API_KEY" not in opts.env
    assert "no-session-persistence" in opts.extra_args
    assert opts.output_format["type"] == "json_schema"


async def test_inherited_api_key_removed_in_subscription_mode(settings, ledger, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-should-be-removed")
    AgentRunner(settings, ledger, query_fn=make_stream(ok_result())[0])
    assert "ANTHROPIC_API_KEY" not in os.environ


async def test_auth_failure_falls_back_to_api_key(settings, ledger):
    settings.anthropic_api_key = SecretStr("sk-ant-fallback")
    results = [ok_result(subtype="error_during_execution", is_error=True, api_error_status=401, structured_output=None,
                         total_cost_usd=0.0), ok_result()]
    calls = []

    async def fake_query(*, prompt, options):
        calls.append(options)
        yield results[len(calls) - 1]

    runner = AgentRunner(settings, ledger, query_fn=fake_query)
    run = await runner.run("p", kind="extract", purpose="test", system_prompt="s", schema={})
    assert run.ok and run.auth == "api_key" and runner.auth == "api_key"
    assert "ANTHROPIC_API_KEY" not in calls[0].env and calls[1].env["ANTHROPIC_API_KEY"] == "sk-ant-fallback"
    auths = [row["auth"] for row in ledger._rows()]
    assert auths == ["subscription", "api_key"]


async def test_no_fallback_without_key(settings, ledger):
    fake_query, _ = make_stream(ok_result(subtype="error_during_execution", is_error=True, api_error_status=429,
                                          structured_output=None), with_tool=False)
    run = await AgentRunner(settings, ledger, query_fn=fake_query).run("p", kind="x", purpose="t", system_prompt="s", schema={})
    assert not run.ok and "auth failed" in run.error


async def test_budget_refuses_before_calling(settings, ledger):
    ledger.total_cap = 0.001
    fake_query, seen = make_stream(ok_result())
    with pytest.raises(BudgetExceeded):
        await AgentRunner(settings, ledger, query_fn=fake_query).run("p", kind="x", purpose="t", system_prompt="s",
                                                                     schema={}, worst_case_usd=0.02)
    assert "options" not in seen                          # never reached the SDK


async def test_runner_waits_while_other_calls_hold_the_budget(settings, ledger):
    import asyncio

    ledger.total_cap = 0.03
    ledger.reserve(0.02, "t")                             # another call in flight
    fake_query, seen = make_stream(ok_result())
    runner = AgentRunner(settings, ledger, query_fn=fake_query)
    runner.budget_poll_s = 0.01
    task = asyncio.create_task(runner.run("p", kind="x", purpose="t", system_prompt="s", schema={}, worst_case_usd=0.02))
    await asyncio.sleep(0.1)
    assert not task.done() and "options" not in seen      # queued, not failed and not yet spending
    ledger.release(0.02)                                  # the other call finished
    assert (await asyncio.wait_for(task, 2)).ok


def test_ledger_survives_concurrent_processes(tmp_path):
    """4 processes x 50 appends to one ledger: every line must parse (regression for the 2026-11-08 corruption)."""
    import subprocess
    import sys

    path = tmp_path / "ledger.jsonl"
    code = ("import sys; from pathlib import Path; from factcheck.pipeline.budget import BudgetLedger; "
            "led = BudgetLedger(Path(sys.argv[1]), 100.0)\n"
            "for i in range(50): led.record(purpose='t', model='claude-haiku-5-5', auth='x', usage=None, "
            "sdk_cost_usd=0.001, run_id=f'{sys.argv[2]}-{i}', extra={'pad': 'x' * 300})")
    procs = [subprocess.Popen([sys.executable, "-c", code, str(path), str(k)]) for k in range(4)]
    assert all(p.wait(timeout=120) == 0 for p in procs)
    led = BudgetLedger(path, 100.0)
    assert len(led._rows()) == 200 and led.spent_total() == pytest.approx(0.2)


def test_defaults_check_only_on_request_with_a_20_usd_cap(monkeypatch):
    """User decision (2026-11-13): fact-check only when the button / shortcut is pressed; project cap $20."""
    from factcheck.config import Settings

    for var in ("FACTCHECK_MODE", "TOTAL_BUDGET_USD"):
        monkeypatch.delenv(var, raising=False)
    s = Settings(_env_file=None)
    assert s.factcheck_mode == "hotkey" and s.total_budget_usd == 20.0
