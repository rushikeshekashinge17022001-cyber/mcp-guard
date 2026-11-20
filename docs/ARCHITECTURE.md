# MCP Guard: architecture and interception approach

## 1. The problem: where does MCP traffic actually flow?

The first design question is *where* a third-party program can see an MCP request before it executes. The answer differs between the two clients:

| Client | MCP server kind | Who opens the connection | Transport | Interception point |
|---|---|---|---|---|
| Claude Desktop | Local servers in `claude_desktop_config.json` | The Desktop app, on your machine | stdio (child process, newline-delimited JSON-RPC) | **Wrap the server command**: Claude launches our proxy, which launches the real server |
| Claude Web (claude.ai) | Custom/remote connectors | **Anthropic's backend**, not your browser | Streamable HTTP (+ SSE) | **Own the connector URL**: register our gateway URL in place of the server URL |
| Claude Desktop | Custom/remote connectors | Anthropic's backend | Streamable HTTP | Same gateway as Claude Web |

We rejected some other approaches:

* **Network sniffing / TLS MITM.** stdio traffic never touches the network. Remote-connector traffic does not leave your machine either: it goes from Anthropic's cloud to the MCP server. MITM also needs root CA installation and breaks on certificate pinning.
* **Browser extension for claude.ai.** The browser never talks MCP. It only sees chat-level events, so an extension could not *hold* or *deny* the actual request.
* **Patching or injecting into the Claude app.** This is fragile across updates and needs elevated privileges.

The two chosen points sit **in-band on the JSON-RPC stream**. That makes interception *authoritative*: a blocked request is physically never delivered to the server. It needs no privileges, survives client updates, and is fully reversible (`mcp-guard uninstall`).

## 2. Components

```
                ┌──────────────────────────── your machine ───────────────────────────────┐
 Claude Desktop │  stdio   ┌─────────────┐  stdio  ┌──────────────┐                      │
 ───────────────┼─────────►│ StdioProxy  ├────────►│ real server  │                      │
                │          └──────┬──────┘         └──────────────┘                      │
                │                 │ decide()                                             │
                │                 ▼             TCP 127.0.0.1 + token                    │
                │          ┌──────────────────────────────────────────┐   main thread    │
                │          │  Approval daemon                         │  ┌─────────────┐ │
                │          │  BrokerServer ─► PolicyEngine ─► UI ─────┼─►│ Tk popups   │ │
                │          │        │                                 │  │ (topmost)   │ │
                │          │        └─► AuditLog (audit.jsonl)        │  └─────────────┘ │
                │          └──────────────────────────────────────────┘                  │
                │                 ▲ decide()                                             │
 claude.ai ─────┼─► tunnel ─►┌────┴────────┐  HTTP / stdio  ┌──────────────┐             │
 (Anthropic     │  HTTPS     │ HttpGateway ├───────────────►│ real server  │             │
  backend)      │            └─────────────┘                └──────────────┘             │
                └─────────────────────────────────────────────────────────────────────────┘
```

| Module | Responsibility |
|---|---|
| `proxy/stdio_proxy.py` | Transparent stdio relay. Parks gated requests and forwards everything else byte-for-byte. |
| `proxy/http_gateway.py` | Streamable-HTTP reverse proxy (JSON and SSE relay), per-session context, secret URL path. |
| `proxy/stdio_upstream.py` | Lets the gateway expose a *local* stdio server to Claude Web (one process per MCP session). |
| `proxy/interceptor.py` | Transport-independent: learns `clientInfo`, `serverInfo`, and the tool catalogue (descriptions and annotations) from traffic, and builds `ApprovalRequest`s. |
| `broker/` | Daemon IPC: loopback TCP, newline-delimited JSON, shared-secret token, one connection per request. |
| `policy.py` | Glob rules (`server`, `tool`, `method`, `client`, `origin`) plus "remembered" choices. |
| `ui/` | `tk` (default), `osascript` (native macOS dialog), and `console` backends behind one interface. |
| `audit.py` | Append-only JSONL with secret redaction. |
| `installer.py` | Reversible rewrite of `claude_desktop_config.json`. |
| `daemon.py`, `service.py` | Process and thread wiring, plus a macOS LaunchAgent for run-at-login. |

## 3. Request lifecycle (stdio)

1. Claude writes `{"id":42,"method":"tools/call","params":{"name":"delete_file",...}}` to the proxy's stdin.
2. The proxy sees a gated method. It **does not forward**: it starts an independent task and keeps reading. Pings, other tool calls, and notifications continue to flow.
3. The task opens a connection to the daemon (`hello` + token, then `decide`).
4. The daemon evaluates policy. If the result is `ask`, a popup opens, with an individual countdown per request.
5. The user's decision is returned:
   * **Allow**: the original bytes are written to the server's stdin. The server's response flows back unchanged.
   * **Block**: the proxy answers the client itself, with a `result` where `isError: true` (default, so the model sees *why*) or a JSON-RPC error `-32001`. The server never learns about the request.
6. The decision is appended to `audit.jsonl`.

Claude Web follows the same flow over HTTP. The gateway holds the HTTP `POST` open while the popup is up, then either relays it upstream (streaming the JSON or SSE response back verbatim) or answers with the block result.

## 4. Concurrency model

* **Proxy**: every gated request is its own `asyncio` task, keyed by JSON-RPC id. Responses are written as soon as each decision arrives, in any order. JSON-RPC matches by id, so out-of-order replies are correct (see `test_concurrent_requests_resolved_independently`).
* **Daemon**: every request is its own connection and its own coroutine, with its own popup and timeout. The popups cascade and each shows "*n* of *N* pending". Tested with 20 simultaneous decisions resolved in reverse order.
* **Threads**: GUI toolkits must own the main thread (mandatory on macOS), so the asyncio broker runs on a worker thread. UI commands cross over through a `queue.Queue`, and results come back through `loop.call_soon_threadsafe`.

## 5. Failure handling: fail closed everywhere

| Situation | Behaviour |
|---|---|
| Daemon not running | The proxy auto-starts it (detached). If it is still unreachable, the request is **blocked** (`proxy.fail_mode: "closed"`). |
| Daemon crashes or restarts mid-decision | The connection drops, and the proxy treats it as "unavailable" and blocks. |
| User doesn't answer | Auto-block after `approval.timeout_seconds` (default 55s, under the 60s request timeout common in MCP clients). The popup is dismissed. |
| Client cancels (`notifications/cancelled`) | The parked request is withdrawn, the popup closes, and nothing is forwarded (the server never saw it). |
| HTTP client disconnects while waiting | Detected by polling the transport. The decision is withdrawn and the popup closes. |
| Daemon shutdown (Ctrl+C, SIGTERM, logout) | All pending requests are answered **block**, then the daemon exits. |
| Popup or UI error | The request is blocked. The UI loop is guarded so one bad popup can't kill the others. |
| Real server exits | The proxy exits with the server's exit code, so Claude shows the server as failed exactly as before. |
| Claude quits (stdin EOF) | Stdin is closed to the server, then SIGTERM, then SIGKILL, following MCP's shutdown sequence. |
| Upstream HTTP server down | JSON-RPC `-32603` with HTTP 502, carrying the request id. |
| Server command not found | Exit 127, with a clear message in Claude's MCP log. The PATH is augmented with Homebrew, Volta, Cargo, and similar directories because GUI apps start with a minimal PATH. |
| Invalid config file | Logged; secure defaults are used. A typo can't disable protection. |
| Second daemon started | It fails to bind the fixed port and exits quietly (single instance). |

## 6. Security considerations

* **IPC authentication**: the daemon listens on loopback only, with a 256-bit random token in `~/.mcp-guard/daemon.json` (mode `0600`, directory `0700`). Other local users cannot answer or flood approvals. Tokens are compared with `hmac.compare_digest`.
* **No accidental approvals**: no keyboard shortcut approves a request (Escape blocks). Buttons stay disarmed for 700 ms after a popup appears, so a click aimed at another window can't land on "Allow". Closing the window means Block.
* **Untrusted metadata is labelled as such**: tool annotations (`destructiveHint` and so on) are declared by the server, so the popup marks them "(declared)". When they are absent, a name heuristic flags risky tools.
* **Gateway exposure**: the gateway binds `127.0.0.1` and is published only through the tunnel you choose. Its URL contains a random capability secret (`/<secret>/mcp/<route>`). Upstream credentials stay in local config and never pass through Claude.
* **Audit privacy**: arguments are logged with keys such as `token`, `password`, and `api_key` redacted. Huge strings are clipped. Argument logging can be disabled.
* **Remembered decisions are exact**: the "Remember" box creates a rule for that server and tool only. Glob characters are escaped.

## 7. Known limitations

* **Desktop Extensions (`.mcpb`/`.dxt`)** installed via Settings → Extensions are not in `claude_desktop_config.json`, so the installer can't wrap them. Add them as regular config servers instead.
* **Claude Web requires a public URL**, because Anthropic's backend must reach the gateway. Run a tunnel such as `cloudflared` or `ngrok`.
* For **stdio-backed gateway routes**, server→client requests (sampling, roots) are refused, because no GET stream is offered. Server notifications are dropped. HTTP routes relay everything, including GET/SSE streams.
* **Upstream OAuth** discovery is not proxied. Use static `headers` per route (for example, a bearer token).
* Only `tools/call` is gated by default. Add `resources/read` or `prompts/get` to `policy.gated_methods` to gate those too.
