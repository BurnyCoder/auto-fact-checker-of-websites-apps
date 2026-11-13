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

## Extension connection model (`extension/service_worker.js`, `content.js`)

* **Triggers.** By default nothing leaves a page until the user asks: toolbar button *Check this page now*,
  `Alt+Shift+F` (`commands`), or the right-click item. *Check pages automatically* in Options restores the
  automatic mode (on load, scroll and DOM changes).
* **Part of a page.** `Alt+Shift+S` (`check-part`) or the popup's *Check part of page…*: with a non-empty selection,
  `checkRange` keeps the blocks with a text node the selection intersects (`Range.intersectsNode`); otherwise a box
  picker opens inside the closed shadow root and keeps the blocks with a text line box overlapping the box
  (`Range.getClientRects`). Whole blocks are sent either way, so claims keep their context and underlines map to
  exact ranges. The right-click item uses `checkRange` too.
* **On demand only.** The worker opens the WebSocket only when there is work (a check, or the popup/Options asking for
  the status), and only after `GET /health` answers. A WebSocket handshake to a closed port logs
  `net::ERR_CONNECTION_REFUSED` in `chrome://extensions` on every attempt; a failed `fetch()` logs nothing (both
  measured over CDP). There is no reconnect loop. When the app drops a connection it cancels that connection's work,
  so the worker tells the waiting pages ("the fact-check app is not running …") instead of retrying.
* **Reloads.** Chrome does not re-inject content scripts into open tabs when an extension is reloaded or removed. The
  copy left behind ("orphaned": `chrome.runtime.id` is gone and API calls throw "Extension context invalidated")
  removes its highlights and card on its next disconnect or mouse move. The worker's `askTab()` injects a fresh copy
  (`chrome.scripting` with `activeTab`, granted by the shortcut, toolbar button and context menu:
  [activeTab](https://developer.chrome.com/docs/extensions/develop/concepts/activeTab)) when a tab has no live content
  script, instead of failing with "Receiving end does not exist".
* Regression test: `tests/test_extension_e2e.py::test_no_extension_errors_when_app_is_down_or_extension_reloaded`
  reads console errors, exceptions and log entries of the worker and the page over CDP.

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
* Hotkey capture reads whole monitors, so before OCR it blacks out every pixel whose topmost window must not be read
  (own windows such as the side panel, blocked apps, private windows, Chrome while the extension is connected).
  `winapi.visible_windows()` walks top-level windows in z-order (`GetTopWindow` + `GetWindow(GW_HWNDNEXT)`) and drops
  hidden, minimized, DWM-cloaked and click-through (`WS_EX_TRANSPARENT`, e.g. our overlay) windows; `skip_mask`
  paints their rectangles bottom-up so each pixel belongs to the topmost window. Layered windows that never take
  focus (e.g. the GeForce overlay) may be transparent, so they never count as covering a window below them.
  Continuous mode applies the same rules to the foreground window (`privacy_reason` + minimized/tiny).
* Region check (`Ctrl+Alt+R`, `ui/region.py`): one dimmed picker window per monitor (excluded from capture); the drag
  is mapped to physical pixels with `phys = monitor origin + round(local · devicePixelRatio)`, the inverse of the
  overlay mapping, so it is exact on mixed-DPI setups (measured: a full-screen drag on the 150 % monitor maps to its
  2560×1440 physical rectangle). The pickers close before `capture_region` OCRs only that rectangle as source
  `screen:region`; own, blocked and private windows are still blacked out, Chrome is not (explicit request).
* Hotkey boxes stay for `SCREEN_LAYOUT_TTL_S` = 60 s after the screen's latest result (claims, status or verdict),
  not after the capture: verdicts take 20–60 s. A 1 s timer in `UiState` removes expired layouts.
