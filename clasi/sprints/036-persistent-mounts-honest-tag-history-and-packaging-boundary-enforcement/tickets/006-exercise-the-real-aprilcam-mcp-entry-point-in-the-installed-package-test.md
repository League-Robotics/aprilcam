---
id: '006'
title: Exercise the real aprilcam mcp entry point in the installed-package test
status: open
use-cases: [SUC-004]
depends-on: ["005"]
github-issue: ''
issue: installed-package-test-does-not-exercise-aprilcam-mcp.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Exercise the real aprilcam mcp entry point in the installed-package test

## Description

2026-08-23: an agent's MCP client failed against a fresh pipx install
because `pyproject.toml` declared `mcp>=1.0` with no upper bound, and
the MCP Python SDK's 2.0.0 release moved `mcp.server.fastmcp` to
`mcp.server.mcpserver` — `src/aprilcam/mcp/server.py:33` imports the
old path, so any environment resolving to mcp 2.x got a
`ModuleNotFoundError` and the server exited 1. The repo's own `.venv`
had mcp 1.26.0 pinned by `uv.lock`, so every existing test passed while
the shipped artifact was broken. Hotfixed by pinning `mcp>=1.0,<2`
(already in `pyproject.toml` — that pin is not this ticket's job to
change).

`tests/packaging/test_installed_package.py` (sprint 035 ticket 013)
builds a wheel, installs into a fresh venv, and checks `import
aprilcam.client`, the console script, `import cv2` (fixed by tickets
004/005), and a camera-free consumer round-trip — but never starts
`aprilcam mcp`, the one place a fresh dependency resolution meets the
actual MCP entry point. This ticket closes that gap by reusing
`tests/aprilcam/mcp/test_stdio_offline.py`'s machinery (sprint 035
ticket 009), which already does a real stdio `initialize` handshake
against a subprocess of the **repo's own dev venv** — this ticket
points the same technique at the **fresh, installed** venv instead.

Migrating to the mcp 2.x API is explicitly out of scope (tracked
separately per the issue's "Longer-term" section) — this ticket is
about the missing test, not the pin.

**Depends on ticket 005**: both this ticket and ticket 005 edit
`tests/packaging/test_installed_package.py`. Implement this ticket
against the file state ticket 005 leaves behind (the new
`client.renderer` import assertion) rather than an earlier version, to
avoid rebasing a hand-written diff.

## Acceptance Criteria

- [ ] `tests/packaging/test_installed_package.py` gains a new test
      (module-scoped fixture reuse where sensible — the existing
      `fresh_base_venv`/`built_wheel` fixtures are **base-only**, so
      this new test needs its **own** fresh venv fixture with the
      `daemon` extra installed, since `aprilcam mcp` needs whatever
      `mcp` resolves to plus the daemon-side deps the server module
      chain touches at import — confirm exactly what `aprilcam mcp`
      needs to *start*, not just what `mcp.server` imports, before
      assuming `[daemon]` alone suffices, and adjust to `[daemon]`
      plus whatever else is missing if `aprilcam mcp` fails to start
      with `[daemon]` alone).
- [ ] The new test starts `aprilcam mcp` as a real subprocess against
      the fresh, extra-installed venv's own console script, isolated
      the same way `test_stdio_offline.py` isolates it
      (`APRILCAM_CONFIG_DIR`/`APRILCAM_STATE_DIR`/`XDG_RUNTIME_DIR`
      pointed at private tmp roots so it never reaches a real daemon
      or the stakeholder's real config).
- [ ] Completes a real `initialize` handshake over stdio using
      `mcp.client.stdio.stdio_client`/`mcp.ClientSession` (the same
      client-side machinery `test_stdio_offline.py` already uses),
      then shuts the subprocess down cleanly.
- [ ] Asserts the pinned tool inventory — reuse or mirror
      `test_stdio_offline.py`'s `_EXPECTED_TOOLS` frozenset (27 tools
      as of that ticket; confirm the current count/set hasn't changed
      since) as a superset assertion against the live server's
      `tools/list`.
- [ ] Asserts the resolved `mcp` package version (in the fresh venv)
      satisfies the declared `>=1.0,<2` constraint from
      `pyproject.toml` — read the constraint from the built wheel's
      metadata or `pyproject.toml` directly, don't hardcode `<2`
      twice in two places that could drift.
- [ ] The new test is marked `@pytest.mark.integration` (matching
      every other test in this module) and reuses the module's
      existing `built_wheel` fixture for the wheel artifact, adding
      only the extra-installed-venv fixture as new.

## Testing

- **Existing tests to run**:
  `uv run pytest tests/packaging/test_installed_package.py tests/aprilcam/mcp/test_stdio_offline.py -q`
  — both are slow (`test_installed_package.py` builds a wheel and one
  or more fresh venvs; this ticket adds a second fresh-venv build to
  that module, so expect the module's total runtime to increase
  noticeably).
- **New tests to write**: the `aprilcam mcp` real-subprocess handshake
  test described above, in `tests/packaging/test_installed_package.py`.
- **Verification command**:
  `uv run pytest tests/packaging/test_installed_package.py -q`
