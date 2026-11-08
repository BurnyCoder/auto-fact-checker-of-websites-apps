# Preregistration: does a Haiku agent with web search, downloads and a sandbox fact-check better than its memory, and does reading the screen through OCR cost accuracy?

Author: Libor "Burny" Burian · Written: 2026-11-08 · Frozen at git tag `prereg-v1`, **before** any confirmatory run.
Exploratory work done before freezing: prompt development and smoke tests on AVeriTeC *train* and CheckThat! *dev*,
and a 25-claim pilot on AVeriTeC *train* (`results/pilot/`). No AVeriTeC *dev* claim and no CheckThat! *test*
sentence has been sent to a model before this document was frozen.

## 1. System under test

The product pipeline in this repository at tag `prereg-v1` (Claude Agent SDK 0.2.179 / bundled Claude Code CLI;
model `claude-haiku-5-5`; prompts in `src/factcheck/pipeline/prompts.py`). Verifier tools: built-in WebSearch
(≤ 2 calls per claim, enforced by a PreToolUse hook) and WebFetch (≤ 3), `fetch_file` (downloads into a per-claim
`/work`), and `bash` in an offline bubblewrap sandbox; `max_turns = 12`; `effort = low`. Each claim is verified in a
fresh, isolated agent session (no cache). The model is given the claim, its date (ISO), speaker, where it appeared and
location, and is told to judge the claim as of its date. It is never given the fact-check article URL or the gold
justification.

## 2. Data

* **AVeriTeC** ([paper](https://arxiv.org/abs/2305.13117), [files](https://github.com/MichSchli/AVeriTeC/tree/main/data); CC-BY-NC-4.0): dev = 500 claims
  (Refuted 305, Supported 122, Conflicting 38, Not Enough Evidence 35). Dev serves as our held-out test set (the official
  test labels are not used).
  * **Web sample for H1/H2**: a label-stratified random sample of the dev set of size **N_web** (§ 5), drawn with
    `numpy.random.default_rng(2026)` proportionally to the dev label distribution; ids are committed in
    `results/samples/dev_web_sample.json` together with this document.
  * **H3** uses all 500 dev claims.
* **CLEF CheckThat! 2024 Task 1, English** ([task](https://checkthat.gitlab.io/clef2024/task1), [files](https://gitlab.com/checkthat_lab/clef2024-checkthat-lab/-/tree/main/task1/data)): the 341-sentence
  test set with released gold labels.

## 3. Conditions (arms)

| Arm | Tools | URL policy |
|---|---|---|
| `closed_book` | sandboxed `bash` only | – |
| `web_blocked` | + WebSearch, WebFetch, `fetch_file` | **Leakage block** + **fact-check block** |
| `web_default` | + WebSearch, WebFetch, `fetch_file` | **Leakage block** only (= the product, minus the leakage block) |
| `h3_clean` | extractor → `closed_book` verifier | text given as ideal blocks (one per page element) |
| `h3_ocr` | extractor → `closed_book` verifier | the same page rendered by headless Chromium, read by Windows OCR + our line grouping |

* **Leakage block** (all web arms): domains huggingface.co, hf.co, github.com, githubusercontent.com, kaggle.com, arxiv.org,
  aclanthology.org, fever.ai, paperswithcode.com, semanticscholar.org; any URL containing `averitec` or `checkthat`;
  archive copies of blocked domains. Enforced on WebSearch (`blocked_domains` injected by hook), WebFetch (hook) and
  `fetch_file` (our code). Implementation: `eval/conditions.py`.
* **Fact-check block** (`web_blocked` only): all 47 publishers of AVeriTeC train+dev fact-check articles (whole domains,
  which also removes those outlets' ordinary news — a conservative choice), 10 further well-known fact-checking
  outlets, and any URL whose path contains `fact-check`, `factcheck` or `fact_check`, including archive copies.
* **Deviation from the original plan (decided before any H3 data):** pages for H3 are rendered with headless Chromium
  (viewport 900 px, device scale factor 1) instead of in a real Chrome window on the author's monitors, because screen
  access was declined. Rendering design: 2 layouts (generic chat message with username + timestamp; article with title,
  byline, paragraph) × 3 font sizes (13, 15, 18 CSS px), balanced by seeded shuffle (`eval/render.py`, seed 2026).
  The OCR arm's text goes through the production extractor; if it returns several claims, the one with the highest
  `token_sort_ratio` to the original claim is verified; if it returns none, the item counts as **wrong**. The clean
  arm uses the identical procedure on ideal blocks.

## 4. Hypotheses, primary metrics and tests

Accuracy on the four AVeriTeC labels is primary (macro-F1 would be dominated by noise from the ~8 NEE and ~8
Conflicting claims in a sample of ~100). All tests use α = 0.05 with Holm–Bonferroni correction across H1, H2, H4
(H3 is a non-inferiority test with its own decision rule and is reported alongside).

| | Hypothesis | Comparison | Primary test | Falsified / not supported if |
|---|---|---|---|---|
| **H1** (primary) | Retrieval improves accuracy beyond the model's memory when direct fact-checks are unavailable | `web_blocked` vs `closed_book`, paired on the N_web sample | one-sided exact McNemar (binomial test on discordant pairs) | Holm-adjusted p ≥ 0.05, or accuracy difference ≤ 0 |
| **H2** | Access to existing online fact-checks raises accuracy further | `web_default` vs `web_blocked`, paired | same | same |
| **H3** | Reading the claim through rendering + Windows OCR is not worse than clean text | `h3_ocr` vs `h3_clean`, paired, n = 500 | paired bootstrap (10,000 resamples, seed 0) of the accuracy difference; **non-inferiority margin δ = 0.03** | the one-sided 95 % lower bound of (ocr − clean) is below −0.03 |
| **H4** | The product extractor detects check-worthy sentences better than simple baselines | F1 of the check-worthy class on CheckThat! test, vs all-positive, stratified-random (1,000 seeded draws) and a number/quantity-word heuristic (`eval/report.py::heuristic_checkworthy`) | one-sided approximate-randomisation test (10,000 swaps) vs the best of the all-positive and heuristic baselines | Holm-adjusted p ≥ 0.05 |

H4 scoring: sentences are sent in batches of 15 with the context "US presidential debate transcript"; a sentence is
predicted check-worthy if the extractor returns at least one claim whose span lies in it.

## 5. Sample size (from the pilot)

<!-- GEN:power -->
Pilot: 25 AVeriTeC *train* claims × 3 arms (`results/pilot/`). Measured cost per claim: closed_book $0.0009, web_blocked $0.0222, web_default $0.0211. Budget reserved for H3/H4/E2/E3: $1.80; remaining eval budget buys **N_web = 120** claims × 3 arms ($0.0442 per claim).

| Comparison | pilot P(A only) | pilot P(B only) | power at N_web if the pilot effect is real | MDE at N_web (80 % power) |
|---|---|---|---|---|
| H1 web_blocked vs closed_book | 0.28 | 0.24 | 0.12 | 17.5 accuracy points |
| H2 web_default vs web_blocked | 0.24 | 0.08 | 0.92 | 13.5 accuracy points |

Effects smaller than the MDE will be reported as inconclusive, not as evidence of no effect. Generated by `uv run python -m eval.power --freeze` from `results/power.json`.
<!-- /GEN:power -->

## 6. Pre-specified secondary analyses (descriptive, no correction)

* Per arm: accuracy and macro-F1 with 95 % bootstrap CIs; per-class P/R/F1; confusion matrices; 3-class accuracy
  (NEE + Conflicting merged); accuracy on the Supported-vs-Refuted subset; predicted-label distribution.
* Paired bootstrap CIs of the macro-F1 difference for H1/H2.
* Calibration of `confidence`: Brier score, ECE with 10 equal-mass bins, reliability diagram, selective accuracy at
  coverages 100/90/80/70/60/50 %.
* Cost per claim, latency p50/p95, searches per claim, sources per claim, share of sources found in the agent's own
  tool results, share of quotes checked and verified on the live page, sandbox and `fetch_file` use rates and accuracy
  when the sandbox was used.
* H3: CER/WER of the OCR'd claim text, extraction failure rate per arm, accuracy by layout × font size.
* **Leakage audit**: for each web-arm claim, flag if any tool result title/URL/output head mentions `averitec`/`checkthat`,
  if any result title or URL contains a fact-check term (`fact-check`, `factcheck`, `fact check`, `fact checked`), and
  (blocked arm) if any result URL belongs to a blocked domain. H1 and H2 are **re-run excluding all flagged claims** as a
  sensitivity analysis; both versions are reported.
* **E2 (exploratory)**: run-to-run variance — `closed_book` repeated twice more on the first 100 sampled claims;
  Fleiss' κ across the 3 runs, SD of accuracy, share of claims with identical labels in all runs.
* **E3 (exploratory)**: for every claim predicted Refuted or Conflicting in `web_default`, a tool-less Haiku judge
  compares the corrected claim with AVeriTeC's gold justification (agrees / partially / disagrees); 20 judgements are
  read by hand. Same-model-family judging is a stated limitation.

## 7. What would change our conclusions / threats

* Haiku 5.5 (training cutoff June 2026) has likely seen many of these 2020–2022 fact-checks; `closed_book` measures this
  memorisation, and H1 is therefore a conservative test of retrieval.
* Syndicated copies of fact-checks on unblocked sites can leak into `web_blocked`; the audit measures it.
* Label-only accuracy is **not** comparable with the official AVeriTeC score (which also scores retrieved evidence).
* Budget: the whole project is capped at $10 API-equivalent; runs stop when the ledger refuses a call. If a run is cut
  short, the analysis uses the claims completed in all arms of a comparison (paired), and the shortfall is reported.
* No temperature/seed control in the SDK: E2 quantifies run-to-run variance.
* Any analysis not listed here is labelled exploratory.
