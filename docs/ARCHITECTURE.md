# Architecture

The independently built desktop uses bundle identifier `app.cdxmux.multi`; its
Computer Use helper uses `com.cdxmux.sky.CUAService`. Neither identifier is used
by the official ChatGPT installation. These identifiers and the `.codex-mux`
state directory remain stable across the product rename so existing macOS
privacy grants, connected accounts, and sticky thread ownership continue to
work.

Codex Subscription Router replaces the copied app's bundled `codex` executable
with a small Go multiplexer and keeps the original binary beside it as
`codex.real`.

## Request routing

The desktop app opens one JSON-RPC app-server connection to the multiplexer.
The multiplexer starts one real app-server child for every enabled account,
each with its own `CODEX_HOME` and `CODEX_SQLITE_HOME`.

New threads are assigned using a quota-urgency score: weekly percentage
remaining divided by the hours until that account resets. Banked usage resets
add a capped bonus, while short-window usage, existing pinned-thread count, and
stable account order break close results. Reset-credit metadata is fetched in
parallel, cached for five minutes, and treated as neutral when unavailable.
Once a thread ID is known, `state.json` persists its owner. Requests, responses,
approvals, and notifications are rewritten only as needed to preserve one
coherent desktop session.

If the owner is depleted, the multiplexer resumes the rollout on an account
with capacity and updates ownership. Threads do not migrate for ordinary load
balancing.

Requests configured for a custom model provider or non-OpenAI base URL use an
external-provider lane. They are forwarded to the selected account's child but
are excluded from ChatGPT quota accounting, depletion failover, and thread-owner
changes, including when the external provider returns a limit error.

## Account isolation

The Primary account uses `~/.codex`. Added accounts use
`~/.codex-mux/accounts/<id>/codex-home`. Managed configuration is copied from
the Primary account, excluding credential-store settings and project trust.
Each isolated account forces file-backed CLI and MCP OAuth credentials.

Legacy per-account rollout directories and SQLite indexes are not imported on
startup. Their presence is a fail-closed condition. The optional offline
migration plans all file hashes and SQLite thread IDs first, refuses collisions,
requires related processes to be stopped, uses an exclusive database lock, and
keeps a rollback receipt and backup until installation commits.

## Desktop integration

The patcher extracts `app.asar`, verifies exact upstream anchors, inserts the
account UI, disables self-update, and repacks the archive with an updated
integrity hash. The app receives a separate Chromium profile and URL scheme.

The copied Electron runtime, Computer Use service, Node runtime, and callers are
re-signed under one Apple team. The patcher derives that team from a freshly
signed probe instead of parsing the signing identity's display name. The helper
uses a separate bundle identity and socket, avoiding the official app's privacy
grants and app-group container.

## Plugin behavior

Plugin definitions and managed MCP configuration are shared. The Plugins page
adds an account selector and marks Apps, MCP status, and MCP OAuth requests with
the selected account ID. The multiplexer removes that private routing marker
before forwarding the strict RPC request to the chosen child.

## Control API

The renderer talks to a loopback-only HTTP service on port 48123. All private
routes require a random 256-bit token. CORS is limited to the copied app's
`app://-` origin. The service exposes account metadata, aggregated usage and
profile data, thread ownership, login/logout actions, and an authenticated SSE
event stream; it never returns OAuth tokens.
