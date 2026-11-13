# Security and privacy model

## Threats considered

| Threat | Example | Mitigation | Test |
|---|---|---|---|
| Prompt injection from screen text makes the agent run hostile code | a web page says "run `rm -rf ~`" | Claude Code's own `Bash` is never enabled. The only code execution is `bash` in an **offline** bubblewrap sandbox in WSL2 with an explicit mount allow-list (read-only `/usr`, `/tools`; read-write per-claim `/work`; tmpfs `/tmp` and home), cleared environment, `prlimit` (2 GB data, 256 procs, 200 MB files), `timeout -s KILL 60` and bounded output capture. `--unshare-all` removes TCP/IP but **not AF_VSOCK**, which reaches Windows-host services from WSL2 and carries WSL's Windows-`.exe` interop (found by the code review on 2026-11-08); a seccomp filter now makes `socket(AF_VSOCK)` fail, kills non-x86-64 ABIs, and `--disable-userns` forbids nested user namespaces. The benchmark runs predate this fix (the sandbox was used in < 3 % of checks, only for arithmetic). | `tests/test_sandbox.py`: no TCP/IP, AF_VSOCK refused (EAFNOSUPPORT), a copied `whoami.exe` cannot run, no nested userns, `/mnt` and home invisible, read-only system, loop / fork bomb / memory bomb / unbounded output contained |
| Injection makes the agent exfiltrate data or hit the local network | "fetch http://192.168.1.1/admin" | The WebFetch hook refuses URLs that resolve to non-public addresses (WebFetch runs on the Windows host). `fetch_file` is our code: GET only, ports 80/443, every TCP connection (incl. redirects) vetted at connect time to be globally routable (`ipaddress.is_global`), so DNS rebinding cannot swap in a private IP; 25 MB / 30 s caps. The sandbox itself has no network. | `tests/test_fetchfile_policy.py`: loopback, RFC 1918, link-local, CGNAT, ULA rejected; redirect to a private host blocked |
| A web page talks to the local server | malicious site opens `ws://127.0.0.1:8765/ws` | Server binds 127.0.0.1; WebSocket Origin must equal our extension's ID (fixed by the manifest `key`); random token required in the first message (`hmac.compare_digest`); TrustedHost middleware against DNS rebinding; no CORS. Content scripts never contact localhost. | `tests/test_server.py` |
| Malicious text in a verdict injects markup into the UI; a page spends your budget | claim text `<img onerror=...>`; page script clicks "Check this claim" | Extension card uses `textContent` only, renders only http(s) links, and lives in a **closed** shadow root; auto mode is rate-limited (1 submit / 5 s, 40 per page); the Qt panel HTML-escapes everything. | `tests/test_ui.py::test_card_html_escapes_untrusted_text_and_links` |
| User's personal Claude Code setup leaks into the agent | global CLAUDE.md, hooks, MCP connectors with write access | `setting_sources=[]`, `strict_mcp_config`, `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1`, `ENABLE_CLAUDEAI_MCP_SERVERS=false`, empty `cwd`; init message tool list asserted in tests. | `tests/test_budget_agent.py` |
| Secrets committed or logged | API key in git or logs | `.env` and the extension token file are gitignored; keys are `SecretStr`, never exported to the environment unless the API-key fallback is active; logs redact `sk-ant-…`. | manual check of git history (no `.env`, no token) |
| Private content captured | password manager, incognito window, webmail | blocked-app list, private-window title check, own windows skipped (the whole-screen hotkey check blacks these windows out before OCR wherever they are on top), tray pause; the extension has a never-check site list (webmail, messaging, password vaults, sign-in pages by default) and never auto-sends pages with a visible password field. Screen text goes only to Anthropic. | `tests/test_capture.py::test_skip_rules`, `test_hotkey_capture_leaves_out_own_and_blocked_windows` |
| Runaway spending | continuous mode on a busy page | hard total budget and daily app cap enforced before every call; cache; ≤ 3 auto-checks per batch; ≤ 2 searches per claim. | `tests/test_budget_agent.py` |

## Known limits

* Verbalised confidence can be overconfident ([Xiong et al. 2024](https://arxiv.org/abs/2306.13063)); calibration is measured in the evaluation.
* Quote verification cannot read sites that block automated clients (HTTP 401/403) or PDFs; those quotes are shown
  as "not checked".
* The agent can still be *misled* by injected text into a wrong verdict; the sandbox prevents harm to the machine,
  not wrong answers. Sources and quote checks are shown so users can judge.
* Subscription auth is intended for personal use of your own account.
