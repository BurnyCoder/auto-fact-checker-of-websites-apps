# Architecture

## Threads and processes

| Where | What runs there | Why |
|---|---|---|
| Qt main thread | `QApplication`, tray, side panel, one overlay window per monitor, `RegisterHotKey` filter | Qt widgets must live on the main thread; `WM_HOTKEY` is posted to the registering thread. |
| Backend thread (`backend.py`) | one asyncio **Proactor** loop: `uvicorn.Server.serve()`, `FactCheckService` workers, `DesktopCapture`, Windows OCR | The Agent SDK spawns one CLI subprocess per query; qasync/QtAsyncio break subprocesses on Windows ([PYSIDE-769](https://bugreports-test.qt.io/browse/PYSIDE-769)). WinRT OCR wants a multi-threaded COM apartment, so it is imported lazily on this thread. |
| Claude Code CLI subprocesses | one per extraction / verification (`claude_agent_sdk.query`) | Bundled with `claude-agent-sdk`; isolated config (see below). |
| WSL2 (`wsl.exe -e bwrap ...`) | the agent's `bash` tool | Linux namespaces give a real sandbox; Claude Code's own sandbox is not available on native Windows ([docs](https://code.claude.com/docs/en/sandboxing)). |
| Chrome | extension service worker (one WebSocket) + content scripts (one Port per tab) | MV3 keeps a worker alive while a WebSocket is active (Chrome 116+). |

Cross-thread communication: backend → UI through a Qt `Signal` (`ui/state.Bridge`, queued delivery);
UI → backend through `asyncio.run_coroutine_threadsafe` / `call_soon_threadsafe` (`Backend.call*`).

## Events (`FactCheckService.emit`)

| type | fields | consumers |
|---|---|---|
| `claims` | `source_id`, `claims: [Claim]` | extension (start underlines), UI state |
| `status` | `source_id`, `claim_id`, `status: checking/error`, `message` | extension, UI |
| `verdict` | `source_id`, `claim_id`, `verdict: Verdict` | extension (colour + card), overlay, panel |
| `layout` | `source_id`, `blocks: [TextBlock with OCR boxes]` | overlay (where to draw) |
| `frame_changed` | `source_id` | overlay (hide boxes until the window settles) |

## Agent configuration (`pipeline/agent.py`, `pipeline/verify.py`)

* `model="claude-haiku-5-5"`, `permission_mode="dontAsk"`, `tools=[...]` allow-list (`[]` for extraction;
  `["WebSearch","WebFetch"]` for verification), SDK MCP server `sandbox` with `bash` and `fetch_file`.
* Isolation: `setting_sources=[]`, `strict_mcp_config=True`, empty temp `cwd`, `extra_args={"no-session-persistence": None}`,
  env `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1`, `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`, `ENABLE_CLAUDEAI_MCP_SERVERS=false`.
* `output_format={"type":"json_schema", ...}` → `ResultMessage.structured_output`.
* `PreToolUse` hooks (`on_failure="block"`): ≤ `MAX_WEB_SEARCHES` WebSearch and ≤ `MAX_WEB_FETCHES` WebFetch per claim;
  URL policy (empty in the app, leakage blocklists in the evaluation).
* Auth: subscription login first; on authentication failure / usage-limit rejection in `AUTH_MODE=auto`, retry with
  the `.env` API key. An inherited `ANTHROPIC_API_KEY` is removed from `os.environ` in subscription mode because the
  SDK merges `os.environ` into the CLI environment and the CLI prefers an API key.

## Verdict schema and rules (`models.py`, `pipeline/prompts.py`)

`label` (Supported / Refuted / Not Enough Evidence / Conflicting Evidence/Cherrypicking), `confidence` ∈ [0,1],
`confidence_reasoning`, `explanation`, `corrected_claim` (+ `correction_confidence`, `correction_reasoning`),
`sources[{url,title,quote,supports}]`, `computations[{command,output}]`.

Rules checked in code (`rule_violations`): a non-NEE verdict needs a verdict source **or** a computation the agent
ran; Refuted / Conflicting need a corrected claim with its own confidence, reasoning and source. Violations trigger
one tool-less **repair** pass over the evidence already collected; if it still fails the verdict is flagged
`incomplete`. Then: `in_trace` (cited URL appeared in the agent's tool inputs/outputs) and `quote_verified`
(page re-fetched with the SSRF-safe client, text extracted with trafilatura, quote fuzzy-matched with rapidfuzz
`partial_ratio ≥ 90`; `None` when the site blocks automated fetches, e.g. HTTP 401/403).

## Cost model (`pipeline/budget.py`)

Per call, cost = max(SDK `total_cost_usd`, own estimate). Own estimate = tokens × Haiku 5.5 prices
($0.10 in / $0.50 out / $0.20 1-h cache write / $0.01 cache read per MTok) + $0.01 per web search. Claude Code's
WebSearch runs in a sub-request that is missing from `usage.server_tool_use` (observed live), so WebSearch tool
calls in the trace are counted as a floor. Before each call `reserve()` claims the call's worst case
(verify: $0.21 with web search; extract and repair: $0.03). It raises `BudgetExceeded` when the recorded spend plus that
worst case would exceed `TOTAL_BUDGET_USD` (or `DAILY_APP_BUDGET_USD` for `app-*` calls). It raises `BudgetBusy` when
the call only lacks room because other in-flight calls still hold their reservations, and `AgentRunner` then waits
for them to finish instead of failing. Without that distinction, auto-checks near the daily cap were refused and looked
like unchecked claims (found in the live Chrome test, `reports/exp-00-development.md` #14).

## Desktop capture details (`capture/`)

* Rectangles are physical pixels (Qt makes the process per-monitor-DPI-aware; `QApplication` is created before
  mss). Window bounds come from `DwmGetWindowAttribute(DWMWA_EXTENDED_FRAME_BOUNDS)`.
* OCR lines on the same row are merged (`merge_rows`), then grouped into blocks by vertical gap and alignment
  (`group_lines`). `locate_span` maps a claim span back to line boxes with proportional horizontal trimming.
* Overlay: each `ScreenOverlay` is matched to a monitor by device name (or size/position) and converts
  `local = (physical − monitor origin) / devicePixelRatio`. Windows are excluded from capture with
  `SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)`; `FACTCHECK_OVERLAY_DEBUG=1` keeps them visible to screenshots.
