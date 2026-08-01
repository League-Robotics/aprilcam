# Core and Adapters

**Status:** draft 2026-07-30 · **Owner:** Eric Busboom
**Related:** [design.md](design.md) · [specification.md](specification.md) · [use-cases/](use-cases/)

The structural rule for v2: **one small, stable daemon, surrounded by
adapters that translate it into whatever a given consumer speaks.**

---

## 1. The shape

```
                 ┌──────────────────────────────┐
                 │          aprilcamd           │
                 │  cameras · vision · geometry │
                 │  gRPC control + streams      │
                 └──────────────┬───────────────┘
                                │  one wire contract
                 ┌──────────────┴───────────────┐
                 │      client library          │   Python, cv2-free
                 └──────────────┬───────────────┘
                                │
        ┌───────────────┬───────┴───────┬────────────────┐
        │               │               │                │
    ┌───┴───┐      ┌────┴────┐     ┌────┴────┐     ┌─────┴─────┐
    │  CLI  │      │   MCP   │     │  ROS    │     │  robot    │
    │       │      │ server  │     │  node   │     │  program  │
    └───────┘      └─────────┘     └─────────┘     └───────────┘
      humans         AI agents       ROS graph      direct client
```

Every adapter is a **translation layer and nothing more**. It converts
between the daemon's contract and a consumer's native idiom — argv and
exit codes, MCP tool calls, ROS topics. None of them holds authoritative
state, and none of them does vision.

## 2. Why the daemon stays small

The daemon is the only component that **cannot** be casually restarted:
it owns the cameras, it holds the calibration, and everything else
depends on it being up. That asymmetry is the whole argument.

- **It is the hardest to test.** Real cameras, real light, real
  geometry. Every capability that lives in the daemon is a capability
  that needs hardware to exercise. Adapters can be tested against a
  fake.
- **Its failures are the most expensive.** A crashed CLI costs one
  command. A crashed daemon takes down every consumer at once, mid-run.
- **It is the shared dependency.** Four consumers rely on it, so a
  daemon change risks four regressions; an adapter change risks one.
- **It cannot be worked around.** If the MCP server is missing a tool,
  an agent can fall back to the CLI. If the daemon is missing a
  capability, nobody has it.

So the rule is: **capability goes in the daemon only when it must.** The
tests are (a) does it need camera access or vision, (b) would two
adapters otherwise duplicate it, (c) does it need to be computed **or
held** once and shared. If none of those hold, it belongs in an adapter
or in the client library.

The annotation store (spec §9 #8) is the worked example for "held once
and shared": it fails (a) — no vision — but passes (b) and (c). The
viewer, robot programs, and MCP agents all need the same drawings;
without daemon storage, every drawing client would have to run its own
query server for the others — which is adapters talking to each other,
forbidden by rule §5.3. The daemon holds the state (in memory, like the
mobile-tag registry) and embeds it in frames; it never renders it.

The corollary is that **adding an adapter must never require changing
the daemon.** A new consumer that needs a new RPC is a signal to check
whether the requirement is real, or whether the adapter is asking the
daemon to do the adapter's job.

## 3. What the daemon owns

Per [specification.md](specification.md) §2, and unchanged here:

- camera ownership and identity; the persistent registry
- **all** vision: detection, tracking, homography, deskew, calibration
- the world coordinate frame and its conventions
- the configuration it has been given (in memory; disk is the source of
  record — see [use-cases/daemon.md](use-cases/daemon.md) UC-01)
- the annotation store — world-coordinate, layer-keyed, in-memory
  (like the mobile-tag registry; spec §9 #8, UC-28)
- publishing: raw stream, deskewed stream, tag stream

## 4. The adapters

| Adapter | Speaks | Consumer | Surface |
|---|---|---|---|
| CLI | argv, stdout, exit codes | humans | full — every capability |
| MCP server | MCP tools over stdio | AI agents | full perception + configuration |
| ROS node | ROS topics | ROS graph | **streams only** |
| client library | Python | robot programs | full |

### 4.1 CLI

The broadest surface, because a human doing bring-up needs everything:
probe, configure, calibrate, view, query.
[use-cases/cli.md](use-cases/cli.md) covers UC-13 … UC-25.

### 4.2 MCP server

Perception and configuration as tool calls, with handles instead of file
paths (actor A-02's constraint). Starts unbound and resolves a daemon per
call, so it survives daemon restarts
([use-cases/cli.md](use-cases/cli.md) UC-22).

### 4.3 ROS node — streams, deliberately narrow

**The ROS node publishes streams. It is not a query interface.**

It subscribes to the daemon's streams and republishes them as ROS
topics:

- **images** — raw and/or deskewed frames as ROS image messages
- **tags** — tag records as ROS messages, world coordinates, with
  UC-12 mount corrections already applied

That is the scope. Configuration, calibration, probing, and one-shot
queries are **not** exposed through ROS; a ROS user who needs them uses
the CLI or the client library, which are right there.

**Why so narrow.** Streaming is what ROS is genuinely good at and what
a robot in a ROS graph actually wants — a topic it can subscribe to at
frame rate. Everything else would mean reimplementing the CLI's surface
as services, maintaining a second full API against the same daemon, and
inventing ROS-shaped answers for questions ROS has no idiom for
("dump this camera's config as a service call"). The cost is real and
the benefit is small, because the alternative is already available.

This also keeps the node cheap to build and nearly free to maintain: it
is a subscriber and a publisher, with no state of its own.

**Consequences.**
- The node is a **pure client-library consumer** and needs nothing new
  from the daemon — the stream contracts already exist. Per §2, that is
  the sign the boundary is drawn correctly.
- Message types are ROS-native, converted from the client library's
  models at the edge. World coordinates cross the boundary; the ROS
  frame convention question is real and unresolved (see §6).
- A stale calibration must be visible to a ROS subscriber, not silently
  swallowed by the topic
  ([use-cases/daemon.md](use-cases/daemon.md) UC-11.S-3).

### 4.4 Client library

Not an adapter so much as the substrate all of them sit on: discovery,
the typed gRPC wrapper, stream consumers, models, and the frame
renderer. Robot programs use it directly.

## 5. Rules

1. **Adapters hold no authoritative state.** Anything that must survive
   an adapter restart lives in the daemon or on disk.
2. **Adapters do no vision.** OpenCV being importable in the base
   install ([use-cases/cli.md](use-cases/cli.md) UC-21) does not license
   its use outside the daemon; `tags generate` is offline image
   *generation*, not perception.
3. **Adapters do not talk to each other.** The daemon is the only
   rendezvous point.
4. **Every capability is reachable from the Python module**
   ([process.md](process.md)); adapters are shells over it.
5. **A new adapter must not require a new daemon capability.** If it
   seems to, that is a design smell to investigate before writing the
   RPC.

## 6. Open questions

- **ROS frame conventions.** ROS REP-103 specifies +x forward, +y left,
  +z up, right-handed. AprilCam's world frame is A1-centred, +x east,
  +y north, in cm. These are compatible in handedness but differ in
  units and in what the axes mean. Where does the conversion happen, and
  what is the ROS frame named? Given how emphatically
  [specification.md](specification.md) §7 treats geometry conventions as
  sacrosanct, this needs deciding explicitly rather than by accident.
- **ROS 1 or ROS 2?** Affects packaging, message definitions, and the
  discovery story.
- **Does the ROS node republish annotations?** The streams-only stance
  suggests no separate topic — annotations arrive embedded in deskewed
  image frames regardless. But a ROS consumer that wants the structured
  annotation data (not pixels) currently has no topic for it. Record,
  don't decide here.
- **Does the ROS node deserve use cases of its own?** It has no chapter
  in [use-cases/](use-cases/) yet. Its two capabilities are thin, but
  "thin" has been wrong before.
- **Where does `aprilcam view` sit?** [specification.md](specification.md)
  §4.8 makes it a subsystem, but by this document's taxonomy it is an
  adapter translating streams into pixels on a screen for a human. Worth
  reconciling the two framings.
