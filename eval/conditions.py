"""Benchmark conditions: the URL policies and VerifyConfig for each preregistered arm.

Local context: build the two blocklists and map condition names to VerifyConfig objects.
Global context: the app itself runs with an empty policy; only the evaluation blocks anything.
  LEAKAGE (all web conditions): sites hosting copies of the benchmark or papers quoting labelled claims,
    plus any URL containing "averitec"/"checkthat", plus archive copies of blocked domains.
  FACT_CHECK (only the H2 "blocked" arm): every publisher of an AVeriTeC fact-check article (whole domains,
    which also removes those outlets' ordinary news: a conservative choice), curated extra fact-checkers,
    and URLs whose path contains "fact-check"/"factcheck".
"""

from __future__ import annotations

from functools import lru_cache

from eval.datasets import EXTRA_FACT_CHECKERS, fact_check_domains
from factcheck.pipeline.policy import UrlPolicy
from factcheck.pipeline.verify import VerifyConfig

LEAKAGE_DOMAINS = frozenset({"huggingface.co", "hf.co", "github.com", "githubusercontent.com", "kaggle.com",
                             "arxiv.org", "aclanthology.org", "fever.ai", "paperswithcode.com", "semanticscholar.org"})
LEAKAGE_TERMS = ("averitec", "checkthat")


@lru_cache(maxsize=1)
def leakage_policy() -> UrlPolicy:
    return UrlPolicy(blocked_domains=LEAKAGE_DOMAINS, blocked_path_terms=LEAKAGE_TERMS)


@lru_cache(maxsize=1)
def factcheck_blocked_policy() -> UrlPolicy:
    domains = frozenset(LEAKAGE_DOMAINS | fact_check_domains() | EXTRA_FACT_CHECKERS)
    return UrlPolicy(blocked_domains=domains, blocked_path_terms=(*LEAKAGE_TERMS, "fact-check", "factcheck", "fact_check"))


def config_for(condition: str, claim_date: str | None, max_searches: int = 2, max_fetches: int = 3) -> VerifyConfig:
    """VerifyConfig for one benchmark arm. Every arm has the sandboxed bash tool; web arms add retrieval."""
    common = {"claim_date": claim_date, "max_searches": max_searches, "max_fetches": max_fetches, "max_turns": 12,
              "verify_quotes": True, "timeout_s": 300}
    if condition in ("closed_book", "h3_clean", "h3_ocr"):
        return VerifyConfig(web=False, verify_quotes=False, **{k: v for k, v in common.items() if k != "verify_quotes"})
    if condition == "web_default":
        return VerifyConfig(web=True, policy=leakage_policy(), **common)
    if condition == "web_blocked":
        return VerifyConfig(web=True, policy=factcheck_blocked_policy(), **common)
    raise ValueError(f"unknown condition {condition}")
