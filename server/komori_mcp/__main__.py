"""Entry point. `KOMORI_MCP_TRANSPORT` picks the transport (`stdio` default, or `streamable-http`
for a real network service) — the tool set and every tool's code is identical either way; only
how a client reaches the process changes. `KOMORI_MCP_PORT`/`KOMORI_MCP_HOST` apply to
`streamable-http` only.
"""

from __future__ import annotations

import os

from komori_mcp.server import mcp


def main() -> None:
    transport = os.environ.get("KOMORI_MCP_TRANSPORT", "stdio")
    if transport == "streamable-http":
        mcp.settings.host = os.environ.get("KOMORI_MCP_HOST", "127.0.0.1")
        mcp.settings.port = int(os.environ.get("KOMORI_MCP_PORT", "8722"))
    mcp.run(transport=transport)  # type: ignore[arg-type]


if __name__ == "__main__":
    main()
