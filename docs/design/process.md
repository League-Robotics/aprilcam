# AprilCam v2 Development Process

**Status:** pilot · **Owner:** Eric Busboom · **Started:** 2026-07-29

This is a light description of the process we are piloting for AprilCam v2.
It exists so the sequence is explicit and so nobody (human or agent) skips
a step and starts writing code from a vague idea.

---

## Why this process

v1 was built subsystem-first from a requirements list. It works, but the
requirements never said *who wanted a thing and why*, so there was no
grounded way to tell whether a feature was finished, whether a test
covered anything that mattered, or whether a subsystem boundary was
correct. Tests verified that code did what the code did.

v2 inverts that. We describe the demand before the supply.

## The pipeline

Each stage consumes the one above it. Nothing downstream may invent
vocabulary the upstream stage does not contain.

```
Jobs to be done   — what someone is fundamentally trying to accomplish
      ↓
Actors            — who (or what) has those jobs
      ↓
Use cases         — a goal an actor achieves via the system
      ↓
Scenarios         — one concrete path through a use case, with real values
      ↓
Tests             — executable scenarios
      ↓
Subsystems        — the code that makes the scenarios pass
```

### 1. Jobs to be done

The underlying need, stated independently of AprilCam's existence and of
any solution. "Know where my robot is on the field to within a
centimetre" is a job. "Call `GetTags`" is not — that is a mechanism.

### 2. Actors

Who has the jobs. For AprilCam these are not all human: AI agents,
robot programs, and the daemon's own clients are actors. Each actor gets
its capabilities and its constraints recorded, because those constrain
the interface (an AI agent cannot see a GUI; a robot control loop cannot
tolerate a 200 ms stall).

### 3. Use cases

An actor achieving a goal through the system. Written at a level that
survives implementation change — the use case says what is accomplished,
not which RPC carries it. Each is uniquely identified (`UC-nn`) so
everything downstream can cite it.

### 4. Scenarios

A single concrete walk through a use case with actual values: this
camera, this playfield, this tag id, these coordinates, this failure.
Scenarios are where the vagueness dies. Each is identified (`UC-nn.S-m`)
and is either a happy path, an alternate path, or a failure path — we
write failure paths deliberately, because v1's weak spot was undefined
behaviour at the edges.

### 5. Tests

Scenarios are the test plan. A scenario with no test is an untested
requirement; a test citing no scenario is a test we cannot justify.
This is the traceability we did not have in v1.

### 6. Subsystems, built one at a time

Only now does code get written, and one subsystem at a time, each with
its scenarios passing before the next starts.

## Rules we are holding ourselves to

1. **Requirements trace to a use case.** A feature nobody can attach to a
   use case does not get built. This is the main brake on scope.
2. **Both interfaces are first-class.** Every capability is reachable
   from the `aprilcam` CLI *and* from the Python module. The CLI is a
   thin shell over the module — never the other way round, and never a
   CLI-only feature.
3. **Failure paths are specified, not discovered.** Each use case
   enumerates its failure scenarios up front.
4. **The existing specification is downstream.**
   [specification.md](specification.md) and [design.md](design.md)
   already describe v2's subsystems in detail. They are *answers*. The
   use cases are the questions, written to check that the answers are
   the right ones — and to expose answers we do not yet have.
5. **One file per subsystem, one shared index.** Use cases live in
   [use-cases/](use-cases/) — [use-cases/README.md](use-cases/README.md)
   holds the jobs, actors, and conventions that all subsystems share;
   each subsystem gets its own file. Numbering is global across files, so
   `UC-09.S-3` is unambiguous everywhere. (Originally a single document;
   split once one subsystem alone passed 900 lines.)

## Artifacts

| Doc | Role |
|---|---|
| [use-cases/](use-cases/) | JTBD, actors, use cases, scenarios — the demand side. Index plus one file per subsystem. |
| [specification.md](specification.md) | v2 system spec: subsystems, contracts, invariants. |
| [design.md](design.md) | System-level design: subsystem map, global conventions. |
| [adapters.md](adapters.md) | Core-and-adapters shape: why the daemon stays small, what the CLI / MCP / ROS adapters may do. |
| `src/aprilcam/*/DESIGN.md` | Per-subsystem design, co-located with the code. |
| [development-plan.md](development-plan.md) | Sequencing of the build. |

## Status of this pilot

We are at stage 1–4: writing [use-cases/](use-cases/). No v2 code
exists yet; v1 is archived under `archive/` for reference.
