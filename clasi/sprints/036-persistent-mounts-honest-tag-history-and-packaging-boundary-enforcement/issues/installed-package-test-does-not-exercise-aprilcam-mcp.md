---
status: in-progress
sprint: '036'
tickets:
- 036-006
---

# The installed-package test never starts `aprilcam mcp`, so a broken MCP server ships silently

## What happened

2026-08-23: an agent's MCP client failed to connect with

```
error: aprilcam mcp requires the 'mcp' package, which is not available in
this environment -- install or upgrade the MCP Python SDK with
'pip install --upgrade mcp'
```

Cause: `pyproject.toml` declared `mcp>=1.0` with **no upper bound**. The MCP
Python SDK released 2.0.0, which moved `mcp.server.fastmcp` to
`mcp.server.mcpserver`. `src/aprilcam/mcp/server.py:33` imports
`from mcp.server.fastmcp import FastMCP`, so any environment resolving to
mcp 2.x gets a `ModuleNotFoundError` and the server exits 1.

The repo's own `.venv` had mcp 1.26.0 pinned by `uv.lock`, so every test
passed. The pipx install resolved fresh and got 2.0.0. Every test in the
suite was green while the shipped artifact was broken.

Hotfixed by pinning `mcp>=1.0,<2` and reinstalling. pipx now has 1.29.0 and
`aprilcam mcp` starts clean.

## The gap

`tests/packaging/test_installed_package.py` (sprint 035 ticket 013) builds a
wheel, installs it into a fresh venv, and checks:

- `import aprilcam.client` succeeds
- the console script exists and `-V` matches
- a camera-free consumer round-trip works
- `import cv2` fails (currently xfail — see the base-dependency issue)

It never **starts `aprilcam mcp`**. That is the one check that would have
caught this, because it is the only place where a fresh dependency resolution
meets the actual entry point.

Note this is exactly the class of bug the fresh-venv test exists to catch —
"works in the repo, broken when installed." It just wasn't pointed at the MCP
entry point.

## Proposed fix

Add to `tests/packaging/test_installed_package.py`:

1. **Start `aprilcam mcp` in the fresh venv** and complete a real MCP
   `initialize` handshake over stdio, then shut it down. Reuse the machinery
   in `tests/aprilcam/mcp/test_stdio_offline.py` (sprint 035 ticket 009) —
   it already does exactly this against the repo venv; this points the same
   check at the *installed* one. Assert the pinned 27-tool inventory too, so
   an SDK change that silently drops tool registration also fails.
2. **Assert the resolved `mcp` version satisfies the declared constraint**,
   so a future unpinned upper bound is caught as a dependency fact rather
   than only as a downstream import error.

Consider the same treatment for any other console-script entry point whose
imports are not exercised by the consumer round-trip.

## Longer-term

The `<2` pin is a stopgap. Someone should migrate to the mcp 2.x API
(`mcp.server.mcpserver`) and lift the ceiling, or deliberately decide to stay
on 1.x and document why. Track that separately — this issue is about the
missing test, not the pin.

## Related

- Sprint 035 ticket 013 (`5dedd70`) — the installed-package test
- Sprint 035 ticket 009 (`c6746b0`) — the stdio handshake machinery to reuse
- `src/aprilcam/mcp/server.py:33`, `src/aprilcam/mcp/errors.py:73`,
  `src/aprilcam/mcp/images.py:33` — the mcp imports
