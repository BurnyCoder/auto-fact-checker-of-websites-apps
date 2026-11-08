# Literature review: fact-checking what a user sees on screen

Author: Libor "Burny" Burian · Date: 2026-11-08 · Status: written before any experiment was run.

This review covers what exists for (1) automated claim verification pipelines, (2) check-worthiness
detection, (3) browser/desktop fact-checking tools, (4) reading text from the screen on Windows,
(5) running LLM-written code safely, (6) LLM confidence calibration, and (7) significance testing.
For each item we say what we reuse. "Verified" means we read the primary source (paper, docs or
installed code); "secondary" means the detail comes from a third-party summary and is flagged.

## 1. Claim verification pipelines

| Work | What it does | What we take |
|---|---|---|
| **Loki / OpenFactVerification** — Li et al. 2024, [arXiv 2410.01794](https://arxiv.org/abs/2410.01794), [code](https://github.com/Libr-AI/OpenFactVerification) | Five steps: decompose long text into claims → check-worthiness → query generation → evidence retrieval → verification. Modular LLM backends (OpenAI, Anthropic, local). | Our pipeline keeps the decomposition + check-worthiness steps, and collapses query → retrieve → verify into one tool-using agent. |
| **SAFE** (Search-Augmented Factuality Evaluator) — Wei et al., *Long-form factuality in large language models*, NeurIPS 2024, [arXiv 2403.18802](https://arxiv.org/abs/2403.18802), [code](https://github.com/google-deepmind/long-form-factuality) | An LLM splits a response into individual facts and rates each with a multi-step loop of Google Search queries; reported >20× cheaper than human annotators. | The "atomic, self-contained fact" decomposition idea and the multi-step search-then-reason loop. |
| **Factcheck-Bench** — Wang et al., Findings of EMNLP 2024, [arXiv 2311.09000](https://arxiv.org/abs/2311.09000), [code](https://github.com/yuxiaw/Factcheck-GPT) | Fine-grained benchmark annotating 8 steps of detection + correction at claim, sentence and document level. Reports that FacTool, FactScore and Perplexity.ai struggle to flag false claims; best F1 0.63 (GPT-4-based). | Sets expectations: false-claim detection is hard even for strong systems; motivates also *correcting* refuted claims. |
| **AVeriTeC** — Schlichtkrull et al. 2023, [arXiv 2305.13117](https://arxiv.org/abs/2305.13117); shared task 2024, [arXiv 2410.23850](https://arxiv.org/abs/2410.23850); FEVER-8 / 2025, [ACL 2025.fever-1.15](https://aclanthology.org/2025.fever-1.15) | Real-world claims from fact-checking articles with four labels: Supported, Refuted, Not Enough Evidence, Conflicting Evidence/Cherrypicking. 3,068 train / 500 dev / 1,000 test; CC-BY-NC-4.0. 2024 winner (TUDA_MAI) used proprietary LLMs; the 2025 edition required open-weight models on one 23 GB GPU within one minute per claim and switched evidence scoring to the Ev2R LLM-based method, so 2024 and 2025 scores are not comparable. | **Our app's verdict labels are exactly these four classes**, so benchmark labels and product labels match. We evaluate label accuracy only, which is *not* the official AVeriTeC score (that also scores retrieved evidence). |
| HerO (Team HUMANE), AIC CTU systems — [arXiv 2508.04390](https://arxiv.org/html/2508.04390v1) | Strong open-weight AVeriTeC systems using retrieval-augmented LLMs. | Context only; we use a hosted model, not their open-weight stacks. |

## 2. Check-worthiness detection

* **CLEF CheckThat! 2024 Task 1** — [task page](https://checkthat.gitlab.io/clef2024/task1), [data](https://gitlab.com/checkthat_lab/clef2024-checkthat-lab). English data are sentences
  from US presidential debate transcripts labelled check-worthy or not. Fine-tuned encoders dominate:
  e.g. RoBERTa reached F1 0.753 on the English test set (9th place) for the IAI group
  ([arXiv 2408.01118](https://arxiv.org/abs/2408.01118)). The dataset licence is not stated on the task page (to be checked in the
  repository README before use).
* **What we take:** we do not train a classifier (the project rules out fine-tuning). We score our
  product's zero-shot LLM extractor on the test split and cite published systems as reference points.

## 3. Existing fact-checking extensions and screen tools

| Tool | Notes | What we take |
|---|---|---|
| **OpenErrata** — [code](https://github.com/ZeroPathAI/OpenErrata) | Open-source Chrome MV3 extension (Svelte); an LLM reads a post, searches the web and underlines claims with concrete counter-evidence; leaves jokes/disputes alone. Licence reported as AGPLv3 (secondary; not copied). | UX: inline underline + reason on hover; only flag with concrete evidence. |
| gsverlov/factcheck-extension — [code](https://github.com/gsverlov/factcheck-extension) | Readability.js extraction, Claude via native messaging, red (false) / yellow (misleading) underlines. | Colour coding. |
| vonavikon/video-factcheck — [code](https://github.com/vonavikon/video-factcheck) | YouTube subtitles → claims; verifies with ClaimReview, web search and literature; keeps a source only with a verbatim quote. | The verbatim-quote rule for sources (we re-fetch cited pages and fuzzy-match quotes). |
| **screenpipe** — [about](https://screenpi.pe/about), [accessibility post](https://screenpi.pe/blog/screenpipe-v2-03-accessibility-capture) | Records the screen; accessibility APIs first, OCR fallback. Licence reported to have moved to a commercial licence (secondary, conflicting sources). | The capture strategy as an idea only; no code reused. |
| Google Fact Check Tools API — [docs](https://developers.google.com/fact-check/tools/api/) | `claims:search` over published ClaimReview fact-checks (API key). | Not used in v1 (future work: free lookup of existing human fact-checks). |

## 4. Reading screen text on Windows

* **Windows.Media.Ocr** through pywinrt: `winrt-Windows.Media.Ocr` ([PyPI](https://pypi.org/project/winrt-Windows.Media.Ocr/)) returns lines and per-word bounding
  rectangles; verified by installing version 10.0.28000.2957 for Python 3.13 in this project. Older
  wrapper `winocr` ([PyPI](https://pypi.org/project/winocr)) is a thin layer over the same API; example of the SoftwareBitmap
  flow: [gist](https://gist.github.com/wolfmanstout/5e8a286176f432d006640e3c1c4b45c1).
* **UI Automation**: [Python-UIAutomation-for-Windows](https://github.com/yinkaisheng/Python-UIAutomation-for-Windows) wraps UIA; Chromium documents its UIA provider
  ([docs](https://chromium.googlesource.com/chromium/src/+/HEAD/docs/accessibility/browser/uiautomation.md)). Electron apps such as Discord may expose sparse trees, so OCR is our default and UIA
  an optional, measured enhancement.

## 5. Running LLM-written code safely

* Claude Code's own sandbox uses bubblewrap on Linux/WSL2 and is not available on native Windows
  ([docs](https://code.claude.com/docs/en/sandboxing)). Anthropic's [sandbox-runtime](https://github.com/anthropic-experimental/sandbox-runtime) adds a domain-allowlist proxy, but its
  filesystem reads are deny-list based, which would leave Windows drives (`/mnt/c`) readable unless every
  path were listed.
* Pydantic's Pyodide-in-Deno `mcp-run-python` was archived by its maintainers as not safe for
  untrusted code ([Simon Willison's note](https://simonwillison.net/2025/Apr/18/mcp-run-python/), [PyPI](https://pypi.org/project/mcp-run-python/)).
* **What we take:** our `bash` tool runs inside bubblewrap in WSL2 with an explicit mount allow-list
  (read-only `/usr` and a tools folder, one writable per-claim `/work`), no network, and resource limits.
  Raw data reaches the sandbox only through our audited `fetch_file` tool.

## 6. LLM confidence and calibration

* Guo et al. 2017, *On Calibration of Modern Neural Networks* ([arXiv 1706.04599](https://arxiv.org/abs/1706.04599)): expected calibration error
  (ECE) and reliability diagrams.
* Tian et al. 2023, *Just Ask for Calibration* ([arXiv 2305.14975](https://arxiv.org/abs/2305.14975)): verbalised confidence from RLHF models can be
  better calibrated than token probabilities (headline numbers taken from a secondary summary).
* Xiong et al., ICLR 2024, *Can LLMs Express Their Uncertainty?* ([arXiv 2306.13063](https://arxiv.org/abs/2306.13063)): verbalised confidence tends to
  be **overconfident**; prompting strategies help; consistency-based methods usually beat it.
* **What we take:** every verdict carries a verbalised `confidence` plus `confidence_reasoning`; we
  measure Brier score, ECE and selective accuracy instead of trusting the numbers.

## 7. Significance testing in NLP

* Dror et al. 2018, *The Hitchhiker's Guide to Testing Statistical Significance in NLP*
  ([ACL P18-1128](https://aclanthology.org/P18-1128/)): paired, non-parametric tests (permutation, bootstrap) for comparing systems on
  the same test items. We use exact McNemar / paired permutation tests with bootstrap confidence
  intervals and Holm–Bonferroni correction.

## 8. Claude Agent SDK facts used in the design (verified against docs and installed code)

* Package `claude-agent-sdk` 0.2.179 (bundles the Claude Code CLI); options include `tools`,
  `allowed_tools`, `setting_sources`, `strict_mcp_config`, `output_format`, `hooks`, `effort`, `env`,
  `extra_args` ([Python reference](https://code.claude.com/docs/en/agent-sdk/python.md), [structured outputs](https://code.claude.com/docs/en/agent-sdk/structured-outputs.md), [hooks](https://code.claude.com/docs/en/agent-sdk/hooks.md)).
* No temperature/seed option ([configuration](https://code.claude.com/docs/en/agent-sdk/configuration.md)), so run-to-run variance must be measured.
* Pricing ([pricing page](https://platform.claude.com/docs/en/about-claude/pricing)): Haiku 5.5 $0.10 / $0.50 per MTok input/output (1-hour cache
  writes $0.20, cache reads $0.01); web search $10 per 1,000 searches. Our live smoke test on
  2026-11-08 reported `total_cost_usd = 0.0002749` for one tool-less call, exactly matching
  2 × $0.10 + 1,166 × $0.20 + 83 × $0.50 per MTok.

## Gaps this project addresses

1. Existing tools check web pages only; none we found checks arbitrary desktop apps (e.g. Discord)
   through OCR with an on-screen overlay.
2. Few systems give a calibrated confidence *and* a sourced correction per claim.
3. We found no measurement of how much a screen-capture channel (OCR) degrades verification accuracy.
