"""Extraction, verification (with repair pass), verdict rules, sources, segmentation and cache ($0)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from factcheck.models import Claim, SourceInfo, TextBlock, Verdict, VerdictSource, rule_violations
from factcheck.pipeline.agent import AgentRun, ToolEvent
from factcheck.pipeline.agent_tools import BASH_TOOL
from factcheck.pipeline.cache import VerdictCache
from factcheck.pipeline.extract import align_span, extract_claims
from factcheck.pipeline.segment import new_sentences, normalize_claim, split_sentences
from factcheck.pipeline.sources import mark_in_trace, normalize_url, quote_in_text
from factcheck.pipeline.verify import VerifyConfig, verify_claim

SRC = SourceInfo(kind="manual", source_id="tab-1", app="test", title="Test page")


def good_verdict(**kw):
    base = {"label": "Supported", "confidence": 0.8, "confidence_reasoning": "two independent agency sources agree",
            "explanation": "x", "corrected_claim": None, "correction_confidence": None, "correction_reasoning": None,
            "sources": [{"url": "https://a.org/p", "title": "A", "quote": "q", "supports": "verdict"}], "computations": []}
    base.update(kw)
    return base


# ---------------- verdict rules ----------------
def test_refuted_requires_sourced_correction():
    v = Verdict.model_validate(good_verdict(label="Refuted"))
    problems = rule_violations(v)
    assert any("corrected_claim" in p for p in problems) and any("supports='correction'" in p for p in problems)
    fixed = Verdict.model_validate(good_verdict(label="Refuted", corrected_claim="The real number is 3.",
                                                correction_confidence=0.7, correction_reasoning="agency table",
                                                sources=good_verdict()["sources"] + [{"url": "https://b.org", "title": "B",
                                                                                      "quote": "3", "supports": "correction"}]))
    assert rule_violations(fixed) == []


def test_confidence_bounds_enforced():
    with pytest.raises(ValidationError):
        Verdict.model_validate(good_verdict(confidence=1.4))


def test_closed_book_does_not_require_sources():
    v = Verdict.model_validate(good_verdict(sources=[]))
    assert rule_violations(v, require_sources=False) == [] and rule_violations(v) != []


def test_computation_counts_as_evidence():
    v = Verdict.model_validate(good_verdict(sources=[], computations=[{"command": "python -c 'print(1000*1.07**30)'",
                                                                        "output": "7612.26"}]))
    assert rule_violations(v) == []


# ---------------- extraction ----------------
def test_align_span_exact_and_fuzzy():
    text = "Breaking: Unemployment fell to 3.5% in 2019, officials said."
    assert align_span("Unemployment fell to 3.5% in 2019", text) == "Unemployment fell to 3.5% in 2019"
    assert align_span("Unemployment  fell to 3.5 % in 2019", text) is not None   # whitespace/punctuation drift
    assert align_span("The moon is made of cheese", text) is None


async def test_extract_drops_unlocatable_spans(scripted):
    blocks = [TextBlock(id="b1", text="The Eiffel Tower is 330 m tall. I love Paris.")]
    runner = scripted([AgentRun(run_id="r", ok=True, structured={"claims": [
        {"block_id": "b1", "span": "The Eiffel Tower is 330 m tall", "claim": "The Eiffel Tower is 330 m tall.", "reason": "number"},
        {"block_id": "b1", "span": "Totally invented sentence", "claim": "x", "reason": "y"},
    ]})])
    claims = await extract_claims(blocks, SRC, runner, purpose="test")
    assert [c.span for c in claims] == ["The Eiffel Tower is 330 m tall"]
    assert runner.calls[0]["tools"] == [] and runner.calls[0]["kind"] == "extract"


# ---------------- verification ----------------
CLAIM = Claim(id="c1", text="The Eiffel Tower is 330 m tall.", span="330 m tall", block_id="b1", source=SRC)


async def test_verify_good_verdict_marks_sources_and_merges_computations(scripted):
    trace = [ToolEvent(id="1", name="WebSearch", input={"query": "eiffel"}, output="https://a.org/p Eiffel..."),
             ToolEvent(id="2", name=BASH_TOOL, input={"command": "python -c 'print(300+30)'"}, output="exit_code: 0\n330")]
    runner = scripted([AgentRun(run_id="r", ok=True, structured=good_verdict(), trace=trace, cost_usd=0.02)])
    v = await verify_claim(CLAIM, runner, VerifyConfig(verify_quotes=False), purpose="test", context="")
    assert v.label == "Supported" and v.sources[0].in_trace is True and not v.incomplete
    assert v.computations[0].command.startswith("python") and v.cost_usd == 0.02
    call = runner.calls[0]
    assert call["tools"] == ["WebSearch", "WebFetch"] and "mcp__sandbox__fetch_file" in call["allowed_tools"]
    assert "PreToolUse" in call["hooks"]


async def test_verify_repairs_missing_correction(scripted):
    broken = good_verdict(label="Refuted")
    repaired = good_verdict(label="Refuted", corrected_claim="It is 330 m including antennas.", correction_confidence=0.7,
                            correction_reasoning="official site", sources=good_verdict()["sources"] + [
                                {"url": "https://a.org/p", "title": "A", "quote": "330", "supports": "correction"}])
    runner = scripted([AgentRun(run_id="r", ok=True, structured=broken, cost_usd=0.02),
                       AgentRun(run_id="r2", ok=True, structured=repaired, cost_usd=0.001)])
    v = await verify_claim(CLAIM, runner, VerifyConfig(verify_quotes=False), purpose="test", context="")
    assert "repaired" in v.flags and not v.incomplete and v.corrected_claim
    assert runner.calls[1]["kind"] == "repair" and runner.calls[1]["tools"] == []
    assert v.cost_usd == pytest.approx(0.021)


async def test_verify_flags_incomplete_when_repair_fails(scripted):
    broken = good_verdict(label="Refuted")
    runner = scripted([AgentRun(run_id="r", ok=True, structured=broken), AgentRun(run_id="r2", ok=True, structured=broken)])
    v = await verify_claim(CLAIM, runner, VerifyConfig(verify_quotes=False), purpose="test", context="")
    assert v.incomplete and "incomplete" in v.flags


async def test_verify_failure_returns_nee_with_error(scripted):
    runner = scripted([AgentRun(run_id="r", ok=False, error="timed out after 240s")])
    v = await verify_claim(CLAIM, runner, VerifyConfig(), purpose="test", context="")
    assert v.label == "Not Enough Evidence" and v.error and v.incomplete


async def test_closed_book_has_bash_only(scripted):
    runner = scripted([AgentRun(run_id="r", ok=True, structured=good_verdict(sources=[]))])
    v = await verify_claim(CLAIM, runner, VerifyConfig(web=False, verify_quotes=False), purpose="test", context="")
    assert not v.incomplete
    call = runner.calls[0]
    assert call["tools"] == [] and call["allowed_tools"] == [BASH_TOOL] and call["hooks"] is None


def test_worst_case_reservation():
    assert VerifyConfig(max_searches=2).worst_case_usd() == pytest.approx(0.21)
    assert VerifyConfig(web=False).worst_case_usd() == pytest.approx(0.05)


# ---------------- sources ----------------
def test_normalize_url():
    assert normalize_url("https://www.Example.org/a/?utm_source=x#frag") == normalize_url("http://example.org/a")


def test_quote_matching_tolerates_typography():
    page = "The tower is 330 metres (1,083 ft) tall, about the same height as an 81-storey building."
    assert quote_in_text("The tower is 330 metres (1,083 ft) tall", page)
    assert not quote_in_text("The tower is 500 metres tall and made of gold", page)


def test_mark_in_trace():
    srcs = [VerdictSource(url="https://a.org/p"), VerdictSource(url="https://invented.example/x")]
    mark_in_trace(srcs, [ToolEvent(id="1", name="WebSearch", input={}, output="see https://www.a.org/p/")])
    assert [s.in_trace for s in srcs] == [True, False]


# ---------------- segmentation + cache ----------------
def test_sentences_and_dedup():
    seen: set[str] = set()
    assert len(split_sentences("Dr. Smith said 3.5% of people agree. That is low!")) == 2
    assert new_sentences("A is 1. B is 2.", seen) == ["A is 1.", "B is 2."]
    assert new_sentences("B is 2. C is 3.", seen) == ["C is 3."]
    assert normalize_claim("The GDP grew 3.5%.") == normalize_claim("the gdp  grew 3.5% ")


def test_cache_exact_fuzzy_and_no_failures(tmp_path):
    cache = VerdictCache(tmp_path / "c.sqlite")
    v = Verdict.model_validate(good_verdict())
    cache.put("The Eiffel Tower is 330 m tall.", v)
    assert cache.get("the eiffel tower is 330 m tall") is not None
    assert cache.get("The Eiffel Tower is 330 m tall") is not None
    assert cache.get("The Eiffel Tower is 900 m tall.") is None
    bad = Verdict.model_validate(good_verdict(label="Refuted"))
    bad.incomplete = True
    cache.put("x claim", bad)
    assert cache.get("x claim") is None


async def test_verdict_carries_compact_evidence(scripted):
    out = '{"title":"Fact check: tower","url":"https://a.org/p"} more text'
    trace = [ToolEvent(id="1", name="WebSearch", input={"query": "tower"}, output=out),
             ToolEvent(id="2", name="StructuredOutput", input={}, output="ok")]
    runner = scripted([AgentRun(run_id="r", ok=True, structured=good_verdict(), trace=trace)])
    v = await verify_claim(CLAIM, runner, VerifyConfig(verify_quotes=False), purpose="test", context="")
    assert len(v.evidence) == 1 and v.evidence[0]["titles"] == ["Fact check: tower"]
    assert v.evidence[0]["urls"] == ["a.org/p"]


async def test_structured_output_failure_is_salvaged_from_evidence(scripted):
    trace = [ToolEvent(id="1", name="WebSearch", input={"query": "x"}, output="https://a.org/p evidence")]
    failed = AgentRun(run_id="r", ok=False, error="ResultError: Failed to provide valid structured output after 5 attempts",
                      trace=trace, cost_usd=0.02)
    runner = scripted([failed, AgentRun(run_id="r2", ok=True, structured=good_verdict(), cost_usd=0.001)])
    v = await verify_claim(CLAIM, runner, VerifyConfig(verify_quotes=False), purpose="test", context="")
    assert "salvaged" in v.flags and v.label == "Supported" and not v.error
    assert runner.calls[1]["kind"] == "repair" and runner.calls[1]["tools"] == []


def test_schema_requires_only_core_fields():
    from factcheck.pipeline.prompts import VERDICT_SCHEMA
    assert VERDICT_SCHEMA["required"] == ["label", "confidence", "confidence_reasoning", "explanation"]


async def test_webfetch_hook_refuses_private_targets():
    from factcheck.pipeline.verify import build_hooks
    hooks = build_hooks(VerifyConfig(), {})
    on_fetch = hooks["PreToolUse"][1].hooks[0]
    for url in ("http://127.0.0.1:8765/ws", "http://192.168.1.1/admin", "http://localhost/", "http://[::1]/"):
        out = await on_fetch({"tool_input": {"url": url}}, None, None)
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny", url


def test_normalize_url_survives_broken_ipv6():
    assert normalize_url("http://[2001:db8::1") == "http://[2001:db8::1"
