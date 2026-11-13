"""Side panel: every checked claim with verdict, confidence + reasoning, correction, sources, computations.

Local context: a scrollable list of cards (newest first), rebuilt incrementally from UiState.
Global context: the overlay shows *where* claims are; this panel shows *why*. All model/page text is
HTML-escaped before it goes into Qt rich text, and only http(s) links are made clickable.
"""

from __future__ import annotations

import html
from collections.abc import Callable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from factcheck.models import LABEL_COLOURS
from factcheck.ui.state import UiState

MAX_CARDS = 150
e = html.escape


TEXT, LINK = "#202124", "#1a73e8"   # fixed colours: the card is always white, whatever the Windows theme


def _link(url: str, text: str) -> str:
    if not url.startswith(("http://", "https://")):
        return e(text or url)
    # inline colour: QLabel otherwise uses the palette's Link role, pale cyan in Windows dark mode (unreadable on white)
    return f'<a href="{e(url, quote=True)}" style="color:{LINK}">{e(text or url)}</a>'


def card_html(claim, verdict, auto_checks: int = 3) -> str:
    """Rich-text body of one card (pure function, unit-testable). `auto_checks` = MAX_AUTO_CHECKS_PER_BATCH."""
    src = f"{e(claim.source.app)} — {e(claim.source.title)[:80]}"
    if verdict is None:
        # "pending" = left over by the per-check cap; say so, or the card reads as stuck (user, 2026-11-13)
        not_checked = f"Not checked yet: only the first {auto_checks} claims found are checked automatically. "                       "Press “Check this claim” below."
        status = {"checking": "Checking…", "pending": not_checked, "error": "Check failed"}.get(claim.status, claim.status)
        return (f"<div style='color:#5f6368'>{src}</div><div><b>{e(claim.text)}</b></div>"   # own block: Qt ran it
                f"<div style='color:#5f6368'>{e(status)}</div>")                             # into the source line
    colour = LABEL_COLOURS[verdict.label]
    pct = round(verdict.confidence * 100)
    parts = [
        f"<div style='color:#5f6368'>{src}</div>",
        (f"<div><span style='background:{colour};color:white;padding:1px 6px;border-radius:8px'><b>{e(verdict.label)}</b>"
         f"</span>&nbsp; <b>{pct}%</b> confidence</div>"),          # own block: Qt ran it into the source line
        f"<div style='margin-top:4px'><b>{e(claim.text)}</b></div>",
        f"<div><i>Why this confidence:</i> {e(verdict.confidence_reasoning)}</div>",
        f"<div style='margin-top:3px'>{e(verdict.explanation)}</div>",
    ]
    if verdict.corrected_claim:
        cc = f" ({round(verdict.correction_confidence * 100)}%)" if verdict.correction_confidence is not None else ""
        parts.append(f"<div style='background:#fef7e0;padding:4px'><b>Correction{cc}:</b> {e(verdict.corrected_claim)}"
                     f"<br><span style='color:#5f6368'>{e(verdict.correction_reasoning or '')}</span></div>")
    if verdict.sources:
        rows = []
        for s in verdict.sources:
            badge = {True: "✓ quote found", False: "✗ quote not found", None: "quote not checked"}[s.quote_verified]
            extra = " · not in agent's results" if s.in_trace is False else ""
            rows.append(f"<li>{_link(s.url, s.title)} <span style='color:#5f6368'>({e(s.supports)} · {badge}{extra})</span>"
                        + (f"<br><i>“{e(s.quote)}”</i>" if s.quote else "") + "</li>")
        parts.append("<div style='margin-top:6px'><b>Sources</b></div><ul style='margin:0'>" + "".join(rows) + "</ul>")
    if verdict.computations:
        comp = "<br>".join(f"<code>$ {e(c.command)}</code><br><code>{e(c.output[:400])}</code>" for c in verdict.computations)
        parts.append(f"<div style='margin-top:6px'><b>Computations</b></div>{comp}")
    if verdict.flags:
        parts.append(f"<div style='color:#5f6368'>flags: {e(', '.join(verdict.flags))} · ${verdict.cost_usd:.4f}</div>")
    return "".join(parts)


class ClaimCard(QFrame):
    def __init__(self, on_check: Callable[[str], None], auto_checks: int = 3):
        super().__init__()
        self.auto_checks = auto_checks
        self.setFrameShape(QFrame.Shape.StyledPanel)
        # Colours are set with the background: in Windows dark mode the inherited palette text is white, which made
        # the card's text (user screenshot) and its Check button (user: "I don't see button") invisible on white,
        # 2026-11-13. A style sheet on the button replaces the windows11 style's dark-mode drawing of it.
        self.setStyleSheet("ClaimCard { background: white; border: 1px solid #dadce0; border-radius: 8px; }"
                           f"ClaimCard QLabel {{ color: {TEXT}; background: transparent; }}"
                           f"ClaimCard QPushButton {{ color: {LINK}; background: white; border: 1px solid #dadce0;"
                           " border-radius: 4px; padding: 4px 10px; font-weight: 600; }"
                           "ClaimCard QPushButton:hover { background: #e8f0fe; }")
        layout = QVBoxLayout(self)
        self.label = QLabel()
        self.label.setWordWrap(True)
        self.label.setTextFormat(Qt.TextFormat.RichText)
        self.label.setOpenExternalLinks(True)
        self.label.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        self.button = QPushButton("Check this claim")
        self.button.clicked.connect(lambda: on_check(self.claim_id))
        layout.addWidget(self.label)
        layout.addWidget(self.button)
        self.claim_id = ""

    def update_from(self, claim, verdict) -> None:
        self.claim_id = claim.id
        self.label.setText(card_html(claim, verdict, self.auto_checks))
        self.button.setVisible(verdict is None and claim.status in ("pending", "error"))


class Panel(QWidget):
    def __init__(self, state: UiState, on_check: Callable[[str], None], status_text: Callable[[], str],
                 auto_checks: int = 3):
        super().__init__(None, Qt.WindowType.Window)
        self.auto_checks = auto_checks
        self.setWindowTitle("Fact checks")
        self.resize(460, 760)
        self.state = state
        self.on_check = on_check
        self.status_text = status_text
        outer = QVBoxLayout(self)
        self.header = QLabel()
        outer.addWidget(self.header)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        self.list = QVBoxLayout(inner)
        self.list.addStretch(1)
        scroll.setWidget(inner)
        outer.addWidget(scroll)
        self.cards: dict[str, ClaimCard] = {}
        state.changed.connect(self.refresh)
        self.header_timer = QTimer(self)                    # header (spend) every 5 s, not on every event: reading the
        self.header_timer.timeout.connect(lambda: self.header.setText(self.status_text()))  # ledger is file I/O
        self.header_timer.start(5000)
        self.header.setText(self.status_text())
        self.refresh()

    def refresh(self) -> None:
        for cid in self.state.order[-MAX_CARDS:]:
            card = self.cards.get(cid)
            if card is None:
                card = ClaimCard(self.on_check, self.auto_checks)
                self.cards[cid] = card
                self.list.insertWidget(0, card)                 # newest on top
            card.update_from(self.state.claims[cid], self.state.verdicts.get(cid))
        while len(self.cards) > MAX_CARDS:                      # drop the oldest cards
            oldest = next(iter(self.cards))
            self.cards.pop(oldest).deleteLater()
