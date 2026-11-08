# AGENTS.md — notes for coding agents working on this repo

* Environment: Windows 11 + WSL2. Use `uv` (`uv sync`, `uv run ...`). Python 3.13 (pinned in `.python-version`).
* Tests: `uv run pytest` (all $0). Live API tests are opt-in (`-m live`). Never run code that spends budget without
  checking `uv run factcheck-cli budget`; the ledger `budget_ledger.jsonl` is committed and the cap is hard.
* Fake mode: `FACTCHECK_FAKE_LLM=true` gives deterministic verdicts for UI/extension work.
* Lint: `uv run ruff check src tests scripts --fix`.
* Never commit `.env`, `data/server_token.txt`, `logs/`, `.agent-work/`.
* Architecture and invariants: `docs/architecture.md`; security invariants (do not weaken): `docs/security.md`.
  In particular: never enable Claude Code's built-in `Bash` for the verifier, never give the sandbox network,
  keep `setting_sources=[]` and the WebSocket Origin + token checks.
* Evaluation code lives in `eval/`; confirmatory analyses must follow `reports/preregistration.md`. Changes after the
  `prereg-v1` tag must be labelled exploratory.
