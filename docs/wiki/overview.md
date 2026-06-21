---
title: Overview
blurb: What AprilCam is, how the daemon / MCP server / clients fit together, and where to start.
order: 10
updated: 2026-06-21
tags: [overview]
---

# AprilCam

AprilCam gives AI agents and robot programs a shared, real-time picture of
a robotics playfield — AprilTag/ArUco positions, orientation, velocity, and
homography — without each consumer running its own vision pipeline.

## Architecture

A single long-running **daemon** (`aprilcamd`) owns the cameras, runs
detection, and serves per-frame data. Two front ends sit on top of it:

- **MCP server** (`aprilcam mcp`) — tools for AI agents to open cameras,
  create playfields, query tags, capture raw frames, and look up playfield
  features over the Model Context Protocol. Vision processing (detection,
  homography, deskew) runs exclusively in the daemon; the MCP server is a
  thin client that returns perception results and raw frames only.
- **Direct gRPC client** (`DaemonControl`) — a low-latency Python API for
  robot control loops that read tags and draw live overlays at 5–50 Hz.

Both talk to the daemon over the same gRPC service and protobuf stream
sockets.

## Where to start

- **[Using the MCP Server](mcp-server.md)** — for AI agents: open cameras,
  build playfields, read tags/objects, capture frames, and draw paths and
  overlays over the Model Context Protocol.
- **[Robot Direct API](robot-direct-api.md)** — the `DaemonControl` Python
  client for high-frequency tag reads and live overlay drawing in a control
  loop.
- **[Operating the Daemon](daemon.md)** — install, run, configure, and
  troubleshoot `aprilcamd`.
- **[Daemon Wire Protocol](daemon-interface.md)** — the gRPC service and
  protobuf stream framing, for building a client in another language.
- **[Tag Detection Under Variable Lighting](lighting-and-detection.md)** —
  why tags drop out under glare and the preprocessing pipeline that
  recovers them.

## Install tiers

OpenCV is a **daemon-only** dependency. The machine that runs the daemon (the
one the cameras are plugged into) needs the `daemon` extra; everything else —
the MCP server, the `DaemonControl` library, the `aprilcam view` window — is a
thin client that needs only the base install.

```
# Camera host (runs the daemon + does all vision):
pipx install 'aprilcam[daemon]'    # base + OpenCV, mss, websockets, …

# Thin clients (MCP server, library, viewer) — no OpenCV:
pipx install aprilcam              # includes the MCP SDK

# Develop against a checkout (editable, full daemon stack):
pipx install --editable '/path/to/aprilcam[daemon]'
```

On a Raspberry Pi the daemon installs the same way (`pipx install
'aprilcam[daemon]'` from a built wheel) and runs as a systemd service — see
[Operating the Daemon](daemon.md).

```
aprilcam daemon start               # start the daemon on the camera host
aprilcam mcp                        # run the MCP server (stdio) against a daemon
aprilcam probe                      # discover daemons + cameras, assign short codes
aprilcam cameras                    # list cameras (persistent number + alpha code)
aprilcam --host vidar.local cameras # target a remote daemon over the network
aprilcam view 3                     # open the live view for camera 3
```

Clients reach the daemon by **mDNS auto-discovery**, or an explicit `--host` /
`APRILCAM_DAEMON_HOST` — they never start a daemon themselves. See
[Operating the Daemon](daemon.md#connecting-local-and-remote).

Camera numbers shown by `aprilcam cameras` (and accepted by `view`, `tags`,
`calibrate`, and the MCP tools) are the **persistent enumeration handle** —
stable across replug — not the volatile OS device index. `aprilcam probe` also
assigns a compact base-26 code (e.g. `AB`) for addressing cameras across hosts.

See the repository [README](https://github.com/League-Robotics/aprilcam#readme)
for the full MCP tool reference and CLI commands.
