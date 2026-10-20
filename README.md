# komori-mcp

Japan-equities disclosure research over the Model Context Protocol — filings, 変遷 (narrative
threads tracked across a company's filing history), scenario trees, and semantic search over what
a company actually wrote, not just its headlines. Built on the [Komori external API](https://api.komori.app/v1)
(`GET /v1/openapi.json` for the full contract).

Free with a [komori.app](https://komori.app) account. No pasted tokens: connecting opens a real
login in your browser — the hosted server uses standard MCP OAuth, the local one mints a personal
API token and hands it back automatically (the same pattern `gh auth login --web` uses).

Every tool result carries `source` — the page on komori.app it came from — so an answer built on
Komori can link its reader to the evidence.

## Install

### Hosted server (Claude.ai, ChatGPT, any client that speaks remote MCP)

Add a custom connector / remote MCP server with the URL

```
https://mcp.komori.app/mcp
```

Your client discovers the authorization server on its own, opens komori.app for you to sign in and
approve, and is connected. The hosted server is **read-only**: research tools only, no watchlist or
notification changes (use komori.app or the local server for those). Disconnect any time at
komori.app → 設定 → 接続中のアプリ.

The local install below has the same tools plus the watchlist/notification writes.

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
`add_to_watchlist`, and the watchlist-notification tools — `get_watchlist_notifications`,
`set_watchlist_document_notifications`, `set_watchlist_news_notifications`,
`set_watchlist_performance_notification`, `set_watchlist_risk_notification`, and the account-wide
`set_notification_delivery` (one tool per kind of notification; each result says whether the
notifications will actually be delivered). Full parameter docs live in each tool's own description (surfaced by your MCP
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

## Hosted server

`KOMORI_MCP_TRANSPORT=streamable-http` runs the hosted server (`komori_mcp/http.py`): Streamable HTTP
at `/mcp`, stateless, behind OAuth 2.1 per the MCP authorization spec —

- OAuth 2.0 Protected Resource Metadata (RFC 9728) at `/.well-known/oauth-protected-resource/mcp`
  (and at the root); a request without a valid token gets `401` with
  `WWW-Authenticate: Bearer resource_metadata="…", scope="komori:read"`.
- Tokens are issued by Komori's authorization server (`https://api.komori.app`, RFC 8414 metadata,
  PKCE S256, dynamic client registration and Client ID Metadata Documents) and bound to this server's
  URL (RFC 8707). Every request's token is introspected (RFC 7662) — never cached — so a revoked
  connection fails on the next request.
- The client's token is never forwarded to the Komori API. It is exchanged (RFC 8693) for a short
  `/v1` credential for the same person; the API meters and rate-limits it like any other call.
- Read tools only; the API also refuses writes for this credential.

| Variable | |
|---|---|
| `KOMORI_OAUTH_ISSUER` | the authorization server's issuer (public URL) — required |
| `KOMORI_MCP_RESOURCE` | this server's canonical URL, e.g. `https://mcp.komori.app/mcp` — required |
| `KOMORI_MCP_CLIENT_SECRET` | this server's confidential-client secret at the AS — required |
| `KOMORI_API_BASE` | the `/v1` base — required |
| `KOMORI_OAUTH_INTERNAL_BASE` | where to reach the AS if not at the issuer URL (e.g. a private network) |
| `KOMORI_MCP_ALLOWED_HOSTS` / `KOMORI_MCP_ALLOWED_ORIGINS` | Host / Origin allow-lists (DNS-rebinding protection) |
| `KOMORI_MCP_HOST` / `KOMORI_MCP_PORT` | bind address (default `127.0.0.1:8722`) |

The server emits OpenTelemetry spans and metrics through `opentelemetry-api` — a no-op unless the
process installs an SDK, which the hosted image does. `server.json` is its
[MCP Registry](https://registry.modelcontextprotocol.io) entry (`app.komori/komori`, remote only).

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
