---
status: pending
---

# Remove the web server and `aprilcam web`

Per [specification.md](../../docs/design/specification.md) §6
(stakeholder decision): the HTTP/WS surface is dropped. Consumers are
the MCP server, the viewer, robot code via the client library, and a
future ROS bridge; the web server adds a parallel API surface nobody
needs.

## Scope

- Delete `src/aprilcam/server/web_server.py` and `src/aprilcam/cli/web_cli.py`;
  remove `web` from the CLI dispatcher and docs.
- Remove web-only dependencies (starlette, uvicorn, websockets) from
  packaging extras.
- Remove/update tests and wiki pages that reference the web interface.

Note: this also moots three known defects — `aprilcam web` currently
**crashes on launch** (`argparse.ArgumentError: conflicting option
string: --host` from duplicate `--host`/`--port` registration; verified
2026-07-29), `web_cli.py` advertises a nonexistent `/mcp/sse` route, and
`web_cli.py` drops `add_daemon_args` values by calling `create_app()`
with no arguments. The command is already unusable, so removal costs
nothing.
