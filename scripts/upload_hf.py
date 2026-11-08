"""Publish the per-claim evaluation results as a Hugging Face dataset inside this project's collection.

Usage: uv run python scripts/upload_hf.py
Local context: copies results/ into a staging folder (dropping `evidence[].output_head`, i.e. raw excerpts of
third-party web pages, keeping URLs and titles), writes the dataset card, uploads, and adds the dataset to the
collection "Fact-checking what you see". Requires `hf auth login`.
huggingface_hub API: https://huggingface.co/docs/huggingface_hub/guides/upload ,
collections: https://huggingface.co/docs/huggingface_hub/guides/collections
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from huggingface_hub import HfApi

ROOT = Path(__file__).resolve().parents[1]
REPO = "BurnyCoder/fact-check-what-you-see-results"
COLLECTION_TITLE = "Fact-checking what you see"
GITHUB = "https://github.com/BurnyCoder/fact-check-websites-apps"

CARD = f"""---
license: cc-by-nc-4.0
language: [en]
task_categories: [text-classification]
tags: [fact-checking, claim-verification, averitec, checkthat, llm-agents, calibration]
pretty_name: Fact-checking what you see — evaluation results
configs:
- config_name: confirmatory
  data_files:
  - split: closed_book
    path: confirmatory/closed_book.jsonl
  - split: web_blocked
    path: confirmatory/web_blocked.jsonl
  - split: web_default
    path: confirmatory/web_default.jsonl
- config_name: h3_ocr_channel
  data_files:
  - split: clean
    path: h3/h3_clean.jsonl
  - split: ocr
    path: h3/h3_ocr.jsonl
- config_name: h4_checkworthiness
  data_files:
  - split: test
    path: h4-test/extractor.jsonl
---

# Fact-checking what you see — evaluation results

Per-claim outputs of the preregistered evaluation of a Claude Haiku 5.5 fact-checking agent (web search, page fetch,
raw-data downloads, sandboxed bash) from the project [{GITHUB}]({GITHUB}).

* `confirmatory/` — 120 AVeriTeC dev claims × 3 arms (closed-book; web with fact-check sites blocked; web default):
  predicted label, stated confidence and its reasoning, explanation, correction, sources (with quote checks), cost,
  tool counts, and the URLs/titles the agent saw (raw page excerpts removed).
* `h3/` — 500 AVeriTeC dev claims: clean text vs rendered-page → Windows OCR, both extracted and verified closed-book.
* `h4-test/` — CheckThat! 2024 Task 1 English test: per-sentence check-worthiness predictions of the zero-shot extractor.
* `e2/`, `e3/`, `pilot/` — run-to-run variance repeats, correction-faithfulness judgements, pilot on AVeriTeC train.
* `summary.json` — every metric reported in the paper (regenerate with `uv run python -m eval.report`).

Hypotheses and analysis plan: [preregistration]({GITHUB}/blob/main/reports/preregistration.md). Results and paper:
[README]({GITHUB}#results-preregistered-evaluation).

**Licence and attribution.** Claims and gold labels come from AVeriTeC (Schlichtkrull et al. 2023,
https://arxiv.org/abs/2305.13117; CC-BY-NC-4.0) and CLEF CheckThat! 2024 (https://checkthat.gitlab.io/clef2024/task1);
this derived dataset is released under CC-BY-NC-4.0. Model outputs were produced by Claude Haiku 5.5.
"""


def strip_excerpts(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    with src.open(encoding="utf-8") as fin, dst.open("w", encoding="utf-8") as fout:
        for line in fin:
            if not line.strip():
                continue
            rec = json.loads(line)
            for ev in (rec.get("verdict") or {}).get("evidence", []):
                ev.pop("output_head", None)
            fout.write(json.dumps(rec, ensure_ascii=False) + "\n")


def main() -> None:
    api = HfApi()
    stage = Path(tempfile.mkdtemp(prefix="hf-stage-"))
    for exp in ("confirmatory", "h3", "h4-test", "e2", "e3", "pilot"):
        for f in sorted((ROOT / "results" / exp).glob("*.jsonl")):
            strip_excerpts(f, stage / exp / f.name)
    shutil.copy(ROOT / "results" / "summary.json", stage / "summary.json")
    shutil.copy(ROOT / "results" / "samples" / "dev_web_sample.json", stage / "dev_web_sample.json")
    (stage / "README.md").write_text(CARD, encoding="utf-8")
    api.create_repo(REPO, repo_type="dataset", exist_ok=True)
    api.upload_folder(folder_path=str(stage), repo_id=REPO, repo_type="dataset",
                      commit_message="Upload evaluation results (preregistered, prereg-v1)")
    collection = next((c for c in api.list_collections(owner="BurnyCoder", limit=100) if c.title == COLLECTION_TITLE), None)
    if collection is None:
        collection = api.create_collection(COLLECTION_TITLE, description="Fact-checking claims seen in browsers and desktop "
                                           "apps with a tool-using Claude Haiku agent: results, data and paper.", exists_ok=True)
    api.add_collection_item(collection.slug, item_id=REPO, item_type="dataset", exists_ok=True)
    print("dataset:", f"https://huggingface.co/datasets/{REPO}")
    print("collection:", f"https://huggingface.co/collections/{collection.slug}")


if __name__ == "__main__":
    main()
