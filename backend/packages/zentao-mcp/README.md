# ZenTao MCP

ZenTao resource reader used by DeerFlow. The MCP surface remains read-only; the Bug
Workbench's server-side note stage uses the same authenticated client to save
one approved analysis note to the Bug-detail page.

## Local configuration

Set the following variables in the environment that starts DeerFlow. Do not put
the token in this repository or commit it to Git.

```bash
export ZENTAO_URL="https://tickets.example.invalid/zentao"
export ZENTAO_TOKEN="replace-with-your-token"
# Optional: refresh the Token automatically only after a 401/403 response.
export ZENTAO_ACCOUNT="your-zentao-account"
export ZENTAO_PASSWORD="your-zentao-password"
```

When account and password are configured, a rejected Token is refreshed once
through ZenTao's token endpoint and the original read request is retried once.
The refreshed Token is written only to `~/.deer-flow/zentao/token` (mode 0600),
not back to `.env`, and is never returned by the MCP tool.

The Workbench needs `ZENTAO_ACCOUNT` and `ZENTAO_PASSWORD` in addition to a
read Token when it writes an **添加备注** entry. ZenTao's visible note form uses
the short-lived browser session (`zentaosid`), not the REST Token header. For
each write the backend logs in as this configured service account, posts the
form exactly once, then performs a bounded sequence of read-only Bug-history
checks and only reports success when the normalized exact note is present.
This tolerates the short consistency delay between ZenTao's HTML form and REST
history without risking a duplicate submission. An HTTP 200 form response that
remains unconfirmed after those reads is treated as an ambiguous write and is
never reported as successful or submitted again automatically. Authored note text
is HTML-escaped at the form boundary so JSX and comparison operators remain literal;
the readback check compares ZenTao's rendered content with the complete original.

## Run locally

From `backend/`, after `uv sync`:

```bash
uv run --package zentao-mcp zentao-mcp
```

The command starts an MCP stdio server; it will appear to wait for input, which
is expected. DeerFlow is the MCP client that communicates with it.

To let DeerFlow start the service, configure a stdio MCP server with:

```json
{
  "command": "uvx",
  "args": [
    "--no-cache",
    "--from",
    "/absolute/path/to/deerflow/backend/packages/zentao-mcp",
    "zentao-mcp"
  ]
}
```

Replace the absolute path with the path to this checkout; Docker uses
`/app/project/backend/packages/zentao-mcp`. `--no-cache` makes `uvx` build the
local package for this MCP process instead of reusing a stale package archive,
so source changes, including token refresh, are always loaded.

## Scope

The MCP exposes `get_bug(bug_id)` for normalized single-Bug reads,
`api_get(path, query)` as a generic REST-v1 GET primitive, and
`daily_bug_report_snapshot(...)` for compact recurring reports. The daily
snapshot resolves products exactly and defensively canonicalizes one unique optional
`家用` prefix back to the real ZenTao name, paginates each product once, filters exact
assignees/status, reads matching histories with bounded concurrency, classifies
explicit cooperation markers, and returns the final compact report text. Note
bodies and repeated per-Bug tool calls never enter the Agent context.

`api_get` accepts
only relative `bugs`, `products`, `projects`, and `users` resource paths,
rejects authentication/write routes and credential parameters, never follows
redirects, and caps response size. It leaves pagination and filtering explicit
so an agent-created skill can learn a reusable workflow from the real API
without receiving the configured Token.

The Workbench can
call the internal `add_bug_note(bug_id, comment)` client method after its
analysis review passes. It posts one note, retries a rejected credential once,
establishes a one-request web session for the form action, then polls only the
idempotent Bug-history read for a bounded period to confirm that the saved note
is visible. Existing editable analysis notes use the same bounded readback after
one edit submission.
It never updates fields, resolves, closes, or reopens a Bug. Network failures
after a write attempt are never retried automatically, preventing duplicates.
