# Design Audit — v2, 2026-07-30

**Status:** for discussion · **Author:** review of the design set against v1 code

An assessment of the v2 design set as it stands, and an inventory of v1
capabilities the design has not yet accounted for. Written to be argued
with, not filed.

---

## 1. State of the design set

| Doc | Lines | Assessment |
|---|---|---|
| [specification.md](specification.md) | 423 | Strong. Subsystems, contracts, invariants, resolved decisions. |
| [development-plan.md](development-plan.md) | 454 | Strong but **stale** — see §2. |
| [design.md](design.md) | 209 | Strong. Principles and subsystem map. |
| [use-cases/](use-cases/) | 1241 | New. 25 use cases, 95 scenarios, 2 of ~6 subsystems. |
| [adapters.md](adapters.md) | 176 | New. Core/adapter shape. |
| [process.md](process.md) | 117 | The pilot process description. |
| [overview.md](overview.md) | 178 | **Obsolete** — v1 requirements, already superseded. |

**What is genuinely good.** The invariants (§7 of the spec) are the best
thing in the set — hard-won geometry conventions written down with the
reasoning, plus a plan (stage 6) to pin them with tests that never get
relaxed. The efficiency-as-requirement stance in the development plan is
unusually concrete: idle daemon does near-zero work, with CPU budget
tests. Both are the kind of thing normally learned twice.

**The main structural problem.** We now have **two planning documents
that do not know about each other**: `development-plan.md` (12 stages,
written before the use-case work) and `use-cases/` (25 use cases,
written after). They disagree — see §2. One of them has to become
subordinate.

---

## 2. Contradictions to resolve

These are places where two documents in the set now say different
things. Each needs a decision, not just an edit.

### 2.1 The development plan predates the use cases

`development-plan.md` Stage 9 says *"daemon restart persists pushed
documents"*. `use-cases/daemon.md` UC-01 says the exact opposite: pushed
config is **discarded** on restart, disk is the source of record. The
use case is the newer decision and the deliberate one.

**Decision needed:** does `development-plan.md` get rewritten against the
use cases, or archived? Its stage structure is still good; its
per-stage details predate five sessions of decisions.

### 2.2 OpenCV in the base install — **spec fixed; plan still stale**

`specification.md` §9 records "taggen keeps its OpenCV dependency,
behind the `[daemon]` extra." `use-cases/cli.md` UC-21 now says OpenCV is
a **base dependency**. The spec has not been updated.

Also affected: development-plan Stage 10 has a "**cv2-free import
test** (fresh venv without OpenCV)" and Stage 11 an exit criterion of "a
full MCP session on a base install (no OpenCV present)." Both tests
become impossible as written. They should become *"no client module
imports cv2"* — testing the discipline rather than the absence.

### 2.3 Calibration persistence — **RESOLVED**

UC-01 flags it as an open question and never answers it: config is
ephemeral, but is *calibration*? It is derived state under `data_dir`,
expensive to recompute, and v1 persisted it (`calibration.json`, with a
`ReloadCalibration` RPC). Almost certainly it persists — but "restart
forgets everything" is a plausible misreading of the config rule and
would be very painful.

**This should be decided explicitly and written into UC-01.**

### 2.4 `aprilcam view` — subsystem or adapter?

`specification.md` §4.8 makes it a subsystem; `adapters.md` implies it
is an adapter. Cosmetic, but the doc set should not contradict itself.

---

## 3. v1 capabilities the design has not considered

Found by reading the archived code. Each is a real thing v1 did that no
v2 document mentions. Not all should survive — but each needs a
deliberate keep/drop, not silence.

### 3.1 Tag history / ring buffer — **RESOLVED: kept (UC-26)**

v1's `camera_pipeline.py` maintains `RingBuffer(maxlen=_RING_BUFFER_SIZE)`
of per-frame tag records, exposed as MCP `get_tag_history`. The original
project overview specified it: *"~300 frames ≈ 10s at 30fps… enables the
agent to compute or verify velocities, trajectories, accelerations."*

UC-10 deliberately made tag queries **current-state only**, and the
history capability vanished without a decision. An agent that wants to
know *where a robot was going* rather than where it is now has no answer.

**Decision needed:** keep history (needs its own use case) or drop it
explicitly.

### 3.2 Camera *streaming* one-shot vs. the `where` query — partially covered

`WhereIs` is covered by UC-08. But v1 also had **`get_objects`** —
non-tag object detection (square/cube detector, LAB-Gaussian and HSV
color classifiers, `vision/objects.py` + `color_classifier.py`, ~640
lines). You said "we're not doing color detection like we were before, so
we can drop the color detection stuff for now," which covers the
classifiers. But `get_objects` as a *capability* — find things that
aren't tags — is dropped with it. Worth confirming that is intended.

### 3.3 Host codes — **RESOLVED: dropped**

`client/host_codes.py` (504 lines) implements bijective base-26 addressing:
local cameras are `A`, `B`, `C`; remote cameras are `FA`, `FB`. Backed by
`hosts.json`, with a stability guarantee across probes.

Our CLI use cases address cameras by their full derived slug
(`arducam-ov9782-usb-camera`) and daemons by hostname. That is more
readable but much longer to type, and the entire host-code scheme is
currently unmentioned. v1 built it for a reason (lab-scale, many
hosts).

**Decision needed:** keep short codes as an addressing convenience, or
drop them as complexity v2 does not need?

### 3.4 Advisory info flags — `frame_dark`, `calibration_stale`

v1 writes `info.json` with advisory flags: `frame_dark` (sustained dark
frames observed) and `calibration_stale`. Deliberately advisory —
"nothing reads `frame_dark` to gate or refuse."

Our UC-05 (confirm readiness) mentions "frame not dark" in passing and
UC-06 mentions a dark-frame flag, but the mechanism — sustained-condition
detection, hysteresis, where it surfaces — is unspecified. This is
exactly the diagnostic that saves an hour of "why is nothing detecting."

### 3.5 Environment/config cascade — `CONFIG_VARS`

`config.py` (683 lines) implements FHS vs XDG directory resolution
(root → `/etc/aprilcam`, user → `~/.config/aprilcam`), a six-level
precedence cascade, and a self-documenting `CONFIG_VARS` table:
`APRILCAM_DATA_DIR`, `APRILCAM_CONFIG_DIR`, `APRILCAM_SOCKET_DIR`,
`APRILCAM_LOG_DIR`, `APRILCAM_DETECTION_FPS`, `APRILCAM_DAEMON_HOST`,
and more.

UC-01 says "a well-known directory such as `/etc/aprilcam`" and stops
there. The FHS/XDG split, the env-var overrides, and the precedence order
are all real behaviour that the use cases do not describe. **The `-d`
flag and `APRILCAM_DAEMON_HOST` interact and nothing says how.**

### 3.6 Packaged guides — **RESOLVED: kept (UC-27)**

v1 ships two markdown guides inside the package, served by `guides.py`
and exposed as MCP `get_robot_api_guide`. This is how an AI agent learns
the system's conventions without the developer pasting them.

Given actor A-02's constraints (no file paths, tool calls only), a
self-describing guide tool is arguably *more* important in v2, not less.
Currently unmentioned.

### 3.7 Daemon self-heal and takeover

`camera_pipeline.py` has a bounded reopen budget: 5 consecutive read
failures trigger reopen, capped at 10 USB reopens. `daemon_cli.py` has a
`--force` takeover path. UC-07 mentions "daemon self-heals" in passing;
the *policy* (how many retries, what happens when the budget is
exhausted, whether the camera is marked dead) is unspecified.

### 3.8 `Shutdown` RPC

v1 has a `Shutdown` RPC — remote daemon shutdown. Our UC-13 explicitly
**refuses** remote lifecycle ("SSH to the host"). That is a defensible
tightening, but it is a capability removal that should be conscious.

### 3.9 Other v1 RPCs unmentioned in v2 docs

| RPC | Status in v2 docs |
|---|---|
| `GetCameraInfo` | not mentioned (distinct from config) |
| `GetCalibration` / `SetCalibration` | UC-04 has "fetch the homography"; `SetCalibration` (push a calibration) is unmentioned |
| `ReloadCalibration` | unmentioned — relevant to §2.3 |
| `EnumerateCameras` vs `ListCameras` | v1 had both; UC-25 assumes probe/list distinction but not this pair |
| `GetPaths`/`SetPaths`/`PublishOverlay` | correctly removed (drawing is client-side) *(2026-07-31 addendum: premise reversed by spec §9 #8 — the daemon now holds annotation state; the v1 file-backed/compositing mechanism stays removed)* |

---

## 4. Gaps in the use-case set itself

Missing chapters, in rough priority order:

1. **Client library / Python module** — the process doc says every
   capability must be reachable from Python and that the CLI is a shell
   over it, yet there is no chapter describing that surface. This is the
   most load-bearing gap: it is the substrate of every adapter.
2. **MCP server tools** — UC-22 covers *launching* the server; the tool
   surface itself is unspecified. v1 had ~30 tools.
3. **Viewer** — UC-18 covers `playfield view` from the CLI; the viewer's
   own behaviour (layers, panels, interaction) is unspecified.
4. **ROS node** — [adapters.md](adapters.md) §4.3 scopes it; no use cases.
5. **Tag parameter registration** (UC-12) has no CLI or MCP surface —
   already flagged in cli.md.
6. **Feature queries** (UC-08) have no CLI command.

---

## 5. Decisions to make

Ordered by how much downstream work they unblock.

| # | Decision | Status |
|---|---|---|
| 1 | Does `development-plan.md` get rewritten, or archived? | **RESOLVED 2026-07-30: rewritten, subordinate.** Use cases say *what*; the plan says *in what order*. Stage 9 config model, the two cv2-free tests, and Stage 1 (archive vs delete) all corrected; a stage→use-case coverage table added. |
| 2 | Does calibration persist across restart? | **RESOLVED 2026-07-30: yes.** The daemon writes `calibration.json` and reloads it. The rule is *file-backed vs in-memory*, not config vs calibration. Written into UC-01. |
| 3 | Keep tag history, or drop it? | **RESOLVED 2026-07-30: keep.** Now UC-26. |
| 4 | Keep host codes (`A`, `FB`) or drop? | **RESOLVED 2026-07-30: drop.** Removed from spec §6 and design.md; no `hosts.json`. |
| 5 | Is there a packaged agent guide? | **RESOLVED 2026-07-30: yes.** Now UC-27. |
| 6 | Config cascade: how do `-d`, `APRILCAM_DAEMON_HOST`, and config files interact? | **RESOLVED 2026-07-30.** Two dirs (`/etc/aprilcam` human-owned read-only, `/var/lib/aprilcam` daemon-owned), 5 env vars, flag > env > file > default. See [configuration.md](configuration.md). |
| 7 | Update spec §9 for the OpenCV reversal, and fix the two now-impossible cv2-free tests. | **RESOLVED 2026-07-30.** Spec §9 updated; both tests became "cv2 absent from `sys.modules` after import". |
| 8 | Write the client-library use cases. | **OPEN** — every adapter sits on it. |

### 6.1 What is still unspecified about configuration

The stakeholder's rule — *"after a reset, configuration is reloaded from
files; dynamically set configuration is not persisted"* — is fully
specified for **configuration documents** (UC-01) and now for
**calibration** (decision 2). What remains unspecified is *where the
files are* and *what overrides what*:

- **FHS vs XDG.** v1's `config.py` resolves `/etc/aprilcam` when running
  as root, `~/.config/aprilcam` otherwise, switchable with
  `APRILCAM_SYSTEM`. UC-01 says only "a well-known directory such as
  `/etc/aprilcam`."
- **Environment variables.** v1 has ~14 `APRILCAM_*` vars in a
  self-documenting `CONFIG_VARS` table — data dir, config dir, socket
  dir, log dir/level, detection fps, daemon host/port, and more. None
  are mentioned in v2 docs.
- **The `-d` interaction.** `APRILCAM_DAEMON_HOST` and the CLI's `-d`
  flag both select a daemon. Precedence is undefined. Users hit this
  immediately.

A simpler cascade than v1's six levels is clearly wanted — but "simpler"
still needs to be written down, because the current answer is "not
specified at all."

## 6. Recommendation

Three things, in order:

1. **Reconcile the two planning documents** (decision 1). Right now a
   programmer following `development-plan.md` would build the wrong
   config model. Suggest: keep its stage structure and testing rigour,
   rewrite the per-stage detail against the use cases, and make it
   explicitly subordinate — use cases say *what*, plan says *in what
   order*.
2. **Settle decisions 2–6.** They are all small individually and all
   affect the daemon's contract, which is what stage 2–8 build.
3. **Write the client-library chapter.** It is the substrate the process
   doc already promised and no document describes.

Everything else can wait until a subsystem is actually being built.
