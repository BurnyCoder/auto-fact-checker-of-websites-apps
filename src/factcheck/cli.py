"""Command-line entry points: check a claim or a piece of text from the terminal.

Local context: `factcheck-cli claim "..."` verifies one claim; `factcheck-cli text "..."` extracts claims
from a paragraph and verifies each; `factcheck-cli budget` prints spend so far.
Global context: a thin wrapper over the same pipeline the app uses (extract.py, verify.py), handy for
development and for scripting. Every call is recorded in budget_ledger.jsonl.
"""

from __future__ import annotations

import argparse
import asyncio
import json

from factcheck.config import load_settings
from factcheck.log import setup_logging
from factcheck.models import Claim, SourceInfo, TextBlock
from factcheck.pipeline.agent import AgentRunner
from factcheck.pipeline.budget import BudgetLedger
from factcheck.pipeline.extract import extract_claims
from factcheck.pipeline.fake import FakeRunner
from factcheck.pipeline.prompts import context_lines
from factcheck.pipeline.verify import VerifyConfig, verify_claim


def build_runner(settings):
    ledger = BudgetLedger(settings.ledger_path, settings.total_budget_usd, settings.daily_app_budget_usd)
    runner = FakeRunner(settings) if settings.factcheck_fake_llm else AgentRunner(settings, ledger)
    return runner, ledger


async def _check_claims(claims: list[Claim], runner, settings, purpose: str, keep: bool) -> None:
    cfg = VerifyConfig(max_searches=settings.max_web_searches, max_fetches=settings.max_web_fetches,
                       max_turns=settings.max_turns, keep_work=keep, timeout_s=settings.verify_timeout_s)
    for claim in claims:
        verdict = await verify_claim(claim, runner, cfg, purpose=purpose, context=context_lines(app="cli"))
        print(json.dumps(verdict.model_dump(), indent=2, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser(prog="factcheck-cli", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_claim = sub.add_parser("claim", help="verify one claim")
    p_claim.add_argument("text")
    p_text = sub.add_parser("text", help="extract claims from text, then verify each")
    p_text.add_argument("text")
    for p in (p_claim, p_text):
        p.add_argument("--purpose", default="dev", help="ledger purpose tag (dev, test, ...)")
        p.add_argument("--keep-work", action="store_true", help="keep the /work folder for inspection")
    sub.add_parser("budget", help="show spend so far")
    args = parser.parse_args()

    settings = load_settings()
    setup_logging(settings.logs_dir, name="cli")
    runner, ledger = build_runner(settings)
    if args.cmd == "budget":
        print(f"spent ${ledger.spent_total():.4f} of ${settings.total_budget_usd:.2f} (API-equivalent)")
        return
    source = SourceInfo(kind="manual", source_id="cli", app="cli")
    if args.cmd == "claim":
        claims = [Claim(id="cli", text=args.text, span=args.text, block_id="b0", source=source)]
    else:
        claims = asyncio.run(extract_claims([TextBlock(id="b0", text=args.text)], source, runner, purpose=args.purpose))
        print(f"extracted {len(claims)} claims: {[c.text for c in claims]}")
    asyncio.run(_check_claims(claims, runner, settings, args.purpose, args.keep_work))
    print(f"total spent so far: ${ledger.spent_total():.4f} of ${settings.total_budget_usd:.2f}")


if __name__ == "__main__":
    main()
