"""Source checks: was a cited URL really seen by the agent, and does its quote appear on the page?

Local context: URL normalisation, URL extraction from the tool trace, and quote verification by
re-fetching the page ourselves (through the same SSRF-safe client as fetch_file) and fuzzy-matching.
Global context: verify.py runs these after the agent answers. WebFetch only gives the agent a
model-written summary, so a quote can be paraphrased or invented; this check (inspired by the
verbatim-quote rule in vonavikon/video-factcheck) tells the user whether each quote was found.
"""

from __future__ import annotations

import asyncio
import re
from urllib.parse import parse_qsl, urlencode, urlsplit

import trafilatura
from rapidfuzz import fuzz

from factcheck.models import VerdictSource
from factcheck.pipeline.agent import ToolEvent
from factcheck.pipeline.fetchfile import public_only_client
from factcheck.pipeline.policy import UrlPolicy
from factcheck.pipeline.segment import normalize_text

_URL_RE = re.compile(r"https?://[^\s\"'<>\]\)\}]+")


def normalize_url(url: str) -> str:
    """Comparable form: no scheme, no 'www.', no fragment, no tracking params, no trailing slash."""
    parts = urlsplit(url.strip().rstrip(".,;"))
    host = (parts.hostname or "").lower().removeprefix("www.")
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if not k.lower().startswith(("utm_", "fbclid", "gclid"))])
    path = parts.path.rstrip("/")
    return f"{host}{path}" + (f"?{query}" if query else "")


def urls_in_trace(trace: list[ToolEvent]) -> set[str]:
    """Normalised URLs that appeared in any tool input or output (search results, fetched pages, downloads)."""
    found: set[str] = set()
    for ev in trace:
        for text in (str(ev.input), ev.output):
            found.update(normalize_url(u) for u in _URL_RE.findall(text))
    return found


def mark_in_trace(sources: list[VerdictSource], trace: list[ToolEvent]) -> None:
    seen = urls_in_trace(trace)
    for s in sources:
        s.in_trace = normalize_url(s.url) in seen


def _norm_for_match(text: str) -> str:
    return re.sub(r"\s+", " ", normalize_text(text).lower())


def quote_in_text(quote: str, page_text: str, threshold: float = 90.0) -> bool:
    """Fuzzy containment test (rapidfuzz partial_ratio) tolerant of whitespace/punctuation differences."""
    q, t = _norm_for_match(quote), _norm_for_match(page_text)
    if not q or not t:
        return False
    return q in t or fuzz.partial_ratio(q, t) >= threshold


async def _page_text(url: str, policy: UrlPolicy, timeout_s: float) -> str | None:
    if policy.check(url):
        return None
    try:
        async with public_only_client(timeout_s) as client:
            resp = await client.get(url)
            if resp.status_code >= 400 or "html" not in resp.headers.get("content-type", "html"):
                return None                               # PDFs etc. are not checked here (stay None)
            html = resp.text[:3_000_000]
    except Exception:  # noqa: BLE001 - any network failure just means "could not check"
        return None
    main = trafilatura.extract(html, include_tables=True) or ""   # main content (https://trafilatura.readthedocs.io)
    full = trafilatura.html2txt(html) or ""                        # whole visible text as a fallback
    return f"{main}\n{full}"


async def verify_quotes(sources: list[VerdictSource], policy: UrlPolicy, timeout_s: float = 15.0) -> None:
    """Set `quote_verified` on each source with a quote: True/False when the page was readable, else None."""
    async def check(s: VerdictSource) -> None:
        if not s.quote.strip():
            return
        text = await _page_text(s.url, policy, timeout_s)
        s.quote_verified = None if text is None else quote_in_text(s.quote, text)

    await asyncio.gather(*(check(s) for s in sources))
