---
id: "004"
title: "--agent flag in CLI and read_guide() helper in guides.py"
status: open
use-cases:
  - SUC-005
depends-on: []
github-issue: ""
issue: ""
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# --agent flag in CLI and read_guide() helper in guides.py

## Description

Add the ability to print AI-agent instructions from the CLI, and consolidate
the guide-file reading logic into a shared helper so the CLI and MCP server
use identical code.

**Part A — New module `src/aprilcam/guides.py`:**

```python
"""Shared guide-file reader used by the CLI and MCP server."""
from pathlib import Path

_GUIDE_DIR = Path(__file__).parent  # src/aprilcam/

_GUIDE_MAP = {
    "agent": "AGENT_GUIDE.md",
    "robot": "ROBOT_API_GUIDE.md",
}


def read_guide(name: str) -> str | None:
    """Return the text of a packaged guide file, or None if the name is unknown.

    name: 'agent' -> AGENT_GUIDE.md
          'robot' -> ROBOT_API_GUIDE.md
    """
    filename = _GUIDE_MAP.get(name.lower().strip())
    if filename is None:
        return None
    return (_GUIDE_DIR / filename).read_text(encoding="utf-8")
```

**Part B — `--agent` flag in `src/aprilcam/cli/__init__.py`:**

In `main()`, insert before the subcommand dispatch block:

```python
if args[0] == "--agent":
    guide_name = args[1] if len(args) > 1 else "agent"
    from aprilcam.guides import read_guide
    content = read_guide(guide_name)
    if content is None:
        available = "agent, robot"
        print(
            f"aprilcam: unknown guide '{guide_name}'. Available: {available}",
            file=sys.stderr,
        )
        sys.exit(1)
    print(content)
    sys.exit(0)
```

Also add `--agent` to the flags section of `_print_help()`:

```
  --agent [NAME]    Print the AI-agent instructions guide (NAME: agent [default], robot)
```

**Part C — Refactor `src/aprilcam/server/mcp_server.py`:**

Replace the two inline `(_PACKAGE_DIR / "...").read_text()` calls with
`read_guide()`:

```python
# In _resource_robot_api():
from aprilcam.guides import read_guide
return read_guide("robot")

# In _resource_agent_guide():
return read_guide("agent")

# In get_robot_api_guide():
return [TextContent(type="text", text=read_guide("robot"))]
```

Remove the `_PACKAGE_DIR` constant if it is only used by these three
callsites (grep first to confirm no other uses).

**Part D — Refresh `src/aprilcam/AGENT_GUIDE.md`:**

Add a short "Directory layout" section near the top of the guide (after the
quick-start, before or after the first major section):

```markdown
## Directory Layout

AprilCam uses FHS directories when running as root and XDG directories
for non-root use. See `aprilcam config` for the current resolved paths.

| Concern | System (root) | Developer (non-root) | Override |
|---------|--------------|----------------------|---------|
| Data (persistent) | `/var/lib/aprilcam` | `~/.local/share/aprilcam` | `APRILCAM_DATA_DIR` |
| Runtime (sockets) | `/run/aprilcam` | `$XDG_RUNTIME_DIR/aprilcam` | `APRILCAM_SOCKET_DIR` |
| Logs | `/var/log/aprilcam` | `~/.local/state/aprilcam` | `APRILCAM_LOG_DIR` |

Run `aprilcam --agent` (or `aprilcam --agent robot` for the robot API guide)
to print this guide to stdout from any shell context.
```

## Acceptance Criteria

- [ ] `src/aprilcam/guides.py` exists with `read_guide(name)` returning the
      correct file content for `"agent"` and `"robot"`.
- [ ] `read_guide("unknown")` returns `None`.
- [ ] `aprilcam --agent` prints `AGENT_GUIDE.md` to stdout and exits 0.
- [ ] `aprilcam --agent robot` prints `ROBOT_API_GUIDE.md` to stdout and exits 0.
- [ ] `aprilcam --agent unknown` prints an error to stderr and exits non-zero.
- [ ] `aprilcam --help` lists `--agent` in the flags/options section.
- [ ] `mcp_server.py` guide resources and the `get_robot_api_guide` tool use
      `read_guide()` (no inline `.read_text()` for guide files).
- [ ] `AGENT_GUIDE.md` contains a "Directory Layout" section with the FHS/XDG
      table.
- [ ] `AGENT_GUIDE.md` mentions `aprilcam --agent`.
- [ ] `uv run pytest tests/test_cli_dispatch.py` passes.

## Implementation Plan

### Approach

Four files in dependency order: guides.py first (no deps), then CLI,
then mcp_server.py refactor, then AGENT_GUIDE.md content edit.
This ticket is independent of Tickets 001-003 (no config.py dependency).

### Files to create/modify

- `src/aprilcam/guides.py` — new file (~20 lines).
- `src/aprilcam/cli/__init__.py` — add `--agent` branch in `main()`; add
  `--agent` line to `_print_help()`.
- `src/aprilcam/server/mcp_server.py` — replace inline `.read_text()` calls
  with `read_guide()`; remove `_PACKAGE_DIR` if no longer needed.
- `src/aprilcam/AGENT_GUIDE.md` — add directory layout section and
  `aprilcam --agent` mention.

### Testing plan

Ticket 007 adds CLI smoke tests for `--agent`. For now, manual verification:
```
uv run aprilcam --agent | head -5
uv run aprilcam --agent robot | head -5
uv run aprilcam --agent bad_name; echo "exit: $?"
```

Also: `uv run pytest tests/test_cli_dispatch.py -x`

### Documentation updates

`AGENT_GUIDE.md` is updated as Part D of this ticket.
