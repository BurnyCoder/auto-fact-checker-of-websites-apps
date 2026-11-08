"""Resumable, budget-aware benchmark runner.

Usage:
  uv run python -m eval.run pilot                 # 25 AVeriTeC *train* claims x {closed_book, web_blocked, web_default}
  uv run python -m eval.run confirmatory          # preregistered dev sample x the same 3 arms (H1, H2)
  uv run python -m eval.run h3                    # all 500 dev claims x {h3_clean, h3_ocr} (closed-book)
  uv run python -m eval.run h4 [--split test|dev] # CheckThat! 2024 check-worthiness with the product extractor
  uv run python -m eval.run e2                    # repeats for run-to-run variance
  uv run python -m eval.run <exp> --smoke 5       # smoke test: 5 *train* items (CheckThat! dev for h4), results/smoke/
Local context: builds (item, condition) tasks, shuffles them (conditions interleaved), runs 4 at a time,
appends one JSON line per finished task to results/<exp>/<condition>.jsonl, skips tasks already present
(resume after a crash or reboot), and stops cleanly when the budget ledger refuses a call.
Global context: results/ is the single source for eval/report.py; every number in the reports and the
paper is recomputed from these files.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import time
from datetime import datetime
from importlib.metadata import version
from pathlib import Path

from rapidfuzz import fuzz
from rapidfuzz.distance import Levenshtein

from eval.conditions import config_for
from eval.datasets import AveritecClaim, load_averitec, load_checkthat
from eval.render import design, ideal_blocks, screenshot_bgra
from factcheck.capture.layout import group_lines
from factcheck.capture.ocr import ocr_bgra
from factcheck.config import load_settings
from factcheck.log import setup_logging
from factcheck.models import Claim, SourceInfo, TextBlock
from factcheck.pipeline.agent import AgentRunner
from factcheck.pipeline.budget import BudgetExceeded, BudgetLedger
from factcheck.pipeline.extract import extract_claims
from factcheck.pipeline.prompts import context_lines
from factcheck.pipeline.verify import verify_claim

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "results"
SAMPLE_FILE = RESULTS / "samples" / "dev_web_sample.json"
log = logging.getLogger("eval.run")
SDK_VERSION = version("claude-agent-sdk")


def iso_date(ddmmyyyy: str | None) -> str | None:
    """AVeriTeC dates are DD-MM-YYYY; give the model unambiguous ISO dates."""
    if not ddmmyyyy:
        return None
    try:
        return datetime.strptime(ddmmyyyy, "%d-%m-%Y").date().isoformat()
    except ValueError:
        return ddmmyyyy


def claim_context(c: AveritecClaim) -> str:
    return context_lines(extra={"Speaker": c.speaker or "", "Claim date": iso_date(c.claim_date) or "",
                                "Where it appeared": c.reporting_source or "", "Location": c.location or ""})


def verdict_record(v, gold: str | None) -> dict:
    return {"pred": v.label, "confidence": v.confidence, "correct": (v.label == gold) if gold else None,
            "cost_usd": v.cost_usd, "duration_s": v.duration_s, "web_searches": v.web_searches,
            "tool_counts": v.tool_counts, "flags": v.flags, "error": v.error, "auth": v.auth,
            "verdict": v.model_dump(mode="json")}


# ------------------------------------------------------------------------------------------------ task bodies
async def verify_task(runner, item: AveritecClaim, condition: str, purpose: str) -> dict:
    cfg = config_for(condition, iso_date(item.claim_date))
    claim = Claim(id=item.item_id, text=item.claim, span=item.claim, block_id="b0",
                  source=SourceInfo(kind="eval", source_id=item.item_id))
    v = await verify_claim(claim, runner, cfg, purpose=purpose, context=claim_context(item))
    return {"gold": item.label, **verdict_record(v, item.label)}


def text_errors(reference: str, blocks: list[TextBlock]) -> dict:
    """CER/WER of the OCR'd text that best matches the reference claim (character / word Levenshtein)."""
    best = max(blocks, key=lambda b: fuzz.partial_ratio(reference, b.text), default=None)
    if best is None:
        return {"cer": 1.0, "wer": 1.0, "ocr_text": ""}
    hit = fuzz.partial_ratio_alignment(reference, best.text)
    hyp = best.text[hit.dest_start:hit.dest_end] if hit else best.text
    return {"cer": Levenshtein.distance(reference, hyp) / max(1, len(reference)),
            "wer": Levenshtein.distance(reference.split(), hyp.split()) / max(1, len(reference.split())),
            "ocr_text": hyp}


async def h3_task(runner, item: AveritecClaim, condition: str, purpose: str, cell: tuple[str, int], page) -> dict:
    layout, font_px = cell
    index = int(item.item_id.split("-")[1])
    if condition == "h3_clean":
        blocks, errs = ideal_blocks(index, item.claim, layout), {"cer": 0.0, "wer": 0.0, "ocr_text": item.claim}
    else:
        pixels = await screenshot_bgra(page, index, item.claim, layout, font_px)
        blocks = group_lines(await ocr_bgra(pixels), prefix="o")
        errs = text_errors(item.claim, blocks)
    source = SourceInfo(kind="eval", source_id=f"{item.item_id}-{condition}", app="chat" if layout == "chat" else "browser",
                        title="#general" if layout == "chat" else "Morning briefing")
    claims = await extract_claims(blocks, source, runner, purpose=f"{purpose}-extract")
    base = {"gold": item.label, "layout": layout, "font_px": font_px, **errs, "n_blocks": len(blocks),
            "extracted": [c.text for c in claims]}
    if not claims:                                       # preregistered: failed extraction counts as wrong
        return {**base, "pred": None, "confidence": 0.0, "correct": False, "extraction_failed": True, "cost_usd": 0.0}
    chosen = max(claims, key=lambda c: fuzz.token_sort_ratio(c.text, item.claim))
    cfg = config_for(condition, iso_date(item.claim_date))
    v = await verify_claim(chosen, runner, cfg, purpose=purpose, context=claim_context(item))
    return {**base, "chosen_claim": chosen.text, "extraction_failed": False, **verdict_record(v, item.label)}


async def h4_task(runner, batch: list, purpose: str) -> list[dict]:
    blocks = [TextBlock(id=s.sentence_id, text=s.text) for s in batch]
    source = SourceInfo(kind="eval", source_id=f"ct-{batch[0].sentence_id}", app="transcript",
                        title="US presidential debate transcript")
    claims = await extract_claims(blocks, source, runner, purpose=purpose, max_claims=2 * len(blocks))
    positive = {c.block_id for c in claims}
    return [{"item_id": s.sentence_id, "gold": s.label, "pred": s.sentence_id in positive,
             "claims": [c.text for c in claims if c.block_id == s.sentence_id]} for s in batch]


# ------------------------------------------------------------------------------------------------ plumbing
def done_ids(path: Path) -> set[tuple[str, int]]:
    if not path.exists():
        return set()
    return {(r["item_id"], r.get("repeat", 1)) for r in map(json.loads, path.read_text(encoding="utf-8").splitlines()) if r}


def append(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def write_progress(exp_dir: Path, ledger: BudgetLedger, totals: dict[str, int]) -> None:
    """Small summary for eval/dashboard.html (running accuracy per condition + spend)."""
    conds = {}
    for cond, total in totals.items():
        f = exp_dir / f"{cond}.jsonl"
        rows = [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines()] if f.exists() else []
        scored = [r for r in rows if r.get("correct") is not None]
        k, n = sum(bool(r["correct"]) for r in scored), len(scored)
        conds[cond] = {"done": len(rows), "total": total, "correct": k, "scored": n,
                       "cost_usd": round(sum(r.get("cost_usd") or 0 for r in rows), 4)}
    (exp_dir / "progress.json").write_text(json.dumps({"updated": datetime.now().isoformat(timespec="seconds"),
                                                       "spent_total_usd": ledger.spent_total(),
                                                       "budget_usd": ledger.total_cap, "conditions": conds}, indent=1))


def sample_items(exp: str, smoke: int | None) -> list[AveritecClaim]:
    if smoke:                                            # smoke tests never touch the held-out dev set
        return random.Random(99).sample(load_averitec("train"), smoke)
    if exp == "pilot":
        items = random.Random(7).sample(load_averitec("train"), 25)
    elif exp in ("confirmatory", "e2"):
        ids = json.loads(SAMPLE_FILE.read_text())["item_ids"]
        by_id = {c.item_id: c for c in load_averitec("dev")}
        items = [by_id[i] for i in ids]
    elif exp == "h3":
        items = load_averitec("dev")
    else:
        raise ValueError(exp)
    return items


async def run_experiment(exp: str, smoke: int | None, concurrency: int, split: str) -> None:
    settings = load_settings()
    setup_logging(settings.logs_dir, name=f"eval-{exp}", echo_llm=False)
    ledger = BudgetLedger(settings.ledger_path, settings.total_budget_usd)
    runner = AgentRunner(settings, ledger)
    if smoke and exp == "h4":
        split = "dev"                                    # smoke tests never touch the CheckThat! test set
    name = f"{exp}-{split}" if exp == "h4" else exp
    exp_dir = RESULTS / "smoke" / name if smoke else RESULTS / name
    sem = asyncio.Semaphore(concurrency)
    stop = asyncio.Event()
    meta = {"experiment": exp, "sdk_version": SDK_VERSION, "model": settings.model, "started": datetime.now().isoformat()}
    append(exp_dir / "runs.jsonl", meta)

    if exp == "h4":
        sentences = load_checkthat(split)[: smoke or None]
        batches = [sentences[i:i + 15] for i in range(0, len(sentences), 15)]
        out = exp_dir / "extractor.jsonl"
        done = {i for i, _ in done_ids(out)}
        todo = [b for b in batches if not all(s.sentence_id in done for s in b)]

        async def run_batch(batch):
            async with sem:
                if stop.is_set():
                    return
                try:
                    for rec in await h4_task(runner, batch, purpose=f"eval-h4-{split}"):
                        append(out, {**rec, "sdk_version": SDK_VERSION})
                except BudgetExceeded as exc:
                    log.error("budget stop: %s", exc)
                    stop.set()

        await asyncio.gather(*(run_batch(b) for b in todo))
        log.info("h4 done; ledger $%.4f", ledger.spent_total())
        return

    items = sample_items(exp, smoke)
    if exp == "h3":
        conditions, repeats = ["h3_clean", "h3_ocr"], [1]
    elif exp == "e2":
        conditions, repeats = ["closed_book"], [2, 3]
        items = items[:100]
    else:
        conditions, repeats = ["closed_book", "web_blocked", "web_default"], [1]
    cells = design([c.item_id for c in items]) if exp == "h3" else {}
    done = {cond: done_ids(exp_dir / f"{cond}.jsonl") for cond in conditions}
    tasks = [(it, cond, rep) for it in items for cond in conditions for rep in repeats
             if (it.item_id, rep) not in done[cond]]
    random.Random(11).shuffle(tasks)                      # interleave conditions (time-of-day / load confounds)
    totals = {cond: len(items) * len(repeats) for cond in conditions}
    log.info("%s: %d tasks to run (%d items x %s), ledger $%.4f", exp, len(tasks), len(items), conditions,
             ledger.spent_total())

    pw = browser = None
    if exp == "h3":
        from playwright.async_api import async_playwright
        pw = await async_playwright().start()
        browser = await pw.chromium.launch()
        page_lock = asyncio.Lock()

    async def run_one(item, cond, rep):
        async with sem:
            if stop.is_set():
                return
            purpose = f"{'pilot' if exp == 'pilot' else 'eval'}-{exp}-{cond}"
            t0 = time.monotonic()
            try:
                if exp == "h3":
                    async with page_lock:                 # one renderer page; OCR is fast
                        pg = await browser.new_page(viewport={"width": 900, "height": 400}, device_scale_factor=1)
                    try:
                        rec = await h3_task(runner, item, cond, purpose, cells[item.item_id], pg)
                    finally:
                        await pg.close()
                else:
                    rec = await verify_task(runner, item, cond, purpose)
            except BudgetExceeded as exc:
                log.error("budget stop: %s", exc)
                stop.set()
                return
            append(exp_dir / f"{cond}.jsonl", {"item_id": item.item_id, "condition": cond, "repeat": rep,
                                               "sdk_version": SDK_VERSION, "wall_s": round(time.monotonic() - t0, 1),
                                               "ts": datetime.now().isoformat(timespec="seconds"), **rec})
            write_progress(exp_dir, ledger, totals)
            log.info("[%s] %s %s -> %s (gold %s) $%.4f | ledger $%.4f", exp, item.item_id, cond, rec.get("pred"),
                     rec.get("gold"), rec.get("cost_usd") or 0, ledger.spent_total())

    try:
        await asyncio.gather(*(run_one(*t) for t in tasks))
    finally:
        if browser is not None:
            await browser.close()
            await pw.stop()
    write_progress(exp_dir, ledger, totals)
    log.info("%s finished; ledger $%.4f", exp, ledger.spent_total())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("experiment", choices=["pilot", "confirmatory", "h3", "h4", "e2"])
    ap.add_argument("--smoke", type=int, default=None, help="run N train items into results/smoke/")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--split", default="test", help="CheckThat! split for h4 (dev for prompt development)")
    args = ap.parse_args()
    asyncio.run(run_experiment(args.experiment, args.smoke, args.concurrency, args.split))


if __name__ == "__main__":
    main()
