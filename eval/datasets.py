"""Benchmark loaders: AVeriTeC (claims + 4 labels) and CLEF CheckThat! 2024 Task 1 English (check-worthiness).

Local context: download-once into data/raw/ (gitignored), parse into small dataclasses, derive the
fact-checker domain list used by the H2 blocklist.
Global context: the eval runner, the pilot and the report all load data only through here.
Sources (verified 2026-11-08):
  AVeriTeC (Schlichtkrull et al. 2023, https://arxiv.org/abs/2305.13117), official files
    https://github.com/MichSchli/AVeriTeC/tree/main/data — train 3,068 / dev 500; licence CC-BY-NC-4.0 per the
    shared-task paper (https://arxiv.org/abs/2410.23850; the GitHub repo itself has no LICENSE file).
  CheckThat! 2024 Task 1 (https://checkthat.gitlab.io/clef2024/task1), files
    https://gitlab.com/checkthat_lab/clef2024-checkthat-lab/-/tree/main/task1/data — English train/dev/dev-test
    and test gold (341 sentences).
"""

from __future__ import annotations

import csv
import json
import re
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
AVERITEC_URL = "https://raw.githubusercontent.com/MichSchli/AVeriTeC/main/data/{split}.json"
CHECKTHAT_BASE = "https://gitlab.com/checkthat_lab/clef2024-checkthat-lab/-/raw/main/task1/data/"


def _download(url: str, dest: Path) -> Path:
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(url, dest)
    return dest


@dataclass(frozen=True)
class AveritecClaim:
    item_id: str            # "<split>-<index>" (stable: index in the official file)
    claim: str
    label: str
    claim_date: str | None  # DD-MM-YYYY as in the dataset
    speaker: str | None
    reporting_source: str | None
    location: str | None
    justification: str      # gold rationale (used only by the E3 judge, never shown to the verifier)
    fact_check_url: str     # never shown to the verifier


def load_averitec(split: str) -> list[AveritecClaim]:
    path = _download(AVERITEC_URL.format(split=split), RAW / "averitec" / f"{split}.json")
    rows = json.loads(path.read_text(encoding="utf-8"))
    return [AveritecClaim(item_id=f"{split}-{i}", claim=r["claim"], label=r["label"], claim_date=r.get("claim_date"),
                          speaker=r.get("speaker"), reporting_source=r.get("reporting_source"),
                          location=r.get("location_ISO_code"), justification=r.get("justification") or "",
                          fact_check_url=r.get("fact_checking_article") or "") for i, r in enumerate(rows)]


def unwrap_archive(url: str) -> str:
    """AVeriTeC stores fact-check articles as web.archive.org/web/<ts>/<original>; return <original>."""
    m = re.match(r"https?://web\.archive\.org/web/[^/]+/(.*)", url)
    inner = m.group(1) if m else url
    return inner if inner.startswith("http") else f"http://{inner}"


def fact_check_domains(splits=("train", "dev")) -> set[str]:
    """Publishers of the fact-check articles behind AVeriTeC claims (47 domains on 2026-11-08)."""
    domains = set()
    for split in splits:
        for c in load_averitec(split):
            host = (urlsplit(unwrap_archive(c.fact_check_url)).hostname or "").lower().removeprefix("www.")
            if host:
                domains.add(host)
    return domains


# Widely known fact-checking outlets that do not appear in AVeriTeC's article list, added so the blocked
# condition also covers them (curated by hand; not claimed to be a complete list of IFCN signatories).
EXTRA_FACT_CHECKERS = {"snopes.com", "truthorfiction.com", "reuters.com", "apnews.com", "newschecker.in",
                       "logicallyfacts.com", "correctiv.org", "maldita.es", "demagog.cz", "factcheck.kz"}


@dataclass(frozen=True)
class CheckworthySentence:
    sentence_id: str
    text: str
    label: bool             # True = check-worthy ("Yes")


def load_checkthat(split: str) -> list[CheckworthySentence]:
    """split in {"train", "dev", "dev-test", "test"}; test uses the released gold file."""
    folder = RAW / "checkthat2024"
    if split == "test":
        z = _download(CHECKTHAT_BASE + "CT24_checkworthy_test_gold.zip", folder / "CT24_checkworthy_test_gold.zip")
        member, path = "english/CT24_checkworthy_english_test_gold.tsv", folder / "english" / "CT24_checkworthy_english_test_gold.tsv"
    else:
        z = _download(CHECKTHAT_BASE + "CT24_checkworthy_english.zip", folder / "CT24_checkworthy_english.zip")
        member = f"CT24_checkworthy_english/CT24_checkworthy_english_{split}.tsv"
        path = folder / member
    if not path.exists():
        with zipfile.ZipFile(z) as zf:
            zf.extract(member, folder)
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh, delimiter="\t", quoting=csv.QUOTE_MINIMAL)
        return [CheckworthySentence(r["Sentence_id"], r["Text"], r["class_label"].strip() == "Yes") for r in reader]
