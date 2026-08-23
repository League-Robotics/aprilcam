---
title: Overview
blurb: What AprilCam v2 is, how the daemon / MCP server / client library / viewer fit together, the install tiers, and where to start.
order: 2
updated: 2026-08-23
tags: [overview, architecture, install]
---

# AprilCam

AprilCam gives AI agents and robot programs a shared, real-time picture of a
robotics playfield — AprilTag/ArUco positions, orientation, velocity, and a
deskewed top-down view — without each consumer running its own vision
pipeline. An overhead camera watches a field marked with fiducials; one
process turns those frames into world-coordinate perception data that any
number of clients read at the same time.

This wiki documents **AprilCam v2**, the ground-up rebuild. If you used
AprilCam before the rebuild, assume nothing carried over — command shapes,
the client API, and the wire protocol are all new.

## Architecture

A single long-running **daemon** (`aprilcamd`, managed as `aprilcam daemon
start|stop|status`) is the **sole camera owner and the sole vision
authority**. Every OpenCV operation — capture, AprilTag/ArUco detection,
tracking, homography, deskew, calibration — runs inside the daemon process
and nowhere else. Derived products are computed once and published to all
subscribers rather than recomputed per client. The daemon serves four
things:

1. **Deskewed, calibrated top-down images** of the playfield.
2. **Live tag records** — id, world position (cm), heading, velocity —
   corrected for lens distortion, parallax, and robot tag mounting.
3. **A configuration service** — playfield definitions and camera configs,
   pushed and served as whole documents to any client, local or remote.
4. **Shared annotation state** — world-coordinate paths, markers, and
   symbols that any client can push and every other client renders locally.

Everything else is a **thin client** speaking the daemon's gRPC service
(`AprilCamV2`) and its length-prefixed protobuf stream sockets:

- **MCP server** (`aprilcam mcp`) — a FastMCP stdio server exposing
  perception, configuration, mobile-tag, and annotation tools to AI agents
  over the Model Context Protocol. See [Using the MCP
  Server](mcp-server.md).
- **Python client library** (`aprilcam.client`) — `Discovery().connect()`
  returns a daemon handle for robot control loops that read tags and push
  overlays at 5–50 Hz. See the [Robot Direct API](robot-direct-api.md).
- **Viewer** — a basic Tk live-view window, opened per camera with
  `aprilcam camera view` (raw feed) or `aprilcam playfield view` (deskewed,
  annotated top-down feed).
- **CLI** — every `aprilcam` subcommand except `guide`, `config`, and
  `tags generate` is itself a thin daemon client.

Clients find daemons by **mDNS** (`_aprilcam._tcp` via zeroconf — `aprilcam
daemon list` shows everything discovered on the network), or target one
explicitly with `-d`/`--daemon HOST` or the `APRILCAM_DAEMON_HOST`
environment variable. Clients never start a daemon themselves.

One convention underpins all of it: **world coordinates first**. Positions
are exchanged in centimetres in a fixed world frame (origin at corner
marker A1, +x east, +y north; heading 0 = east, counter-clockwise
positive), never in pixels — pixel coordinates exist only inside the
daemon's detection internals and at a client's final render step. Tags are
addressed by family plus number (e.g. AprilTag 7), never a bare id.

## The CLI

One entry point, `aprilcam`, with seven subcommand trees: `daemon`,
`camera`, `playfield`, `tags`, `guide`, `config`, and `mcp`.

```
aprilcam daemon start                 # start aprilcamd on the camera host
aprilcam daemon list                  # every daemon discovered via mDNS
aprilcam camera list                  # cameras a daemon can see
aprilcam camera image front -o f.jpg  # capture one raw frame
aprilcam camera view front            # live raw-video window
aprilcam playfield view               # live deskewed, calibrated window
aprilcam playfield where main "the blue dot"   # surveyed-feature lookup
aprilcam tags generate --family apriltag --ids 1-10 --size-cm 10 --out ./tags
aprilcam guide                        # print the packaged agent guide ('robot' for the other)
aprilcam mcp                          # run the MCP stdio server
```

Cameras are addressed by **slug or persistent number** — a stable identity
the daemon maintains across replugs, not the volatile OS device index. The
`camera` tree also holds `calibrate` (establish a camera's pixel-to-world
mapping against its linked playfield) and `locate` (solve a camera's ground
nadir and optical height from surveyed reference tags); `tags mount`
registers a robot tag's mount geometry so reports become the robot's
position rather than the raw tag's. `aprilcam guide` prints the same
agent/robot usage guides the daemon serves over RPC, and works with no
daemon running at all.

## Install tiers

AprilCam requires Python ≥ 3.10 and installs with pip, pipx, or uv. The
**base install** is the full client stack — gRPC, mDNS discovery, Pillow,
and the MCP SDK — enough to run the MCP server, the client library, the
CLI, and the viewer against a remote daemon. The **`daemon` extra** adds
what only the camera host needs: camera enumeration, screen capture, the
stream transports, and gRPC reflection.

```
# Camera host (often a Raspberry Pi over the field) — runs aprilcamd:
pipx install 'aprilcam[daemon]'

# Any other machine — MCP server, client library, CLI, viewer:
pipx install aprilcam

# Develop against a checkout:
pip install -e '.[daemon,dev]'
```

Two footnotes for the honest reader: OpenCV currently ships in the *base*
install rather than behind the `daemon` extra (the design target is
daemon-only OpenCV; this is a known packaging gap), and the `imaging` extra
exists but is currently empty. A `dev` extra adds notebook tooling and
`grpcio-tools` for regenerating the protobuf bindings.

Running the daemon as a systemd service, camera configuration, and
troubleshooting are covered in [Operating the Daemon](daemon.md).

## Where to go next

- **[Using the MCP Server](mcp-server.md)** — for AI agents: read tags,
  capture raw and deskewed frames, push annotations, and manage
  configuration through MCP tools.
- **[Robot Direct API](robot-direct-api.md)** — the `aprilcam.client`
  Python library for high-frequency tag reads and live overlay drawing in
  a control loop, bypassing the MCP layer.
- **[Operating the Daemon](daemon.md)** — install, run, configure, and
  troubleshoot `aprilcamd`.
- **[Daemon Wire Protocol](daemon-interface.md)** — the `AprilCamV2` gRPC
  service and the protobuf stream framing, for building a client in
  another language.
- **[Tag Detection Under Variable Lighting](lighting-and-detection.md)** —
  why tags drop out under glare and the preprocessing pipeline that
  recovers them.
- **[Docs index](index.md)** — the full page map for this subsystem.

For the complete MCP tool reference and development workflow, see the
repository
[README](https://github.com/League-Robotics/aprilcam#readme).
