"""Fact-checking pipeline: claim extraction, verification agent, its tools, cache and budget.

Global context: everything between "text was captured" and "a verdict is ready" lives here.
The capture side (OCR, Chrome extension) feeds text in; the UI side consumes verdicts.
"""
