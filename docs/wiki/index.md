---
title: AprilCam Docs
blurb: Navigation map for the AprilCam subsystem — daemon, MCP server, direct library, and the wire protocol.
order: 1
updated: 2026-06-20
tags: [index, navigation]
---

# AprilCam Documentation

AprilCam gives AI agents and robot programs a shared, real-time picture of a
robotics playfield — AprilTag/ArUco positions, orientation, velocity, and
homography — served by one daemon that owns the cameras. Pick your entry point:

## Start here

- **[Overview](overview.md)** — what AprilCam is, how the daemon / MCP server /
  clients fit together, and the install tiers.

## Use it

- **[Using the MCP Server](mcp-server.md)** — for AI agents. Open cameras,
  build playfields, read tags/objects, capture frames, draw paths and
  overlays, and look up playfield features over the Model Context Protocol.
  The MCP server is a thin client and needs **no OpenCV**.
- **[Robot Direct API (the library)](robot-direct-api.md)** — the
  `DaemonControl` Python client for robot control loops: high-frequency tag
  reads and live overlay drawing at 5–50 Hz, straight over gRPC.
- **[Operating the Daemon](daemon.md)** — install, run, configure, and
  troubleshoot `aprilcamd`: the CLI lifecycle, environment variables,
  directory layout, systemd, and logs.

## Build against it

- **[Daemon Wire Protocol](daemon-interface.md)** — the gRPC control service,
  the length-prefixed protobuf stream sockets, and every message schema. Read
  this to implement a client in another language.

## Background

- **[Tag Detection Under Variable Lighting](lighting-and-detection.md)** — why
  tags drop out under glare and the preprocessing pipeline that recovers them.

---

*Maintainers: this wiki is published to the hub automatically. See
[`/AGENTS.md`](https://github.com/League-Robotics/aprilcam/blob/master/AGENTS.md)
for how to keep these pages in sync with the code and how publishing works.*
