"""Build reports/paper/paper.pdf from paper.md with pandoc + xelatex (MiKTeX/TeX Live).

Usage: uv run python scripts/build_paper.py   (run `uv run python -m eval.report` first so all numbers are current)
Pandoc manual: https://pandoc.org/MANUAL.html#creating-a-pdf
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAPER = ROOT / "reports" / "paper"


def main() -> None:
    if shutil.which("pandoc") is None:
        sys.exit("pandoc not found (https://pandoc.org/installing.html)")
    src = (PAPER / "paper.md").read_text(encoding="utf-8")
    # HTML comments (GEN markers) are dropped by pandoc; replace symbols the Cambria font lacks
    src = re.sub(r"<!--.*?-->", "", src, flags=re.DOTALL).replace("✓", "yes").replace("✗", "no")
    tmp = PAPER / "_build.md"
    tmp.write_text(src, encoding="utf-8")
    cmd = ["pandoc", str(tmp), "-o", str(PAPER / "paper.pdf"), "--pdf-engine=xelatex", f"--resource-path={PAPER}",
           "-V", "tables=true", "--columns=40"]   # narrow: wide pipe tables wrap to the text width
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    tmp.unlink(missing_ok=True)
    errors = [ln for ln in (result.stderr or "").splitlines() if "major issue" not in ln]
    if result.returncode != 0:
        sys.exit("\n".join(errors) or f"pandoc failed ({result.returncode})")
    print("\n".join(errors[-10:]))
    print("wrote", PAPER / "paper.pdf")


if __name__ == "__main__":
    main()
