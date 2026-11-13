"""Desktop app entry point (`uv run factcheck`): wires the phases together.

Phases, in order:
  1. load_config      — Settings from .env
  2. setup_logging    — timestamped logs + full LLM record (logs/)
  3. create_qt_app    — QApplication FIRST, so Qt sets per-monitor DPI awareness before mss does
  4. build_backend    — server + fact-check service + desktop capture on a background asyncio loop
  5. build_ui         — side panel, per-monitor overlay, tray menu, global hotkey
  6. run              — start the backend, then the Qt event loop
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import urllib.request

from PySide6.QtCore import QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication

from factcheck.backend import Backend
from factcheck.capture.watcher import DesktopCapture
from factcheck.config import Settings, load_settings
from factcheck.log import setup_logging
from factcheck.pipeline.sandbox import sandbox_available
from factcheck.ui.hotkey import HotkeyFilter
from factcheck.ui.overlay import OverlayManager
from factcheck.ui.panel import Panel
from factcheck.ui.region import RegionPickerManager
from factcheck.ui.state import Bridge, UiState
from factcheck.ui.tray import Tray

log = logging.getLogger("factcheck.app")


def already_running(port: int) -> bool:
    """True when this app already answers on `port` (GET /health names it). A second copy (e.g. the launcher
    double-clicked twice) cannot bind the port or the hotkeys, so it stops at once instead of failing after a
    20 s timeout with a traceback. Another program on the port does not count."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as response:
            return json.load(response).get("app") == "fact-check-websites-apps"
    except (OSError, ValueError):                       # nothing listening, timeout, or not our JSON
        return False


def create_qt_app() -> QApplication:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)                 # closing the panel keeps the tray app running
    app.setApplicationName("fact-check-websites-apps")
    return app


def build_backend(settings: Settings) -> tuple[Backend, DesktopCapture]:
    backend = Backend(settings)
    capture = DesktopCapture(settings, backend.service, lambda: backend.extension_connected)
    backend.add_startup_task(capture.run_continuous)
    return backend, capture


def build_ui(settings: Settings, backend: Backend, capture: DesktopCapture, qt: QApplication) -> dict:
    """Create the UI objects; returns them so they stay referenced for the app's lifetime."""
    state = UiState()
    bridge = Bridge()
    bridge.event.connect(state.on_event)
    backend.service.listeners.append(bridge.listener)

    def status_text() -> str:
        spent = backend.ledger.spent_total()
        mode = "paused" if capture.paused else f"trigger: {settings.factcheck_mode}"
        fake = " · FAKE MODE (no real checks)" if settings.factcheck_fake_llm else ""
        return f"Spent ${spent:.3f} of ${settings.total_budget_usd:.2f} · {mode}{fake}"

    def check_now() -> None:
        backend.call(capture.capture_now())

    pickers = RegionPickerManager()

    def check_part() -> None:
        """Drag a box over part of the screen; only that part is read (the pickers close before the screenshot)."""
        if capture.paused:
            tray.showMessage("Fact check", "Capture is paused. Untick 'Pause capture' in the tray menu first.")
            return
        pickers.pick(on_done=lambda rect: backend.call(capture.capture_region(rect)))

    panel = Panel(state, on_check=lambda cid: backend.call_soon(backend.service.check, cid), status_text=status_text,
                  auto_checks=settings.max_auto_checks_per_batch)
    overlays = OverlayManager(state, debug_visible_in_capture=os.environ.get("FACTCHECK_OVERLAY_DEBUG") == "1")

    def set_display(display: str) -> None:
        settings.factcheck_display = display
        overlays.set_visible(display in ("overlay", "both"))
        panel.setVisible(display in ("panel", "both"))

    def set_mode(mode: str) -> None:
        settings.factcheck_mode = mode
        capture.continuous_enabled = mode in ("continuous", "both")
        hotkey_enabled[0] = mode in ("hotkey", "both")

    hotkey_enabled = [settings.factcheck_mode in ("hotkey", "both")]
    # Ctrl+Alt+F follows the trigger mode; the region hotkey is always on (dragging a box is an explicit request)
    hotkey = HotkeyFilter([(settings.hotkey, lambda: check_now() if hotkey_enabled[0] else None),
                           (settings.region_hotkey, check_part)])

    def copy_token() -> None:
        QGuiApplication.clipboard().setText(settings.server_token)
        tray.showMessage("Fact check", "Extension token copied. Paste it in the extension's Options page.")

    tray = Tray(mode=settings.factcheck_mode, display=settings.factcheck_display,
                on_pause=lambda on: setattr(capture, "paused", on), on_check_now=check_now, on_check_part=check_part,
                on_mode=set_mode,
                on_display=set_display, on_show_panel=lambda: (panel.show(), panel.raise_(), panel.activateWindow()),
                on_copy_token=copy_token, on_open_logs=lambda: os.startfile(settings.logs_dir),
                budget_text=status_text, on_quit=qt.quit)
    set_display(settings.factcheck_display)
    return {"state": state, "bridge": bridge, "panel": panel, "overlays": overlays, "hotkey": hotkey, "tray": tray,
            "pickers": pickers}


def main() -> None:
    settings = load_settings()
    setup_logging(settings.logs_dir, name="app")
    if already_running(settings.server_port):
        log.error("the fact-check app is already running (blue check-mark icon in the Windows tray); not starting "
                  "a second copy")
        sys.exit(2)                                     # exit code 2 = already running (Start Fact Check.cmd says so)
    ok, why = sandbox_available()
    log.info("starting: mode=%s display=%s fake=%s sandbox=%s", settings.factcheck_mode, settings.factcheck_display,
             settings.factcheck_fake_llm, "ok" if ok else why)
    qt = create_qt_app()
    backend, capture = build_backend(settings)
    ui = build_ui(settings, backend, capture, qt)
    backend.start()
    # Ctrl+C in the terminal: Python only runs signal handlers between bytecodes, which never happens while Qt's
    # C++ loop waits, so it surfaced as a KeyboardInterrupt traceback inside a Qt slot (live test, 2026-11-13).
    # A handler that quits Qt plus a 250 ms no-op timer that gives Python those moments fixes it
    # (https://docs.python.org/3/library/signal.html#execution-of-python-signal-handlers).
    signal.signal(signal.SIGINT, lambda *_: qt.quit())
    wake = QTimer()
    wake.timeout.connect(lambda: None)
    wake.start(250)
    code = qt.exec()
    ui["hotkey"].close()
    backend.stop()
    sys.exit(code)


if __name__ == "__main__":
    main()
