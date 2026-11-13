"""Extraction coverage: how many of a text's reference claims does claim extraction find? (dev tool, real Haiku)

Local context: `run` extracts claims from one text file several times per prompt variant (extraction only, no
verification, ledger purpose "dev-extract") and saves the claims; `report` scores saved runs against a list of
reference claims (one regex per claim, case-insensitive) and writes a markdown table.
Global context: measured why the app missed claims on a user's long X post (2026-11-13) and that the per-sentence
schema fixes it (reports/exp-00-development.md, issue #27). The input text is not in the repo (third-party post);
the saved claims and scores are in results/dev/extraction-coverage.json.

Variants:
  cap12        the preregistered eval wording ("Return at most 12 claims", free-form list) = the app before #27
  every        free-form list, "Return EVERY check-worthy claim ... do not stop after the main ones"
  every+order  `every` plus "work through the text sentence by sentence" and kinds of statements not to skip
  per-sentence the app since #27: one required schema key per sentence (prompts.per_sentence_schema)

Usage:
  uv run python scripts/extraction_coverage.py run TEXT.txt OUT.json --runs 3 --variants per-sentence
  uv run python scripts/extraction_coverage.py report results/dev/extraction-coverage.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path

import factcheck.pipeline.extract as extract_mod
from factcheck.cli import build_runner
from factcheck.config import PROJECT_ROOT, load_settings
from factcheck.log import setup_logging
from factcheck.models import SourceInfo, TextBlock
from factcheck.pipeline import prompts

# Checkable statements about the world in the measured post (opinions, the author's forecasts and common knowledge
# excluded); one regex each, matched against the extracted claim texts.
REFERENCE = {
    "METR released the research": r"METR.*(releas|report|research)|(releas|report|research).*METR",
    "70% speed-up finding": r"70 ?%",
    "METR is a nonprofit": r"non-?profit",
    "AI Futures Project modeling": r"AI Futures|models? of AI development",
    "many incidents in six months": r"six months|6 months",
    "Hugging Face hack, ~1200 agents": r"1,?200",
    "cyberattack during the hack": r"cyber",
    "developers can't instill values": r"human values|instill",
    "author's nine years / since 23": r"nine years|9 years|since (age )?23",
}
EVERY = ("Return EVERY check-worthy claim in the text, from every block: there is no limit, so do not stop after the "
         "main ones (a long text can have dozens). List the most important first.")
ORDER = ("Return EVERY check-worthy claim in the text, from every block: there is no limit, so do not stop after the "
         "main ones (a long text can have dozens). Work through the text sentence by sentence, in order, and for each "
         "sentence output every check-worthy statement it makes before moving on. Do not skip: findings attributed to "
         "a study, report or organisation; what an organisation or person is or did; each detail of a named event "
         "(who, how many, what happened); numbers, percentages, dates and durations. List the most important first.")


def _free_form(ask: str):
    """Prompt builder for the free-form variants: the eval layout with `ask` instead of the cap sentence."""
    def build(blocks, context, _max_claims):
        return prompts.build_extract_prompt(blocks, context, 12).replace("Return at most 12 claims.", ask)
    return build


async def _extract(text: str, variant: str, runner) -> list[str]:
    source = SourceInfo(kind="chrome", source_id="dev:coverage", app="chrome", title="measured text")
    blocks = [TextBlock(id="b1", text=text)]                       # one block, as X renders a long post
    original = extract_mod.build_extract_prompt
    try:
        if variant in ("every", "every+order"):
            extract_mod.build_extract_prompt = _free_form(EVERY if variant == "every" else ORDER)
        cap = 12 if variant == "cap12" else (99 if variant in ("every", "every+order") else None)
        claims = await extract_mod.extract_claims(blocks, source, runner, purpose="dev-extract", max_claims=cap)
    finally:
        extract_mod.build_extract_prompt = original
    return [c.text for c in claims]


def run(text_path: Path, out: Path, runs: int, variants: list[str]) -> None:
    settings = load_settings()
    setup_logging(settings.logs_dir, name="dev-extraction-coverage")
    runner, ledger = build_runner(settings)
    text, before = text_path.read_text(encoding="utf-8"), ledger.spent_total()
    data = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {"runs": []}

    async def main():
        for variant in variants:
            for _ in range(runs):
                data["runs"].append({"variant": variant, "claims": await _extract(text, variant, runner)})
    asyncio.run(main())
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"saved {out}; spent ${ledger.spent_total() - before:.4f}")


def report(results: Path, out: Path) -> str:
    data = json.loads(results.read_text(encoding="utf-8"))
    rows, by_variant = [], {}
    for r in data["runs"]:
        found = [k for k, rx in REFERENCE.items() if any(re.search(rx, c, re.IGNORECASE) for c in r["claims"])]
        by_variant.setdefault(r["variant"], []).append((len(r["claims"]), len(found),
                                                         [k for k in REFERENCE if k not in found]))
    for variant, runs in by_variant.items():
        recalls = ", ".join(str(f) for _, f, _ in runs)
        claims = ", ".join(str(n) for n, _, _ in runs)
        missed = sorted({m for _, _, ms in runs for m in ms})
        rows.append(f"| `{variant}` | {len(runs)} | {claims} | {recalls} (of {len(REFERENCE)}) | {'; '.join(missed) or '-'} |")
    md = ("<!-- generated by scripts/extraction_coverage.py report; do not edit -->\n"
          "| variant | runs | claims per run | reference claims found per run | missed in some run |\n"
          "|---|---|---|---|---|\n" + "\n".join(rows) + "\n")
    out.write_text(md, encoding="utf-8")
    return md


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("text", type=Path)
    r.add_argument("out", type=Path)
    r.add_argument("--runs", type=int, default=3)
    r.add_argument("--variants", nargs="+", default=["cap12", "every", "every+order", "per-sentence"])
    g = sub.add_parser("report")
    g.add_argument("results", type=Path)
    g.add_argument("--out", type=Path, default=PROJECT_ROOT / "reports" / "generated" / "extraction-coverage.md")
    a = p.parse_args()
    if a.cmd == "run":
        run(a.text, a.out, a.runs, a.variants)
    else:
        print(report(a.results, a.out))
