#!/usr/bin/env bash
# setup_wsl_sandbox.sh — one-time, no-sudo setup of the read-only toolchain the agent's sandbox sees as /tools.
#
# Global context: the fact-checking agent gets a `bash` tool that runs inside bubblewrap in WSL2
# (see src/factcheck/pipeline/sandbox.py). The sandbox mounts WSL's /usr read-only (python3, node,
# gcc, perl, jq, sqlite3, ... already installed by apt) plus this tools folder read-only at /tools:
#   tools/venv  — a Python venv with data-analysis libraries (numpy, pandas, scipy, ...)
#   tools/bin   — extra single-binary tools (deno)
# Nothing here needs root, so it can be re-run safely to update the toolchain.
#
# Run from Windows:  wsl.exe -e bash scripts/setup_wsl_sandbox.sh
set -euo pipefail                                   # stop on any error, unset var, or failed pipe stage

SANDBOX_ROOT="${SANDBOX_ROOT:-$HOME/projects/fact-check-sandbox}"   # where the toolchain lives in WSL
TOOLS="$SANDBOX_ROOT/tools"                          # this folder is mounted read-only at /tools
mkdir -p "$TOOLS/bin"                                # bin/ holds standalone binaries such as deno

# --- uv (Python package manager) — official installer puts it in ~/.local/bin, no sudo ----------------
# Source: https://docs.astral.sh/uv/getting-started/installation/
if ! command -v uv >/dev/null 2>&1 && [ ! -x "$HOME/.local/bin/uv" ]; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
UV="$(command -v uv || echo "$HOME/.local/bin/uv")"  # absolute path to uv for the rest of the script

# --- Python venv built on the system python3 in /usr (which the sandbox also mounts) -----------------
# The venv's python is a symlink to /usr/bin/python3.x, so it resolves inside the sandbox too.
if [ ! -x "$TOOLS/venv/bin/python" ]; then
  "$UV" venv --python /usr/bin/python3 "$TOOLS/venv"
fi
# Libraries chosen for fact-checking work: numerics/statistics (numpy, scipy, statsmodels, sympy),
# tables (pandas, pyarrow, openpyxl for .xlsx), units (pint), dates (python-dateutil),
# and document parsing (pypdf for PDFs, beautifulsoup4 + lxml for raw HTML).
"$UV" pip install --python "$TOOLS/venv/bin/python" \
  numpy pandas scipy statsmodels sympy pint python-dateutil openpyxl pyarrow pypdf beautifulsoup4 lxml

# --- Deno (JS/TS runtime), mirroring the Windows install — official installer, custom prefix --------
# Source: https://docs.deno.com/runtime/getting_started/installation/
if [ ! -x "$TOOLS/bin/deno" ]; then
  curl -fsSL https://deno.land/install.sh | DENO_INSTALL="$TOOLS" sh -s -- --yes --no-modify-path
fi

# --- Report what the sandbox will see -------------------------------------------------------------------
echo "toolchain ready in $TOOLS"
"$TOOLS/venv/bin/python" -c "import numpy, pandas, scipy, sympy; print('python libs ok:', numpy.__version__, pandas.__version__)"
"$TOOLS/bin/deno" --version | head -1
