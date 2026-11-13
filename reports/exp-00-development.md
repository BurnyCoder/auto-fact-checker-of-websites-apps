# Experiment 0 — building and debugging the system (development log)

Author: Libor "Burny" Burian · 2026-11-08 · All runs here used AVeriTeC *train*, CheckThat! *dev*, synthetic text or
live web pages — never the held-out evaluation data.

## What was built and why

| Component | Choice | Why (alternatives considered) |
|---|---|---|
| Verifier | Claude Agent SDK, Haiku 5.5, WebSearch + WebFetch + `fetch_file` + sandboxed `bash` | User requirement (Agent SDK, Haiku, web search, code execution). One agent per claim keeps verdicts independent and cacheable ([Loki](https://arxiv.org/abs/2410.01794) collapses query/retrieve/verify similarly). |
| Claim extraction | One tool-less Haiku call per batch of text blocks | Cheap (≈ $0.0006 per call measured); [SAFE](https://arxiv.org/abs/2403.18802)-style atomic, self-contained claims with exact spans for highlighting. A fine-tuned classifier was ruled out by the user. |
| Code execution | bubblewrap in WSL2, offline, explicit mount allow-list | Claude Code's sandbox is not available on native Windows; Pyodide sandbox (`mcp-run-python`) was archived as unsafe; Anthropic `sandbox-runtime` allows reads by default (would expose `/mnt/c`). |
| Downloads | Own SSRF-safe `fetch_file` | WebFetch returns a model summary, not raw data; the sandbox must stay offline. |
| Desktop text | Windows.Media.Ocr via pywinrt | Built in, ~30 ms per image, word boxes for the overlay; `winocr` is unmaintained. UI Automation deferred (Electron apps expose sparse trees). |
| Browser | MV3 extension, CSS Custom Highlight API, WebSocket from the service worker | No DOM edits; MV3 workers are killed after 30 s idle unless a WebSocket is active (Chrome 116+). |
| Threading | Qt main thread + one asyncio Proactor thread | qasync/QtAsyncio break subprocesses on Windows; the SDK spawns one CLI per query. |

## Issues found by running the system, and their fixes

| # | Symptom (how it was found) | Cause | Fix | Evidence |
|---|---|---|---|---|
| 1 | A correct "Supported" for "1,000 at 7 % for 30 years > 7,500" became "Not Enough Evidence" (live CLI run) | Rule required a web source; a sandbox computation (1000·1.07³⁰ = 7612.26) was not accepted as evidence, so the repair pass changed the label | Computations the agent actually ran count as evidence; repair prompt keeps the label unless evidence requires otherwise | `tests/test_pipeline_units.py::test_computation_counts_as_evidence` |
| 2 | `usage.server_tool_use.web_search_requests` was 0 although WebSearch ran (live run) | Claude Code's WebSearch executes in a sub-request not included in `usage` | Ledger cost = max(SDK `total_cost_usd`, own estimate + $0.01 per WebSearch call in the trace) | `tests/test_budget_agent.py` |
| 3 | `quote_verified` was null for Britannica and a Kiwix mirror | HTTP 403/401 bot protection (a browser User-Agent did not help) | Report "quote not checked" honestly; keep an honest User-Agent | demo run logs |
| 4 | Every Wikipedia-page verdict was flagged `quote-unverified` | Not a bug: one secondary source per verdict had a paraphrased quote (WebFetch summary); Wikipedia quotes verified | Per-source badges in the UI; quote-verification rate became a reported metric | `scripts/demo_extension.py` output |
| 5 | A username line ended up in its own block, separate from its message (overlay demo) | Windows OCR splits "mike_92" and "Today at 14:02" into two lines; grouping then attached the message to the timestamp | `merge_rows` joins lines on the same row before grouping (9 → 5 blocks on the demo) | `tests/test_capture.py::test_merge_rows_joins_username_and_timestamp` |
| 6 | Overlay tags covered the username/timestamp above a claim | Tag drawn above the first box | Tag drawn to the right of the claim when there is room | `docs/images/overlay-demo.png` |
| 7 | `WinError -2147467260: Another RecognizeAsync operation is already running` (H3 smoke test) | One OcrEngine cannot run concurrent recognitions | Module-level asyncio lock around `recognize_async` | H3 run |
| 8 | 3 / 75 pilot verifications failed with "Failed to provide valid structured output after 5 attempts" | Haiku omitted nullable required fields (`corrected_claim`, `explanation`) repeatedly | Schema requires only label, confidence, reasoning, explanation; other rules enforced in code; failed structured output is salvaged by a tool-less repair pass over the collected evidence | `test_structured_output_failure_is_salvaged_from_evidence` |
| 9 | H4 smoke results were written under `h4-test` | Split switched to `dev` after the results folder name was built | Reordered; smoke runs never touch test data | `eval/run.py` |
| 10 | WebSocket tests were rejected with "Invalid host header" | Starlette's TestClient uses `ws://testserver`; our TrustedHost middleware correctly rejected it | Tests use `ws://127.0.0.1/ws` | `tests/test_server.py` |
| 11 | `uv sync` failed: "failed to remove … dist-info: Access is denied" | The folder carried the Windows read-only attribute | Removed the folder and re-synced | – |
| 13 | All three concurrent eval processes (H1/H2, H3, E2) crashed at 19:10 with `JSONDecodeError: Extra data` | Their appends to the shared `budget_ledger.jsonl` interleaved; one record's head was overwritten, and every later budget check failed to parse the file (fail-safe: spending stopped) | Cross-process file lock (`msvcrt.locking` / `fcntl.flock`) around every ledger read and append; the damaged line was rebuilt exactly from the LLM log (run `3fad4b81a6`, $0.0251); an upper-bound adjustment ($0.208) was added for 20 calls cancelled by restarts and the crash whose cost was never reported; runs resumed from their result files | `tests/test_budget_agent.py::test_ledger_survives_concurrent_processes` (4 processes × 50 appends) |
| 14 | In the user's real Chrome (2026-11-13), auto-check verified 0 of 3 claims on Wikipedia "Eiffel Tower" and 1 of 3 on "Mount Everest"; the rest stayed grey ("Not checked yet"), while "Check this claim" worked. Found with the Claude in Chrome extension: highlight counts read from `CSS.highlights`, LLM log showed no verify prompt for the missing claims, then a 6-second toast "BudgetExceeded: daily app budget $1.00 reached" was caught on screen | Each verification reserves its worst case ($0.21) before running. With $0.63 of the $1.00 daily app cap spent (mostly continuous desktop capture), the 2nd concurrent reservation reached $1.05 and was refused as if the cap were used up. The refusal was not logged, and the page painted errors like unchecked claims | `BudgetBusy` (subclass of `BudgetExceeded`) when only in-flight reservations block a call; `AgentRunner` waits and retries. Refusals are logged. The hover card shows "Couldn't check" with the reason and a *Try again* button. After the fix, on "Golden Gate Bridge" all 3 auto-checks finished (3 Supported) | `test_daily_cap_busy_is_not_exhausted`, `test_runner_waits_while_other_calls_hold_the_budget`, `test_failed_check_explains_why_in_the_card`; `docs/images/live-chrome-card.jpg` |
| 12 | Pipeline of a single run looked 3× slower than the agent's own duration | Quote re-fetching (up to 15 s per page) and CLI start-up dominate per-task wall time | Raised eval concurrency (3 → 6 / 8); quote checks run in parallel | `results/*/progress.json` |

## Live end-to-end checks (real Haiku)

* CLI: "The Eiffel Tower is more than twice as tall as the Great Pyramid of Giza." → Supported, confidence 0.90, two
  sources in the agent's trace, 13 s, $0.014.
* Browser path: the real extension on the live Wikipedia "Great Wall of China" page → 3 verdicts in 27 s (≈ $0.019 each),
  all Supported (`docs/images/extension-demo.png`).
* Desktop path on a rendered chat (`scripts/demo_overlay.py`): "Great Wall visible from the Moon" → Refuted 0.93 with a
  correction; "Everest 8,849 m" → Supported 0.90; "7 % doubles in ~10 years" → Supported 0.93 via
  `ln 2 / ln 1.07 = 10.24` in the sandbox ($0.0007); opinions skipped (`docs/images/overlay-demo.png`).
* User's own setup (2026-11-13), checked with computer use and the Claude in Chrome extension: the server logged
  "extension connected", which happens only after the Origin (extension ID) and token checks pass. So the
  README steps (tray → *Copy extension token* → *Load unpacked* → Options) worked as written. The hover card, *Check this
  claim* and auto-check (after fix #14) worked on live Wikipedia pages (`docs/images/live-chrome-card.jpg`).
  Not clicked by the agent itself: the tray's right-click menu and `chrome://extensions`. The computer-use tool grants
  the taskbar left-click only and Chrome read-only, and the Chrome extension cannot open `chrome://` or other
  extensions' pages.
* Desktop path: continuous capture produced claims from real Discord windows (LLM log, `Source app: discord.exe`).
  Not verified: visual alignment of the overlay boxes on a real Discord window.

## Hyperparameters and why

| Setting | Value | Reason |
|---|---|---|
| `MAX_WEB_SEARCHES` | 2 per claim | Searches dominate cost ($0.01 each); 2 keeps a claim ≈ $0.02 under the $10 project cap |
| `MAX_WEB_FETCHES` | 3 | Enough to read the top results; WebFetch has no per-call fee |
| `max_turns` | 12 | Room for search → fetch → download → compute → answer |
| `effort` | low | Cheapest setting; the closed-book pilot did not need more (not tuned further to save budget) |
| auto-checks per batch | 3 | Bounds cost of continuous mode on busy pages |
| quote match | rapidfuzz `partial_ratio ≥ 90` after normalisation | Tolerates whitespace/typography; rejects paraphrase (unit test) |
| OCR change detection | mean abs diff of 1/8-downsampled green channel < 1.5 | Cheap; noise-tolerant (unit test) |
| line grouping | gap ≤ 1.2 line heights and left edges within 2.5 line heights (or overlap) | Chat and article layouts in tests and demos |
