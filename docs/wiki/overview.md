---
title: Overview
blurb: What AprilCam is, how the daemon / MCP server / clients fit together, and where to start.
order: 10
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

- **[Robot Direct API](robot-direct-api.md)** — the Python client for
  high-frequency tag reads and live overlay drawing in a control loop.
- **[Daemon Wire Protocol](daemon-interface.md)** — the gRPC service and
  protobuf stream framing, for building a client in another language.
- **[Tag Detection Under Variable Lighting](lighting-and-detection.md)** —
  why tags drop out under glare and the preprocessing pipeline that
  recovers them.

## Install & run

```
pipx install 'aprilcam[daemon]'   # full server stack
aprilcam mcp                       # run the MCP server (stdio)
aprilcam daemon start              # or run the daemon directly
```

See the repository [README](https://github.com/League-Robotics/aprilcam#readme)
for install tiers, the full MCP tool reference, and CLI commands.
