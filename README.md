# MCP Guard

**Interactive authorization for every MCP tool call made by Claude Desktop and Claude Web.**

When Claude tries to run an MCP tool, MCP Guard holds the request and shows an always-on-top popup. The popup lists the client, the MCP server, the tool, its description and risk hints, and the exact arguments. Nothing executes until you click **Allow**. **Block** returns a clean denial that Claude can see and explain.

```
Claude Desktop ─stdio─► mcp-guard proxy ─stdio─► real MCP server
claude.ai ─HTTPS─► tunnel ─► mcp-guard gateway ─HTTP/stdio─► real MCP server
                         │
                         └─ tools/call ─► approval daemon ─► [ Block ] [ Allow ] popup
```

* **Real interception, not just monitoring.** It runs in-band on the JSON-RPC stream, so a blocked call never reaches the server.
* **Concurrent-safe.** Every pending call gets its own popup and timer. Calls are answered in any order while other traffic (pings, `tools/list`) keeps flowing.
* **Fails closed.** If the daemon is down, crashes, or times out, or the popup errors, the call is blocked.
* **Policy rules and "remember my choice"** let you auto-allow trusted read-only tools or always block dangerous ones.
* **Audit log** (`~/.mcp-guard/audit.jsonl`) records every decision, with secrets redacted.
* **Reversible.** `mcp-guard uninstall` restores your Claude config exactly.

See **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** for the design: where MCP traffic actually flows, why these two interception points, the concurrency model, failure handling, and security.

---

## 1. Install

You need Python 3.9 or later. Tk ships with python.org and system Python; on Homebrew, run `brew install python-tk`.

```bash
git clone <this repo> mcp-guard && cd mcp-guard
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"            # the package must be installed: Claude starts the proxy from "/"
mcp-guard init-config              # optional: writes ~/.mcp-guard/config.json with defaults
```

## 2. Start the approval daemon

```bash
mcp-guard daemon                   # foreground (Ctrl+C to stop)
mcp-guard service install          # macOS: start automatically at login (LaunchAgent)
mcp-guard status                   # is it running? what is pending?
mcp-guard test-popup --count 2     # see two simultaneous popups right now
```

Proxies also auto-start the daemon if it isn't running.

## 3. Claude Desktop

```bash
mcp-guard install --dry-run        # preview
mcp-guard install                  # wraps every stdio server in claude_desktop_config.json (backup written)
```

Then **fully quit and reopen Claude Desktop**. Ask Claude to use any tool and the popup appears.

To try it with the bundled demo server, add this to `claude_desktop_config.json` before running `install`:

```json
{ "mcpServers": { "demo": { "command": "python3", "args": ["/ABS/PATH/mcp-guard/examples/demo_server.py"] } } }
```

The demo tools are `get_weather` (read-only), `write_note`, `delete_file` (simulated, flagged destructive), and `slow_task`. Try a prompt like *"Check the weather in Pune, save a note about it, and delete /tmp/old.txt"*. It triggers three tool calls: allow some and block others.

To undo: `mcp-guard uninstall`.

## 4. Claude Web (claude.ai custom connectors)

claude.ai connects to MCP servers **from Anthropic's backend**, so MCP Guard runs a gateway that you register as the connector URL.

1. Add routes to `~/.mcp-guard/config.json` (see [examples/config.example.json](examples/config.example.json)):
   ```json
   "gateway": { "routes": {
       "demo":   { "command": "python3", "args": ["/ABS/PATH/examples/demo_server.py"] },
       "remote": { "url": "https://mcp.example.com/mcp", "headers": {"Authorization": "Bearer ..."} }
   }}
   ```
2. Run the daemon with the gateway (or run `mcp-guard gateway` separately):
   ```bash
   mcp-guard daemon --gateway         # logs: Gateway route demo -> http://127.0.0.1:8787/<secret>/mcp/demo
   ```
3. Expose it publicly:
   ```bash
   cloudflared tunnel --url http://127.0.0.1:8787      # or: ngrok http 8787
   ```
4. In claude.ai, go to **Settings → Connectors → Add custom connector**, with URL `https://<tunnel-host>/<secret>/mcp/demo`.
5. Enable the connector in a chat and ask Claude to use it. The popup appears on your machine.

The random `<secret>` path segment (stored in `~/.mcp-guard/gateway_secret`) keeps strangers from using your tunnel. The same URL also works as a custom connector in Claude Desktop.

## 5. Configuration (`~/.mcp-guard/config.json`)

All keys are optional and deep-merged over the defaults in [mcp_guard/config.py](mcp_guard/config.py).

| Key | Default | Meaning |
|---|---|---|
| `approval.timeout_seconds` | `55` | Auto-block if there is no answer (kept below clients' 60s request timeout) |
| `approval.ui` | `auto` | `tk`, `osascript` (native macOS dialog), or `console` |
| `policy.default` | `ask` | `ask`, `allow`, or `block` when no rule matches |
| `policy.rules` | `[]` | First match wins. Glob fields: `server`, `tool`, `method`, `client`, `origin`, plus `action` and optional `reason` |
| `policy.gated_methods` | `["tools/call"]` | Add `resources/read` or `prompts/get` to gate those too |
| `proxy.fail_mode` | `closed` | `open` lets calls through when the daemon is unreachable (not recommended) |
| `proxy.block_style` | `tool_error` | `tool_error` (the model sees the denial) or `jsonrpc_error` (`-32001`) |
| `proxy.autostart_daemon` | `true` | Proxies launch the daemon when needed |
| `audit.log_arguments` / `audit.redact_keys` | `true` / secrets list | Audit privacy |
| `gateway.host` / `port` / `path_secret` / `routes` | `127.0.0.1` / `8787` / `auto` / `{}` | HTTP gateway |

Example rules:

```json
"rules": [
  {"server": "filesystem", "tool": "read_*",  "action": "allow"},
  {"tool": "*delete*",                         "action": "block", "reason": "no deletes, ever"},
  {"origin": "claude-web",                     "action": "ask"}
]
```

## 6. Day-to-day commands

```bash
mcp-guard logs -n 50          # recent decisions
mcp-guard logs -f             # follow live
mcp-guard status              # daemon health and pending requests
```

The `logs` output looks like this:

```
2026-11-19T17:38:21+0530  ALLOW  demo/get_weather   by user      claude-desktop   1830ms  allowed by user
2026-11-19T17:38:25+0530  BLOCK  demo/delete_file   by user      claude-web       2410ms  blocked by user
```

File locations:

* `~/.mcp-guard/`: config, audit log, remembered decisions, daemon token, daemon log. Override with `MCP_GUARD_HOME`.
* Proxy logs appear in Claude's own MCP logs: `~/Library/Logs/Claude/mcp-server-<name>.log`.

## 7. Demo without Claude (reproducible)

```bash
mcp-guard daemon &                       # popup UI
python examples/simulate_client.py       # speaks MCP over stdio exactly like Claude Desktop


```

This sends three tool calls **at the same time**. Answer the three popups in any order and watch each response arrive independently. Add `--cancel` to see a client-side cancellation close its popup.

## 8. Tests

```bash
pytest -q        # 24 tests, ~5s
```

The end-to-end tests drive the **real proxy subprocess** and **real demo server** over pipes, and the **real gateway** over HTTP (stdio upstream, and an HTTP upstream that answers over SSE). A scripted UI stands in for the popup. They cover:

* allow and block
* out-of-order concurrent decisions
* client cancellation
* timeout
* policy rules
* fail-closed when the daemon is down
* forged IPC token
* daemon shutdown with pending requests
* 20 simultaneous decisions
* installer round-trip
* missing server binary

## Project layout

```
mcp_guard/
  cli.py              command line (daemon, proxy, gateway, install, status, logs, test-popup, service)
  daemon.py           thread wiring: UI on main thread, asyncio broker on worker thread
  broker/             daemon IPC: server (decision pipeline), client (used by proxies), protocol
  proxy/              stdio_proxy (Claude Desktop), http_gateway (+ stdio_upstream) (Claude Web), interceptor
  ui/                 tk_ui (default popup), osascript_ui, console_ui
  policy.py audit.py installer.py service.py config.py models.py jsonrpc.py paths.py
examples/             demo_server.py, simulate_client.py, config.example.json
tests/                end-to-end + unit tests
docs/ARCHITECTURE.md
```
