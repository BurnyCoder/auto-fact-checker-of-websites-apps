"""Thin wrapper around `claude_agent_sdk.query()`: isolation, auth fallback, logging, budget, tool trace.

Local context: `AgentRunner.run()` executes one agent session and returns an `AgentRun` with the
structured output, cost, usage and every tool call/result.
Global context: extract.py (no tools) and verify.py (web + sandbox tools) both call this, so
isolation, auth and spend tracking are implemented exactly once. Options are grounded in the SDK docs
(https://code.claude.com/docs/en/agent-sdk/python.md) and in the installed package (0.2.179).
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import claude_agent_sdk
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKError,
    RateLimitEvent,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from factcheck.config import Settings
from factcheck.log import llm_event
from factcheck.pipeline.budget import BudgetLedger

log = logging.getLogger(__name__)

# Env vars that keep the user's own Claude Code setup out of the agent
# (https://code.claude.com/docs/en/agent-sdk/claude-code-features.md, .../modifying-system-prompts.md).
ISOLATION_ENV = {
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",       # no auto-memory reads/writes
    "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",        # never load any CLAUDE.md
    "ENABLE_CLAUDEAI_MCP_SERVERS": "false",       # no claude.ai connectors (subscription login exposes them)
}
AUTH_FAILURE_ERRORS = {"authentication_failed", "billing_error", "rate_limit"}   # AssistantMessage.error values
AUTH_FAILURE_STATUS = {401, 403, 429}
AUTH_FAILURE_TEXT = ("usage limit", "please run /login", "invalid api key", "not logged in", "credit balance")


@dataclass
class ToolEvent:
    """One tool call made by the agent and (once it arrives) its result."""

    id: str
    name: str
    input: dict[str, Any]
    output: str = ""
    is_error: bool = False


@dataclass
class AgentRun:
    """Everything we keep from one agent session."""

    run_id: str
    ok: bool
    structured: Any = None
    subtype: str = ""
    cost_usd: float = 0.0
    usage: dict[str, Any] | None = None
    num_turns: int = 0
    duration_s: float = 0.0
    tools_available: list[str] = field(default_factory=list)
    trace: list[ToolEvent] = field(default_factory=list)
    text: str = ""
    error: str | None = None
    auth: str = ""

    @property
    def web_searches(self) -> int:
        """Searches made. Claude Code's WebSearch runs in a sub-request that is missing from `usage`
        (observed live 2026-11-08: one WebSearch call, web_search_requests=0), so count tool calls as a floor."""
        reported = int(((self.usage or {}).get("server_tool_use") or {}).get("web_search_requests", 0) or 0)
        return max(reported, self.tool_counts().get("WebSearch", 0))

    def tool_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for ev in self.trace:
            counts[ev.name] = counts.get(ev.name, 0) + 1
        return counts


def _result_text(content: Any) -> str:
    """ToolResultBlock.content is a string or a list of MCP content dicts; flatten to text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for item in content:
        if isinstance(item, dict):
            parts.append(item.get("text") or str({k: v for k, v in item.items() if k != "type"}))
        else:
            parts.append(str(item))
    return "\n".join(parts)


def _sum_usage(usages) -> dict[str, Any] | None:
    """Add up per-message usage dicts (token counts and cache-creation breakdown) for partial runs."""
    total: dict[str, Any] = {}
    for u in usages:
        for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
            total[k] = total.get(k, 0) + (u.get(k) or 0)
        cc = u.get("cache_creation") or {}
        tc = total.setdefault("cache_creation", {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 0})
        for k in tc:
            tc[k] += cc.get(k) or 0
    return total or None


class AuthFallback(Exception):
    """Internal signal: subscription auth failed or hit its limit; retry with the API key."""


class AgentRunner:
    """Runs isolated agent sessions with auth fallback, budget checks and full logging."""

    def __init__(self, settings: Settings, ledger: BudgetLedger,
                 query_fn: Callable[..., AsyncIterator[Any]] = claude_agent_sdk.query):
        self.settings = settings
        self.ledger = ledger
        self.query_fn = query_fn
        self.auth = "api_key" if settings.auth_mode == "api_key" else "subscription"
        self._cwd = tempfile.mkdtemp(prefix="factcheck-agent-")  # empty folder: the CLI sees no project files
        if self.auth == "subscription" and "ANTHROPIC_API_KEY" in os.environ:
            # The SDK merges os.environ into the CLI env (subprocess_cli.py), and the CLI prefers an API key
            # over the subscription login, so an inherited key must be removed from our own environment.
            log.warning("removing inherited ANTHROPIC_API_KEY from the environment (subscription mode)")
            os.environ.pop("ANTHROPIC_API_KEY")

    def _env(self) -> dict[str, str]:
        env = dict(ISOLATION_ENV)
        if self.auth == "api_key":
            if self.settings.anthropic_api_key is None:
                raise RuntimeError("API-key auth requested but ANTHROPIC_API_KEY is not set in .env")
            env["ANTHROPIC_API_KEY"] = self.settings.anthropic_api_key.get_secret_value()
        return env

    def build_options(self, *, system_prompt: str, schema: dict[str, Any], tools: list[str],
                      mcp_servers: dict[str, Any] | None, allowed_tools: list[str], hooks: dict | None,
                      max_turns: int, max_budget_usd: float) -> ClaudeAgentOptions:
        """ClaudeAgentOptions with isolation; see the module docstring for sources."""
        return ClaudeAgentOptions(
            model=self.settings.model,
            system_prompt=system_prompt,                 # custom string, not the Claude Code preset
            tools=tools,                                 # allow-list of built-in tools ([] = none)
            allowed_tools=allowed_tools,                 # pre-approved tools (built-in + MCP)
            permission_mode="dontAsk",                   # anything not pre-approved is denied, never prompted
            mcp_servers=mcp_servers or {},
            strict_mcp_config=True,                      # ignore every MCP config except ours
            setting_sources=[],                          # no user/project/local settings, hooks, skills
            cwd=self._cwd,
            env=self._env(),
            extra_args={"no-session-persistence": None},  # don't save screen text as resumable sessions
            output_format={"type": "json_schema", "schema": schema},
            max_turns=max_turns,
            max_budget_usd=max_budget_usd,
            effort=self.settings.effort,
            hooks=hooks,
        )

    async def run(self, prompt: str, *, kind: str, purpose: str, system_prompt: str, schema: dict[str, Any],
                  tools: list[str] | None = None, mcp_servers: dict[str, Any] | None = None,
                  allowed_tools: list[str] | None = None, hooks: dict | None = None, max_turns: int = 2,
                  worst_case_usd: float = 0.02, timeout_s: float = 120.0, meta: dict | None = None) -> AgentRun:
        """Run one session; on subscription auth failure in auto mode, retry once with the API key."""
        self.ledger.reserve(worst_case_usd, purpose)   # raises BudgetExceeded before spending anything
        try:
            try:
                return await self._run_once(prompt, kind, purpose, system_prompt, schema, tools or [], mcp_servers,
                                            allowed_tools or [], hooks, max_turns, worst_case_usd, timeout_s, meta)
            except AuthFallback as exc:
                if self.settings.auth_mode != "auto" or self.settings.anthropic_api_key is None:
                    return AgentRun(run_id="-", ok=False, error=f"auth failed: {exc}", auth=self.auth)
                log.warning("subscription auth unavailable (%s); switching to the API key for the rest of this run", exc)
                self.auth = "api_key"
                self.ledger.release(worst_case_usd)       # the first attempt is already in the ledger:
                self.ledger.reserve(worst_case_usd, purpose)  # re-check the cap before spending again
                return await self._run_once(prompt, kind, purpose, system_prompt, schema, tools or [], mcp_servers,
                                            allowed_tools or [], hooks, max_turns, worst_case_usd, timeout_s, meta)
        finally:
            self.ledger.release(worst_case_usd)

    async def _run_once(self, prompt, kind, purpose, system_prompt, schema, tools, mcp_servers, allowed_tools,
                        hooks, max_turns, worst_case_usd, timeout_s, meta) -> AgentRun:
        run = AgentRun(run_id=uuid.uuid4().hex[:10], ok=False, auth=self.auth)
        opts = self.build_options(system_prompt=system_prompt, schema=schema, tools=tools, mcp_servers=mcp_servers,
                                  allowed_tools=allowed_tools, hooks=hooks, max_turns=max_turns,
                                  max_budget_usd=worst_case_usd)
        llm_event("prompt", run.run_id, agent_kind=kind, purpose=purpose, model=self.settings.model, auth=self.auth,
                  system_prompt=system_prompt, prompt=prompt, tools=tools, allowed_tools=allowed_tools, meta=meta or {})
        pending: dict[str, ToolEvent] = {}
        partial_usage: dict[str, Any] | None = None
        usage_by_message: dict[str, dict[str, Any]] = {}  # AssistantMessage.usage is per message: sum unique ids
        result: ResultMessage | None = None
        fallback_reason: str | None = None
        start = time.monotonic()
        try:
            async with asyncio.timeout(timeout_s):
                async for msg in self.query_fn(prompt=prompt, options=opts):
                    if isinstance(msg, SystemMessage) and msg.subtype == "init":
                        run.tools_available = list(msg.data.get("tools") or [])
                        llm_event("init", run.run_id, tools=run.tools_available, model=msg.data.get("model"),
                                  api_key_source=msg.data.get("apiKeySource"))
                    elif isinstance(msg, AssistantMessage):
                        if msg.error in AUTH_FAILURE_ERRORS:
                            fallback_reason = msg.error
                        if msg.usage:
                            usage_by_message[msg.message_id or str(len(usage_by_message))] = msg.usage
                        for block in msg.content:
                            if isinstance(block, TextBlock):
                                run.text += block.text
                                llm_event("assistant_text", run.run_id, text=block.text)
                            elif isinstance(block, ThinkingBlock):
                                llm_event("thinking", run.run_id, text=block.thinking)
                            elif isinstance(block, ToolUseBlock):
                                ev = ToolEvent(id=block.id, name=block.name, input=dict(block.input))
                                pending[block.id] = ev
                                run.trace.append(ev)
                                llm_event("tool_use", run.run_id, tool=block.name, input=block.input)
                    elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
                        for block in msg.content:
                            if isinstance(block, ToolResultBlock):
                                ev = pending.get(block.tool_use_id)
                                text = _result_text(block.content)
                                if ev is not None:
                                    ev.output, ev.is_error = text, bool(block.is_error)
                                llm_event("tool_result", run.run_id, tool=ev.name if ev else "?", is_error=block.is_error,
                                          output=text)
                    elif isinstance(msg, RateLimitEvent):
                        llm_event("rate_limit", run.run_id, status=msg.rate_limit_info.status,
                                  type=msg.rate_limit_info.rate_limit_type)
                        if msg.rate_limit_info.status == "rejected":
                            fallback_reason = f"rate limit rejected ({msg.rate_limit_info.rate_limit_type})"
                    elif isinstance(msg, ResultMessage):
                        result = msg
        except TimeoutError:
            run.error = f"timed out after {timeout_s:.0f}s"
        except ClaudeSDKError as exc:                  # CLI not found, process crashed, bad JSON, ...
            text = str(exc)
            if self.auth == "subscription" and any(t in text.lower() for t in AUTH_FAILURE_TEXT):
                raise AuthFallback(text[:200]) from exc
            run.error = f"{type(exc).__name__}: {text[:500]}"
        run.duration_s = round(time.monotonic() - start, 2)

        if result is not None:
            err_text = " ".join(result.errors or []) + " " + (result.result or "")
            if self.auth == "subscription" and (
                fallback_reason or result.api_error_status in AUTH_FAILURE_STATUS
                or (result.is_error and any(t in err_text.lower() for t in AUTH_FAILURE_TEXT))
            ):
                self._record(run, purpose, result.usage, result.total_cost_usd, kind)   # still log what it cost
                raise AuthFallback(fallback_reason or f"api status {result.api_error_status}: {err_text[:120]}")
            run.subtype = result.subtype
            run.usage = result.usage
            run.num_turns = result.num_turns
            run.structured = result.structured_output
            run.ok = result.subtype == "success" and result.structured_output is not None
            if not run.ok and run.error is None:
                run.error = f"{result.subtype}: {' '.join(result.errors or [])[:300]}"
            run.cost_usd = self._record(run, purpose, result.usage, result.total_cost_usd, kind)
        else:
            partial_usage = _sum_usage(usage_by_message.values())   # timed out / crashed: charge every message
            run.usage = partial_usage
            run.cost_usd = self._record(run, purpose, partial_usage, None, kind)
        llm_event("result", run.run_id, ok=run.ok, subtype=run.subtype, structured=run.structured, error=run.error,
                  cost_usd=run.cost_usd, usage=run.usage, num_turns=run.num_turns, duration_s=run.duration_s)
        return run

    def _record(self, run: AgentRun, purpose: str, usage, sdk_cost, kind: str) -> float:
        reported = int(((usage or {}).get("server_tool_use") or {}).get("web_search_requests", 0) or 0)
        unreported = max(0, run.tool_counts().get("WebSearch", 0) - reported)   # searches missing from usage
        return self.ledger.record(purpose=purpose, model=self.settings.model, auth=self.auth, usage=usage,
                                  sdk_cost_usd=sdk_cost, run_id=run.run_id, extra_searches=unreported,
                                  extra={"agent_kind": kind, "tools": run.tool_counts()})
