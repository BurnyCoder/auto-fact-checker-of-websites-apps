"""In-process MCP tools given to the verification agent: `bash` (sandbox) and `fetch_file` (downloads).

Local context: build a per-claim MCP server whose tool functions are closures bound to that claim's
workspace and URL policy.
Global context: registered via `ClaudeAgentOptions.mcp_servers`; the SDK names them
`mcp__sandbox__bash` / `mcp__sandbox__fetch_file` (https://code.claude.com/docs/en/agent-sdk/custom-tools.md).
Claude Code's own Bash tool is never enabled, because on Windows it would run unsandboxed.
"""

from __future__ import annotations

from claude_agent_sdk import create_sdk_mcp_server, tool

from factcheck.pipeline.fetchfile import fetch_file
from factcheck.pipeline.policy import UrlPolicy
from factcheck.pipeline.sandbox import SandboxConfig, run_bash
from factcheck.pipeline.workspace import Workspace

SERVER_NAME = "sandbox"
BASH_TOOL = f"mcp__{SERVER_NAME}__bash"
FETCH_TOOL = f"mcp__{SERVER_NAME}__fetch_file"

BASH_DESCRIPTION = (
    "Run a bash script in an offline Linux sandbox (no network). Available: python3 with numpy, pandas, scipy, sympy, "
    "statsmodels, pint, openpyxl, pyarrow, pypdf, beautifulsoup4, lxml; node; deno; gcc/g++; make; cmake; perl; bc; "
    "awk; jq; sqlite3. The working directory is /work, which persists between calls for this claim and contains files "
    "saved by fetch_file. Limits: 60 s, 2 GB memory. Returns exit code, stdout and stderr."
)
FETCH_DESCRIPTION = (
    "Download a public URL (CSV, JSON, XLSX, PDF, HTML, ...) into /work/<filename> so bash can analyse the raw data. "
    "Public internet only, at most 25 MB and 30 s. Returns the saved path, size, content type and a short preview."
)


def build_tool_server(ws: Workspace, policy: UrlPolicy, sandbox_cfg: SandboxConfig, with_fetch: bool):
    """Return (mcp_server_config, allowed_tool_names) for one verification."""

    @tool("bash", BASH_DESCRIPTION, {"command": str})
    async def bash(args):
        res = await run_bash(str(args.get("command", "")), ws, sandbox_cfg)
        return {"content": [{"type": "text", "text": res.as_tool_text()}], "is_error": res.exit_code != 0}

    @tool("fetch_file", FETCH_DESCRIPTION, {"url": str, "filename": str})
    async def fetch(args):
        res = await fetch_file(str(args.get("url", "")), str(args.get("filename", "")), ws.host_path, policy)
        return {"content": [{"type": "text", "text": res.as_tool_text()}], "is_error": not res.ok}

    tools = [bash, fetch] if with_fetch else [bash]
    server = create_sdk_mcp_server(name=SERVER_NAME, version="1.0.0", tools=tools)
    names = [BASH_TOOL, FETCH_TOOL] if with_fetch else [BASH_TOOL]
    return {SERVER_NAME: server}, names
