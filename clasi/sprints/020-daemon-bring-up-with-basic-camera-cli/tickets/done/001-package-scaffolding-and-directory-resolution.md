---
id: '001'
title: Package scaffolding and directory resolution
status: done
use-cases:
- UC-01
depends-on: []
github-issue: ''
issue: daemon-bring-up-with-basic-camera-cli.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Package scaffolding and directory resolution

## Description

Foundation ticket. No prior ticket to depend on. Implements
`config/directories.py` — the `Directories` Protocol
(`src/aprilcam/config/interfaces.py`) — resolving `config_dir`,
`state_dir`, and `socket_dir` per the cascade in
`docs/design/configuration.md`: flag > env (`APRILCAM_CONFIG_DIR`,
`APRILCAM_STATE_DIR`) > file > built-in default
(`/etc/aprilcam` / `/var/lib/aprilcam`, falling back to
`~/.config/aprilcam` / `~/.local/share/aprilcam` when the system paths
are unavailable, per `configuration.md` §2). Sockets resolve to
`/run/aprilcam` or `$XDG_RUNTIME_DIR/aprilcam` — not independently
configurable, per `configuration.md` §1.

**Resolves Sprint 020 architecture Open Question 2**: this ticket opens
only the unix-domain socket this sprint (no TCP:5280 listener yet) —
every in-scope CLI command works against a local daemon or an
explicitly-named `-d HOST` via direct TCP connect from the client side,
neither of which requires the daemon itself to also *listen* on TCP.
Opening TCP:5280 is deferred to whichever future sprint actually
exercises a daemon-to-daemon or CLI-to-remote-daemon path end-to-end
with a real second host. Record this explicitly in the module's
docstring so it reads as a decision, not an oversight.

This ticket also creates the `pyproject.toml` `[project.scripts]`
entry point wiring (`aprilcam = "aprilcam.cli:main"` already declared;
this ticket adds the actual `src/aprilcam/cli/__init__.py` with a
`main()` stub that ticket 006 fills in) and the `tests/aprilcam/`
test package layout with the `@pytest.mark.camera` marker already
registered in `pyproject.toml`.

**Reuse from archive**: none directly — `archive/` has no equivalent
directory-cascade module (v1 used a more elaborate FHS/XDG scheme
per `docs/design/configuration.md`'s framing). This is new code
implementing the already-designed `Directories` Protocol.

## Acceptance Criteria

- [x] `Directories.config_dir` / `.state_dir` / `.socket_dir` resolve
      correctly for: (a) no env vars, no flags, system paths writable —
      resolves to `/etc/aprilcam` / `/var/lib/aprilcam` /
      `/run/aprilcam`; (b) system paths unavailable — falls back to
      `~/.config/aprilcam` / `~/.local/share/aprilcam`; (c)
      `APRILCAM_CONFIG_DIR` / `APRILCAM_STATE_DIR` override the
      default; (d) resolution is reported (logged or returned) so
      which roots were chosen is never a mystery (UC-01's "config
      cascade is never a mystery" framing).
- [x] `src/aprilcam/cli/__init__.py` exists with a `main()` entry point
      stub (no subcommands yet — ticket 006 adds them) and
      `aprilcam --help` runs without error after `pip install -e .`.
- [x] `tests/aprilcam/` package exists; a trivial headless test
      confirms the directory cascade logic with `tmp_path` and
      monkeypatched env vars — no hardware, no daemon required.

## Testing

- **Existing tests to run**: none yet exist for `src/aprilcam/` (first
  implementation ticket in the sprint).
- **New tests to write**: `tests/aprilcam/config/test_directories.py` —
  cascade precedence (flag > env > file > default) with monkeypatched
  `HOME`/env vars and `tmp_path` standing in for system roots; fallback
  behavior when system roots are unwritable/nonexistent.
- **Verification command**: `uv run pytest tests/aprilcam/config/`
