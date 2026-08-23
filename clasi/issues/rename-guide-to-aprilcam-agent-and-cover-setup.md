---
status: pending
---

# Rename "AprilCam Guide" to "AprilCam Agent" and make it cover setup end to end

## Description

An agent handed this project should be able to run one thing —
**`aprilcam agent`** — and get from nothing to a working, queryable
system. Two problems block that today.

**1. The name.** The packaged onboarding text is called a *guide*:
`aprilcam guide [agent|robot]`, the `get_guide` MCP tool,
`aprilcam.guides`, `client.guides.Guides`, `Daemon.get_guide`,
`AGENT_GUIDE.md`. "Guide" undersells it and is not what anyone reaches
for. It should be **AprilCam Agent** — `aprilcam agent`.

**2. The content stops short of setup.** `AGENT_GUIDE.md` opens by
promising it answers "what is this and how do I use it *before anything
is configured, before a daemon is even running*" — and then its bring-up
order (§3) starts at step 1, `aprilcam daemon start`. Everything before
that is missing:

- **Installing** — nothing states that the daemon and MCP server need
  `pipx install 'aprilcam[daemon]'` while a robot client needs only the
  base install. An agent on a base install that tries to start a daemon
  hits an OpenCV import error with no context.
- **Registering the MCP server** — no `.mcp.json` block, no mention of
  `aprilcam mcp [-d HOST]`, and no statement of the crucial fact that
  **the MCP server never starts a daemon** — it connects to one, so
  registering the server is not the same as bringing the system up. v1
  had an `aprilcam init` that wrote the `.mcp.json` / `.vscode/mcp.json`
  entries; v2 dropped it and replaced it with nothing.
- **Connecting / the socket** — the guide never says how a client finds
  a daemon. In v2 that is: `-d HOST` flag, then
  `APRILCAM_DAEMON_HOST`, then the local unix socket
  (`Directories.socket_dir / SOCKET_FILENAME`, dialed as
  `unix:<path>` — `client/discovery.py:235`), then real mDNS browse of
  `_aprilcam._tcp.local.` (`client/discovery.py`, sprint 028). None of
  that is in either guide, and **there is no current document to point
  at instead**: [docs/wiki/daemon.md](../../docs/wiki/daemon.md)
  describes the v1 CLI (`aprilcam probe`, host letters, global
  `--host`), which v2 does not have.

So the one text that is explicitly designed to be readable with no
daemon running — its whole architectural point, UC-27.S-1, a pure
file read with no client or network dependency — is also the one text
that declines to explain how to get a daemon running.

## Proposal

### Part 1 — the rename

User-facing surfaces to rename:

- `aprilcam guide` → **`aprilcam agent`** (`cli/guide.py` →
  `cli/agent.py`, wiring in `cli/__init__.py`).
- The document title in `AGENT_GUIDE.md` ("AprilCam Agent Guide" →
  "AprilCam Agent") and its cross-references from
  `ROBOT_API_GUIDE.md`.
- The `get_guide` MCP tool, plus the `_SERVER_INSTRUCTIONS` golden-path
  text in `mcp/server.py:66` that names it. **Keep `get_guide` as a
  deprecated alias** — it is the documented first call, and any agent
  or config carrying the old instruction should not break.

Two open decisions for whoever picks this up:

- **The sub-name collision.** "agent" is currently the *name of one of
  the two guides* (`agent` vs `robot`), so a literal rename yields
  `aprilcam agent robot`, which reads badly. Options: (a) `aprilcam
  agent` for the agent text with `--robot` for the other; (b) two
  sibling commands, `aprilcam agent` and `aprilcam robot`, dropping the
  container noun entirely — cleanest to read; (c) keep the
  `<container> <name>` shape and accept the awkwardness. **(b) is the
  recommendation.**
- **How deep to rename.** `Daemon.get_guide` is a **proto RPC**
  (`daemon/interfaces.py:448`, `daemon/rpc.py`, generated
  `proto/aprilcam_v2_pb2.py`) — renaming it is a wire-compatibility
  break for a mixed-version daemon/client pair. Recommend scoping the
  rename to user-facing names and leaving the RPC, `aprilcam.guides`
  module, `client.guides.Guides`, and `UnknownGuide`
  (`errors.py:80`, `kind = "guide"`) alone unless there is a reason to
  take the break.

Do not miss `pyproject.toml:83` — `[tool.setuptools.package-data]`
declares `AGENT_GUIDE.md` / `ROBOT_API_GUIDE.md` by name. If the files
are renamed and that list is not, the wheel ships without them and
every accessor fails only in an installed build, never in the source
checkout. Tests to update: `tests/aprilcam/cli/test_guide_cli.py`,
`client/test_guides.py`, `mcp/test_server.py`,
`mcp/test_integration.py`, `daemon/test_service.py`,
`client/test_daemon_client.py`.

### Part 2 — the setup content

Add a section ahead of the current §3 bring-up order, so the sequence
starts from a bare machine. It must stay a **static packaged text with
no daemon dependency** (UC-27.S-1) — which is exactly why setup belongs
here rather than in a tool response:

1. **Install tiers** — `pipx install 'aprilcam[daemon]'` on the camera
   host (daemon + MCP server, pulls OpenCV); base install for a robot
   client; what breaks if you get it backwards.
2. **Register the MCP server** — the copy-pasteable `.mcp.json` block,
   `aprilcam mcp [-d HOST]`, and the statement that the server resolves
   a daemon *per tool call*, so it starts fine with no daemon and
   survives a daemon restart. Consider restoring an `aprilcam init` that
   writes the block, since an agent editing its own MCP config is
   awkward.
3. **Start the daemon** — `aprilcam daemon start`, verify with
   `daemon status`, and `daemon list` for what is reachable.
4. **Connecting, including the socket** — the resolution order (`-d` >
   `APRILCAM_DAEMON_HOST` > local unix socket > mDNS), where the socket
   actually lives and how `socket_dir` resolves, and the TCP/mDNS path
   for a remote daemon. This needs a **current** reference to point at
   for the details; `docs/wiki/daemon.md` is v1-stale, so either refresh
   it as part of this issue or keep the essentials inline.
5. **Confirm you are on v2** — `aprilcam agent` existing at all is a
   usable discriminator (v1 has no such command). See the related
   stale-docs issue: a bare `aprilcam` on PATH may resolve to a v1
   install whose MCP tool set is entirely different.

## Motivation

Raised while writing onboarding instructions for other agents (Aug
2026). The correct instruction turned out to be almost entirely "call
`get_guide()` first" — the packaged text is genuinely the best doc in
the project — but it could not be the *only* instruction, because it
covers neither installing the thing nor connecting to it, and the wiki
pages that do cover those describe v1. Folding setup into the one
always-readable, always-shipped text collapses that back to a single
instruction, and `aprilcam agent` is the name people will actually
reach for.

Related:
[camera-locate-missing-from-mcp-surface-and-agent-guide.md](camera-locate-missing-from-mcp-surface-and-agent-guide.md)
— another gap in the same document; worth fixing in the same pass.
