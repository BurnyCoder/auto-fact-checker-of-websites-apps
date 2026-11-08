"""Claim verification: one Haiku agent per claim with web search, page fetch, downloads and sandboxed bash.

Local context: set up the per-claim workspace, tools and PreToolUse hooks, run the agent, validate the
verdict rules (repairing once if needed), then check sources against the tool trace and the live pages.
Global context: service.py calls `verify_claim` for the app; eval/run.py calls it with benchmark
conditions (closed-book, fact-check sites blocked, ...) via `VerifyConfig`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from claude_agent_sdk import HookMatcher
from pydantic import ValidationError

from factcheck.models import Claim, Computation, Verdict, rule_violations
from factcheck.pipeline.agent import AgentRun, AgentRunner
from factcheck.pipeline.agent_tools import BASH_TOOL, build_tool_server
from factcheck.pipeline.budget import WEB_SEARCH_USD
from factcheck.pipeline.policy import OPEN_POLICY, UrlPolicy
from factcheck.pipeline.prompts import (
    REPAIR_SYSTEM,
    VERDICT_SCHEMA,
    build_repair_prompt,
    build_verify_prompt,
    build_verify_system,
)
from factcheck.pipeline.sandbox import DEFAULT_SANDBOX, SandboxConfig
from factcheck.pipeline.sources import mark_in_trace, verify_quotes
from factcheck.pipeline.workspace import workspace

log = logging.getLogger(__name__)
BACKEND_SEARCHES_PER_CALL = 8   # one WebSearch call may run up to 8 backend searches (tools reference) -> worst case


@dataclass
class VerifyConfig:
    """One verification condition. Defaults = the app's product behaviour."""

    web: bool = True                      # False = closed-book (bash only)
    policy: UrlPolicy = field(default_factory=lambda: OPEN_POLICY)
    max_searches: int = 2
    max_fetches: int = 3
    max_turns: int = 12
    claim_date: str | None = None         # eval: judge as of this date
    verify_quotes: bool = True
    keep_work: bool = False
    timeout_s: float = 240.0

    def worst_case_usd(self) -> float:
        searches = self.max_searches * BACKEND_SEARCHES_PER_CALL if self.web else 0
        return round(searches * WEB_SEARCH_USD + 0.05, 4)  # + generous token allowance


def _deny(reason: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def build_hooks(cfg: VerifyConfig, counters: dict[str, int]) -> dict:
    """PreToolUse hooks: cap WebSearch/WebFetch calls and enforce the URL policy (hooks doc:
    https://code.claude.com/docs/en/agent-sdk/hooks.md). `on_failure="block"` makes a crashing hook deny."""

    async def on_search(input_data, tool_use_id, context):
        counters["WebSearch"] = counters.get("WebSearch", 0) + 1
        if counters["WebSearch"] > cfg.max_searches:
            return _deny(f"search limit reached ({cfg.max_searches}); decide with the evidence you have")
        blocked = cfg.policy.search_blocked_domains()
        if not blocked:
            return {}
        new_input = dict(input_data.get("tool_input") or {})
        new_input.pop("allowed_domains", None)        # WebSearch cannot combine allowed + blocked domains
        new_input["blocked_domains"] = sorted(set(new_input.get("blocked_domains") or []) | set(blocked))
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                       "updatedInput": new_input}}

    async def on_fetch(input_data, tool_use_id, context):
        counters["WebFetch"] = counters.get("WebFetch", 0) + 1
        if counters["WebFetch"] > cfg.max_fetches:
            return _deny(f"page-fetch limit reached ({cfg.max_fetches})")
        reason = cfg.policy.check(str((input_data.get("tool_input") or {}).get("url", "")))
        return _deny(f"URL not allowed: {reason}") if reason else {}

    return {"PreToolUse": [HookMatcher(matcher="WebSearch", hooks=[on_search], on_failure="block"),
                           HookMatcher(matcher="WebFetch", hooks=[on_fetch], on_failure="block")]}


def evidence_digest(run: AgentRun, limit_each: int = 2500, limit_total: int = 20000) -> str:
    """Tool calls and outputs as plain text, for the cheap repair pass."""
    parts, total = [], 0
    for ev in run.trace:
        chunk = f"[{ev.name}] input={json.dumps(ev.input, ensure_ascii=False)[:500]}\n{ev.output[:limit_each]}"
        if total + len(chunk) > limit_total:
            break
        parts.append(chunk)
        total += len(chunk)
    return "\n\n".join(parts) or "(no tool outputs)"


def _parse(structured) -> tuple[Verdict | None, str | None]:
    try:
        return Verdict.model_validate(structured), None
    except ValidationError as exc:
        return None, str(exc)[:500]


def failed_verdict(claim: Claim, error: str) -> Verdict:
    return Verdict(label="Not Enough Evidence", confidence=0.0, confidence_reasoning="The check could not be completed.",
                   explanation=f"Verification failed: {error}", claim_id=claim.id, claim=claim.text, incomplete=True,
                   error=error, flags=["error"])


async def verify_claim(claim: Claim, runner: AgentRunner, cfg: VerifyConfig, *, purpose: str, context: str,
                       sandbox_cfg: SandboxConfig = DEFAULT_SANDBOX) -> Verdict:
    """Verify one claim end to end and return a Verdict with metadata filled in."""
    counters: dict[str, int] = {}
    with workspace(runner.settings.work_root, claim.id, keep=cfg.keep_work) as ws:
        servers, mcp_names = build_tool_server(ws, cfg.policy, sandbox_cfg, with_fetch=cfg.web)
        builtins = ["WebSearch", "WebFetch"] if cfg.web else []
        run = await runner.run(
            build_verify_prompt(claim, context), kind="verify", purpose=purpose,
            system_prompt=build_verify_system(cfg.web, cfg.max_searches, cfg.max_fetches, cfg.claim_date),
            schema=VERDICT_SCHEMA, tools=builtins, mcp_servers=servers, allowed_tools=builtins + mcp_names,
            hooks=build_hooks(cfg, counters) if cfg.web else None, max_turns=cfg.max_turns,
            worst_case_usd=cfg.worst_case_usd(), timeout_s=cfg.timeout_s, meta={"claim_id": claim.id},
        )
    total_cost = run.cost_usd
    if not run.ok:
        v = failed_verdict(claim, run.error or "no structured output")
        v.cost_usd, v.duration_s, v.model, v.auth = total_cost, run.duration_s, runner.settings.model, run.auth
        return v

    verdict, parse_error = _parse(run.structured)
    problems = [parse_error] if parse_error else rule_violations(verdict, require_sources=cfg.web)
    flags: list[str] = []
    if problems:                                         # one cheap, tool-less repair pass over the same evidence
        log.info("verdict for %s broke rules %s; repairing", claim.id, problems)
        repair = await runner.run(build_repair_prompt(claim, run.structured or {}, problems, evidence_digest(run)),
                                  kind="repair", purpose=purpose, system_prompt=REPAIR_SYSTEM, schema=VERDICT_SCHEMA,
                                  tools=[], max_turns=3, worst_case_usd=0.03, timeout_s=90,
                                  meta={"claim_id": claim.id})
        total_cost += repair.cost_usd
        fixed, parse_error = _parse(repair.structured) if repair.ok else (None, repair.error)
        if fixed is not None:
            verdict = fixed
            flags.append("repaired")
        remaining = [parse_error] if fixed is None else rule_violations(verdict, require_sources=cfg.web)
        if verdict is None:
            v = failed_verdict(claim, f"invalid verdict: {parse_error}")
            v.cost_usd = total_cost
            return v
        if remaining:
            flags.append("incomplete")
            verdict.incomplete = True
            verdict.error = "; ".join(remaining)

    # computations: keep what the model listed, add sandbox calls it forgot to mention
    listed = {c.command.strip() for c in verdict.computations}
    for ev in run.trace:
        cmd = str(ev.input.get("command", "")).strip()
        if ev.name == BASH_TOOL and cmd and cmd not in listed:
            verdict.computations.append(Computation(command=cmd, output=ev.output[:1500]))

    mark_in_trace(verdict.sources, run.trace)
    if cfg.web and cfg.verify_quotes and verdict.sources:
        await verify_quotes(verdict.sources, cfg.policy)
    if any(s.in_trace is False for s in verdict.sources):
        flags.append("source-not-in-trace")
    if any(s.quote_verified is False for s in verdict.sources):
        flags.append("quote-unverified")

    verdict.claim_id, verdict.claim = claim.id, claim.text
    verdict.flags = sorted(set(verdict.flags + flags))
    verdict.cost_usd = round(total_cost, 6)
    verdict.duration_s = run.duration_s
    verdict.num_turns = run.num_turns
    verdict.web_searches = run.web_searches
    verdict.tool_counts = run.tool_counts()
    verdict.model = runner.settings.model
    verdict.auth = run.auth
    return verdict
