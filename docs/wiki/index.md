---
title: AprilCam v2 Docs
blurb: Navigation map for the AprilCam v2 subsystem — daemon, MCP server, robot client library, and the wire protocol.
order: 1
updated: 2026-08-23
tags: [index, navigation]
---

# AprilCam v2 Documentation

AprilCam gives AI agents and robot programs a shared, real-time picture of a
robotics playfield — AprilTag/ArUco positions, orientation, velocity, and
homography — served by one daemon that owns the cameras and does all the
vision. This wiki documents **AprilCam v2**, the ground-up rebuild: a new
gRPC protocol, a world-coordinate-first tag pipeline, and thin clients that
never touch pixels. Pick your entry point:

## Start here

- **[Overview](overview.md)** — what AprilCam v2 is, how the daemon / MCP
  server / client library / viewer fit together, the world-coordinate
  conventions, and the install tiers.

## Use it

- **[Using the MCP Server](mcp-server.md)** — for AI agents. Run
  `aprilcam mcp`, follow the golden path, and drive the full tool catalog —
  perception, configuration and calibration, mobile tags, and shared
  annotations — over the Model Context Protocol.
- **[Robot Direct API (the library)](robot-direct-api.md)** — the
  `aprilcam.client` Python library for robot control loops: discover and
  connect to the daemon, read tag positions at loop rate, subscribe to tag
  and image streams, and draw shared annotations over gRPC.
- **[Operating the Daemon](daemon.md)** — install, run, configure, and
  troubleshoot the daemon: the `aprilcam daemon` lifecycle, the config
  cascade and directory layout, mDNS advertisement, systemd, and the
  troubleshooting playbook.

## Build against it

- **[Daemon Wire Protocol](daemon-interface.md)** — the `AprilCamV2` gRPC
  control service, the length-prefixed protobuf stream sockets, and the core
  message schemas. Read this to implement a client in another language.

## Background

- **[Tag Detection Under Variable Lighting](lighting-and-detection.md)** —
  why tags drop out under glare and low contrast, what the v2 pipeline does
  about it, and how to re-tune exposure for your ambient light.

---

*Maintainers: this wiki is published to the hub automatically on push to
`docs/wiki/**` on master. See
[`/AGENTS.md`](https://github.com/League-Robotics/aprilcam/blob/master/AGENTS.md)
and the source map in `_subsystem.yml` for how to keep these pages in sync
with the code.*
