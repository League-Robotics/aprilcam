# AprilCam Use Cases

**Status:** IN PROGRESS · **Owner:** Eric Busboom
**Process:** [process.md](../process.md)

The demand side of AprilCam v2: jobs to be done, actors, use cases, and
scenarios. Everything the system is built to do traces back to an entry
here.

This directory holds **one file per subsystem**. Jobs, actors, and the
shared conventions live in this index; the use cases themselves live in
the subsystem files, so no single document becomes unreadable.

Downstream docs ([specification.md](../specification.md),
[design.md](../design.md), per-subsystem `DESIGN.md`) describe *how* the
system meets these. When they disagree with these documents, these
documents state the intent.

## Subsystem files

| File | Covers | Use cases |
|---|---|---|
| [daemon.md](daemon.md) | Daemon bring-up, configuration, calibration, the query/stream surface, tag history, packaged instructions, annotations | UC-01 … UC-12, UC-26 … UC-28 |
| [cli.md](cli.md) | The `aprilcam` command line: daemon targeting, cameras, playfield, the probe→dump→edit→push config lifecycle, tag generation, MCP launch | UC-13 … UC-25 |

Planned, not yet written: client library (Python module surface), viewer
internals, MCP tool surface in detail.

**Numbering is global, not per-file.** UC-13 continues after UC-12
regardless of which subsystem file it lands in, so a reference like
UC-09.S-3 is unambiguous across the whole set.

---

## 1. Jobs to be done

> The fundamental needs, stated without reference to AprilCam or any
> solution. See [process.md](../process.md) §1.

| ID | Job | Who has it | Notes |
|---|---|---|---|
| JTBD-01 | Know where my robot is on the playfield, in real-world units, accurately enough to act on | robot program, AI agent | The core job. Drives calibration, world coordinates, tag height projection. |
| JTBD-02 | See what the playfield looks like right now | AI agent, human developer | Single images and continuous video. |
| JTBD-03 | Know where a known fixed feature is, without looking at it | robot program, AI agent | Answered from the surveyed definition, not from vision. |
| JTBD-04 | Track how things are moving on the field over time | robot program, AI agent | Streaming rather than polling; velocity and heading. |
| JTBD-05 | Get a camera and field working from nothing, repeatably | human developer | Bring-up: enumerate, configure, calibrate. |
| JTBD-06 | Trust that reported positions are actually correct | human developer | Calibration acceptance, provenance, staleness. |
| JTBD-07 | Communicate spatial intent — planned paths, goals, zones — to everyone else looking at the same field | robot program, AI agent, human developer | One party draws, every other party sees. Drives the daemon annotation store and client-side rendering. |

## 2. Actors

> Who or what has the jobs, with the capabilities and constraints that
> shape the interface they need.

| ID | Actor | Kind | Capabilities | Constraints |
|---|---|---|---|---|
| A-01 | Robot program | software, in-process client library | Full Python API; holds its own state; knows its own tag id | Control-loop timing — cannot tolerate multi-hundred-ms stalls; usually headless; may be on a different host than the daemon |
| A-02 | AI agent | software, via MCP stdio | Tool calls with structured results; can read images | Cannot see a GUI; no file paths — handles and tools only; each call costs latency and tokens, so chatty polling is a poor fit |
| A-03 | Human developer | person, via CLI and viewer | Shell, editor, eyes on a live view; can physically move the camera and field | Wants one command to do one thing; will not hand-edit state files |
| A-04 | Daemon operator | person or init system | Starts/stops `aprilcamd`, possibly with no configuration at all | May start the daemon before any playfield or camera config exists |

## 3. The worked example

Scenarios cite real hardware and the real field rather than invented
values:

- **Cameras:** `arducam-ov9782-usb-camera`, `hd-usb-camera` — the two in
  the captured fixture matrix.
- **Playfield:** `main-playfield`, 134.3 × 89.3 cm, origin at AprilTag 1
  in the field centre, +x east, +y north. Definition:
  `config/aprilcam-old/playfields/main-playfield.json`.
- **Features:** 1 AprilTag, 10 ArUco (perimeter 1–8, internal 9 at
  (−35, 0) and 10 at (35, 0)), 6 rectangles, 8 dots.

---

## 4. Conventions

### Identifiers

| Form | Meaning |
|---|---|
| `JTBD-nn` | Job to be done |
| `A-nn` | Actor |
| `UC-nn` | Use case (globally numbered across all subsystem files) |
| `UC-nn.S-m` | Scenario within a use case |
| `UC-nn.S-ma` | Variation on a scenario — same shape, one dimension changed |

Identifiers are permanent and never renumbered. A dropped use case is
marked `WITHDRAWN` with a reason, keeping its number — so external
references never rot.

### Use-case template

```markdown
### UC-nn — <goal, phrased as the actor's outcome>

**Job:** JTBD-nn · **Primary actor:** A-nn · **Also serves:** A-nn
**Status:** draft | agreed | built | withdrawn
**Interfaces:** CLI | Python | MCP        (all that must expose this)

**Goal.** What the actor gets, in one sentence.

**Preconditions.** What must already be true.

**Main success path.**
1. …

**Postconditions.** What is true afterwards that was not before.

**Failure modes.** What can go wrong, and what the system does about it
— each becoming a failure scenario below.

**Open questions.** Anything undecided. These are the point of the
exercise; leave them visible rather than guessing.

#### Scenarios

**UC-nn.S-1 — <name>** · _happy path_
Concrete values. This camera, this playfield, this tag id, these
coordinates, this expected result.

**UC-nn.S-2 — <name>** · _failure path_
…
```

### Scenario rules

1. **Concrete values, always.** "A tag near the edge" is not a scenario;
   "tag 7 at (65.0, 41.2) cm, 1.5 cm inside the north boundary" is. Use
   the real playfield and the real cameras where possible — the captured
   fixture matrix under `archive/tests/images/` records actual field
   states we can cite.
2. **One path per scenario.** If it branches, it is two scenarios.
3. **Every use case gets at least one failure scenario.** v1's weakness
   was undefined edge behaviour; specifying failures is how we fix that.
4. **Observable outcomes only.** A scenario asserts what the actor can
   see through a supported interface, never a daemon internal. (The
   internal image pipeline is explicitly invisible — see
   [specification.md](../specification.md) §4.1.)
5. **Mark the interface.** If a scenario exercises the CLI, the Python
   module, and MCP differently, note which — the CLI/Python parity rule
   in [process.md](../process.md) means most capabilities need both
   covered.
6. **Use variations for one-dimension changes.** When a scenario differs
   from another only in one respect (remote instead of local, three
   pushes instead of one), write it as a variation (`S-4a`, `S-4b`)
   rather than a full scenario. Keeps the set readable.
