"""Entry point. `KOMORI_MCP_TRANSPORT` picks the transport.

- `stdio` (default): the local server — every tool, the token from the OS keychain via a browser
  login on first use. What the Claude Code / Codex plugins and `uvx` installs run.
- `streamable-http`: the hosted server (`komori_mcp.http`, senki#1074) — read tools only, every
  request authorised with an OAuth access token. It never touches the keychain. `KOMORI_MCP_HOST` /
  `KOMORI_MCP_PORT` apply to this mode only.
"""

from __future__ import annotations

import os

import uvicorn

from komori_mcp.http import create_app
from komori_mcp.server import mcp


def main() -> None:
    transport = os.environ.get("KOMORI_MCP_TRANSPORT", "stdio")
    if transport == "streamable-http":
        uvicorn.run(
            create_app(),
            host=os.environ.get("KOMORI_MCP_HOST", "127.0.0.1"),
            port=int(os.environ.get("KOMORI_MCP_PORT", "8722")),
            proxy_headers=True,
            log_level=os.environ.get("KOMORI_MCP_LOG_LEVEL", "info"),
        )
        return
    if transport != "stdio":
        raise SystemExit(f"KOMORI_MCP_TRANSPORT must be 'stdio' or 'streamable-http', not {transport!r}")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
