# komori-mcp

Japan-equities disclosure research over the Model Context Protocol — filings, 変遷 (narrative
threads tracked across a company's filing history), scenario trees, and semantic search over what
a company actually wrote, not just its headlines. Built on the [Komori external API](https://api.komori.app/v1)
(`GET /v1/openapi.json` for the full contract).

Requires a [Komori Basic](https://komori.app/plans) subscription. No pasted tokens: connecting
opens a real login in your browser, and a personal API token is minted and handed back
automatically — the same pattern `gh auth login --web` uses.

## Install

### Claude Code

```
/plugin marketplace add Hone-Systems/komori-mcp
/plugin install komori
```

The first tool call opens `komori.app` in your browser to log in; after that, the token is stored
locally and reused.

### Codex

```
codex plugin marketplace add Hone-Systems/komori-mcp
codex plugin add komori
```

### Cursor / VS Code / Zed / anything else that reads raw MCP config

There's no plugin/bundle system for these — add the server directly.

**Cursor** (`.cursor/mcp.json` or `~/.cursor/mcp.json`) / most other stdio-based clients:
```json
{
  "mcpServers": {
    "komori": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/Hone-Systems/komori-mcp#subdirectory=server", "komori-mcp"]
    }
  }
}
```

**VS Code** (`.vscode/mcp.json`) — needs an explicit `type`:
```json
{
  "servers": {
    "komori": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--from", "git+https://github.com/Hone-Systems/komori-mcp#subdirectory=server", "komori-mcp"]
    }
  }
}
```

**Zed** (`settings.json`, under `context_servers`):
```json
{
  "context_servers": {
    "komori": {
      "command": { "path": "uvx", "args": ["--from", "git+https://github.com/Hone-Systems/komori-mcp#subdirectory=server", "komori-mcp"] }
    }
  }
}
```

No `uv`? `pip install -e ./server` from a clone of this repo, then point `command` at `komori-mcp`
(the installed console script) or `python -m komori_mcp` directly.

## How login works

No token to copy-paste. The first tool call (or any call after a token is revoked) opens
`https://komori.app/mcp-authorize` in your default browser. If you're not already signed in, you go
through the normal Komori login first. Once signed in, a personal API token is minted on your
account (visible and revocable any time at komori.app → 設定 → APIトークン) and handed back to the
waiting local process over a one-shot loopback listener — nothing leaves your machine except the
initial browser navigation. The token is then cached (in your OS keychain when available, otherwise
a file under `~/.komori/` with owner-only permissions) so you don't repeat this every session.

## Tools

Every tool maps to one `/v1` endpoint — `whoami`, `search`, `search_filing_sections`, `get_trending`,
`get_changes`, `list_companies`, `get_company`, `list_filings`, `get_filing`, `get_filing_analysis`,
`get_filing_scenario`, `get_company_threads`, `get_thread`, `list_news`, `get_news_story`,
`list_signals`, `get_signal`, `get_market_summary`, `list_watchlists`, `create_watchlist`,
`add_to_watchlist`. Full parameter docs live in each tool's own description (surfaced by your MCP
client) and in the canonical contract at `GET https://api.komori.app/v1/openapi.json`.

Reads that cost a unit (threads, filing analysis, scenarios, news detail, signal detail, section
search) are free forever on re-read, and free for any company on one of your watchlists — normal
research usage rarely touches the budget.

## Skills

Three bundled skills encode how to actually use these tools for real research, not just what each
one returns:

- **`pre-position-checklist`** — before looking at a name, what to verify from its thread history.
- **`claim-audit`** — check an outside thesis or note against the actual filing text, sorted into
  supported / extrapolation / not addressed.
- **`language-drift`** — track how one commitment's wording has shifted across a company's filing
  history.

## Development

```
cd server
python -m venv .venv && .venv/bin/pip install -e .
KOMORI_API_BASE=http://localhost:5132/v1 .venv/bin/komori-mcp   # point at a local Komori dev stack
```

## Not financial advice

Every response from the underlying API carries a standing notice: Komori is a research tool, not
investment advice. This server and its skills are built to present findings as things worth
checking, never as recommendations.

## License

MIT
