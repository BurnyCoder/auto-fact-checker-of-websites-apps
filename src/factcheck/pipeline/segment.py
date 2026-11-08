"""Text normalisation, sentence splitting and fingerprints for de-duplication.

Local context: small pure helpers.
Global context: the capture side uses `new_sentences` so continuous mode only sends text it has not
sent before; the cache uses `normalize_claim` as its key.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
import warnings
from functools import lru_cache

warnings.filterwarnings("ignore", category=SyntaxWarning)  # pysbd 0.3.4 has invalid escape sequences (harmless)
import pysbd

_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-"})


@lru_cache(maxsize=1)
def _segmenter() -> pysbd.Segmenter:
    """pysbd rule-based sentence boundary detector (https://github.com/nipunsadvilkar/pySBD)."""
    return pysbd.Segmenter(language="en", clean=False)


def normalize_text(text: str) -> str:
    """Unicode-normalise, unify quotes/dashes and collapse whitespace (keeps case and punctuation)."""
    text = unicodedata.normalize("NFKC", text).translate(_QUOTES)
    return re.sub(r"\s+", " ", text).strip()


def normalize_claim(text: str) -> str:
    """Aggressive normalisation for cache keys: lowercase, no punctuation except digits' separators."""
    text = normalize_text(text).lower()
    text = re.sub(r"(?<!\d)[.,](?!\d)", " ", text)     # drop sentence punctuation but keep 3.5 and 1,000
    text = re.sub(r"[^\w\s.,%$€£-]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def fingerprint(text: str) -> str:
    return hashlib.sha1(normalize_claim(text).encode("utf-8")).hexdigest()[:16]


def split_sentences(text: str) -> list[str]:
    text = normalize_text(text)
    return [s.strip() for s in _segmenter().segment(text) if s.strip()] if text else []


def new_sentences(text: str, seen: set[str]) -> list[str]:
    """Sentences of `text` whose fingerprint is not in `seen` (and add them to `seen`)."""
    fresh = []
    for sentence in split_sentences(text):
        fp = fingerprint(sentence)
        if fp not in seen:
            seen.add(fp)
            fresh.append(sentence)
    return fresh
