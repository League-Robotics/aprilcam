---
id: '040'
title: Run the v2 daemon as a systemd service on a Raspberry Pi
status: executing
branch: sprint/040-run-the-v2-daemon-as-a-systemd-service-on-a-raspberry-pi
use-cases: []
issues:
- plan-daemon-runs-on-a-remote-raspberry-pi-every-client-discovers-it-and-connects-over-tcp.md
- live-tag-detection-and-systemd-persistence-on-a-pi-with-a-calibrated-playfield.md
- camera-bridge-watchdog-never-detects-a-stalled-loopback.md
- remote-daemon-by-mdns-hostname-times-out-ipv6-link-local-vs-ipv4-only-bind.md
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 040: Run the v2 daemon as a systemd service on a Raspberry Pi

## Goals

Turn the v2 daemon from "runs on a developer's Mac" into "runs unattended,
forever, on a headless Raspberry Pi with CSI cameras" — ship the deploy
assets v2 has never had, make the Pi capture path (libcamera enumeration →
v4l2loopback → `V4l2LoopbackBackend`) actually work against real hardware,
and prove the whole thing end to end on `vidar.local`: install, start,
detect tags, survive a reboot, get rediscovered over mDNS.

## Problem

v2 rebuilt the daemon, its FHS directory resolution, TCP+mDNS discovery
(sprint 028), and a libcamera enumeration/backend-selection *branch point*
(sprint 020/031) — but never exercised any of it against a Raspberry Pi.
Two issues carry this forward from v1-era planning and a partial sprint 014
bring-up:

- `plan-daemon-runs-on-a-remote-raspberry-pi-...md` was written against v1
  file paths (`grpc_server.py`, `mcp_server.py`) that no longer exist in v2.
  Its Workstreams 1-4 (sole-camera-owner enforcement, client-side mDNS
  discovery, TCP streaming, gRPC file-proxy RPCs) are **already delivered**
  in v2 as shipped: the MCP server and every CLI camera command are thin
  gRPC clients (per `docs/wiki/daemon.md` and the project overview's Vision
  Boundary), `client/discovery.py` browses `_aprilcam._tcp` (sprint 028),
  and the daemon binds TCP `0.0.0.0:5280` + advertises mDNS unconditionally
  in `daemon/__main__.py`/`daemon/mdns.py`. Only its **Workstream 5** (Pi
  deployment: wheel build, pipx install, systemd unit, provisioning) and
  its "deployment risks to verify first" remain undone.
- `live-tag-detection-and-systemd-persistence-...md` is the direct
  follow-up: sprint 014 verified TCP+mDNS discovery from the Mac against a
  daemon running by hand (`setsid python -m aprilcam.daemon`) on
  `vidar.local`, but deliberately deferred live tag detection against a
  real playfield and systemd persistence (install unit, `enable --now`,
  reboot, confirm auto-restart + rediscovery) because the physical setup
  wasn't available at the time.

Separately, nothing in v2 has ever read a real frame through
`V4l2LoopbackBackend` or matched a real `/sys` card label through
`camera/backends/libcamera.py`'s enumeration — both were converted from v1
under an explicit "no Pi hardware to validate against" deferral
(`camera/backends/__init__.py`'s module table; `docs/design/design.md` §9
item 2). `vidar.local` now has exactly that hardware, already wired the way
v1's working bring-up wired it (`v4l2loopback video_nr=70,71
card_label=aprilcam-imx296-88000,aprilcam-imx296-80000 max_buffers=16`,
matching `archive/docs/knowledge/v4l2loopback-vanishes-after-kernel-upgrade.md`'s
"reference config that must line up" verbatim) — but no v2 code has ever
been pointed at it.

## Solution

1. Ship `deploy/` — a systemd unit for the daemon, a libcamera→v4l2loopback
   bridge (script + templated unit) carrying forward v1's stagger/queue/
   watchdog lessons, the v4l2loopback module config, an idempotent
   provision script, and a runbook — sized to v2's actual (much simpler)
   process model: `python -m aprilcam.daemon` with **no flags**, FHS roots
   resolved automatically by `os.access` against whatever
   `ConfigurationDirectory=`/`StateDirectory=`/`RuntimeDirectory=` systemd
   hands the service user (no `APRILCAM_SYSTEM=1` gate — v2 doesn't check
   `euid`, it checks accessibility, so this "just works" for a non-root
   service user with no special env var).
2. Validate and fix the Pi capture path — `list_loopback_cameras()`
   enumeration, `resolve_capture_backend`'s libcamera branch,
   `V4l2LoopbackBackend`'s YUYV read path — against vidar's two real
   `/dev/video70`/`/dev/video71` loopbacks, in v2 style (real base classes,
   never `typing.Protocol`).
3. Verify live on `vidar.local`: stop the ROS camera stack that currently
   owns the CSI sensors, deploy a pinned wheel build via pipx, confirm
   camera list/frame capture/tag detection/mDNS discovery/clean
   `journalctl` output from the Mac, reboot and confirm auto-start +
   rediscovery, then retire the disabled v1 units so exactly one aprilcam
   stack exists on the box.
4. Document what this sprint learns — a ported and updated Pi camera setup
   knowledge doc (including the ROS-coexistence procedure), the
   `docs/wiki/daemon.md` systemd section, and the `design.md`/
   `camera-DESIGN.md` known-gap entries this sprint closes.

## Success Criteria

- `deploy/aprilcamd.service`, `deploy/aprilcam-bridge@.service`,
  `deploy/aprilcam-camera-bridge`, `deploy/v4l2loopback-aprilcam.conf`
  (or equivalent modprobe/modules-load files), `deploy/provision-pi.sh`,
  and `deploy/README.md` exist, are internally consistent with each other,
  and reference only entry points/flags that exist in v2.
- `aprilcam camera list -d vidar.local` shows both imx296 cameras usable;
  `aprilcam camera image -d vidar.local` returns real frames for both.
- Tag detection is honestly reported: if a calibrated playfield/tags are in
  view, `aprilcam tags`/`get_tags` return real detections; if not, the
  ticket says so plainly rather than claiming untested success.
- `aprilcam daemon list` / mDNS browsing from the Mac discovers the
  `vidar.local` daemon by name.
- `journalctl -u aprilcamd` on vidar shows a clean start, the TCP 5280
  bind, and mDNS advertising, with no traceback.
- After `systemctl reboot`, the daemon and both bridge units auto-start and
  the daemon is rediscovered from the Mac without manual intervention.
- The v1 `aprilcam-bridge@.service`/`aprilcamd.service` units on vidar are
  removed or fully superseded (not left running alongside the v2 ones).
- ROS camera services on vidar are restored to their original
  enabled/running state after verification, unless the stakeholder decides
  otherwise (see Open Questions).
- Offline test gate (`scripts/test-gate.sh`) passes; camera-path code
  changes have unit coverage that does not depend on real hardware being
  present, and any hardware-only verification is recorded in the ticket,
  not faked as a passing offline test.

## Scope

### In Scope

- New `deploy/` directory: daemon unit, bridge script + templated unit,
  v4l2loopback module config, provision script, README runbook.
- Fixes to `camera/backends/libcamera.py`, `camera/backends/v4l2_loopback.py`,
  `camera/backends/__init__.py` (`resolve_capture_backend`), and
  `camera/identity.py` (`_probe_libcamera`) as needed to make the real
  capture path work — no interface or call-shape changes beyond what real
  hardware demands.
- `pyproject.toml` `daemon` extra audit for headless-Linux import safety
  (the `mss`/X11 and `opencv-contrib-python` aarch64/cp312 concerns named
  in the issues) — a fix or a documented "already safe, verified" finding.
- Live verification and remediation on `vidar.local` over SSH: stopping
  ROS camera services for the test window, building and shipping a pinned
  wheel, installing via pipx, installing/enabling systemd units, live tag
  detection if a playfield is available, reboot persistence, retiring v1
  units.
- Documentation: a new `docs/knowledge/raspberry-pi-camera-setup.md`
  (ported from `archive/docs/knowledge/` and updated with this sprint's
  findings, including ROS coexistence), `docs/wiki/daemon.md` systemd
  section, `docs/design/design.md` §9 item 2 and `camera/DESIGN.md`'s
  matching Open Questions entries.
- Marking both linked issues resolved, with an explicit note of which
  workstreams v2 already covered before this sprint started.

### Out of Scope

- Any change to the sole-camera-owner enforcement, client-side discovery,
  TCP streaming, or gRPC file-proxy RPC work `plan-daemon-runs-on-a-
  remote-raspberry-pi-...md` originally scoped as Workstreams 1-4 — already
  shipped, verified again only incidentally by this sprint's live bring-up.
- A real libcamera **capture** backend using `libcamerasrc`/`cam` directly
  from within the daemon process — the architecture stays "daemon reads a
  v4l2loopback device," matching v1's proven approach and this sprint's
  hardware (the bridge is a separate out-of-process reader, never a daemon
  dependency on GStreamer/libcamera).
- TLS/authentication on the TCP control plane (unchanged, documented
  LAN-trust assumption from sprint 028).
- Permanent, long-term coexistence tooling between AprilCam and the ROS
  camera stack on shared hardware — this sprint proves the daemon works on
  vidar and documents the coexistence *procedure*, but does not build
  arbitration/handoff automation. See Open Questions.
- `vali.local` or any other Pi — this sprint's live verification target is
  `vidar.local` only.

## Test Strategy

Two tiers, kept deliberately separate per the project's Vision Boundary and
`.claude/rules/source-code.md`'s scoped-per-ticket testing rule:

- **Offline (Mac, part of `scripts/test-gate.sh`, `-m "not camera and not
  integration"`)**: unit tests for any `camera/backends/*` fix, using
  faked `/sys` trees and faked subprocess output exactly as the existing
  `list_loopback_cameras`/`V4l2LoopbackBackend` tests already do — no new
  hardware dependency is added to the offline suite. Shell scripts get
  `bash -n` (and `shellcheck` if available) rather than a pytest wrapper.
  A small parser test (e.g. "does the unit file reference a real module
  path") may live in the test tree if it adds value; deploy assets are
  otherwise validated by inspection, not a testing framework built for
  this sprint alone.
- **Live (vidar.local, one ticket, done by a programmer over SSH)**: the
  actual hardware verification named in Success Criteria — camera list,
  frame capture, tag detection (if a playfield is in view), mDNS discovery,
  `journalctl` inspection, and a real `systemctl reboot`. Results are
  recorded in that ticket's file, including honest "not verified: no
  playfield in view" statements where applicable — never inferred or
  assumed from offline behavior.

## Architecture

**Substantial** — this sprint adds a new deployment subsystem (`deploy/`:
daemon unit, bridge script + template unit, module config, provision
script) that did not exist in v2 before, introduces a new external
integration (a Raspberry Pi host reached over SSH, running a
libcamera→v4l2loopback bridge process the daemon has no code relationship
to but an operational dependency on), and touches 3+ existing modules
(`camera/backends/libcamera.py`, `camera/backends/v4l2_loopback.py`,
`camera/backends/__init__.py`) to validate them against real hardware. No
data-model change and no change to `interfaces.py` contracts is expected —
the work is: ship missing deploy assets, and make already-designed
capture-selection code actually work.

### Step 1 — Understand the Problem

v2 already has every piece a Pi deployment needs *except the deployment
itself*: FHS directory resolution that "just works" for a non-root service
user (`config/directories.py`, cascade `flag > env > default > fallback`,
each tier decided by `os.access`, not `euid`), a daemon entry point with no
required flags (`python -m aprilcam.daemon`), TCP+mDNS binding that is
already unconditional and non-fatal-on-failure (`daemon/__main__.py`
`build()`, `daemon/mdns.py`), and a libcamera enumeration/backend-selection
branch point that has been built but never run against a real `/sys` tree
or a real v4l2loopback device (`camera/backends/libcamera.py`,
`camera/backends/__init__.py::resolve_capture_backend`,
`camera/backends/v4l2_loopback.py`). What is missing is entirely
*operational*: the unit files, the bridge process, the provisioning steps,
and the confirmation that the untested code paths behave against real
hardware. This is why the sprint is "run the daemon as a systemd service,"
not "build daemon deployment support" — the support already exists.

### Step 2 — Identify Responsibilities

Four responsibilities, each changing for a different reason:

1. **Process supervision** (systemd unit for `aprilcamd`) — changes when
   the daemon's process model changes (env vars it reads, FHS roots it
   needs, restart policy).
2. **Camera bridging** (libcamera → v4l2loopback bridge script + per-camera
   template unit) — changes when the CSI camera bring-up procedure changes
   (pipeline parameters, stagger timing, watchdog thresholds) — entirely
   independent of the daemon's own process model; the daemon does not know
   or care that a bridge exists, it only sees `/dev/video70`/`/dev/video71`.
3. **Host provisioning** (module config, packages, groups, wheel install) —
   changes when what a fresh Pi needs before either of the above can run
   changes (new apt package, new udev rule).
4. **Capture-path correctness** (the three `camera/backends/*` fixes) —
   changes when real hardware reveals the enumeration/backend-selection/
   frame-read code was wrong in a way no faked-`/sys`-tree unit test could
   catch.

These four are kept as separate deploy artifacts / separate code changes
rather than folded together, because each has already shown (in v1) that
it changes on its own schedule — the "reference config that must line up"
knowledge doc exists precisely because the module config, the bridge, and
the daemon drifted independently before.

### Step 3 — Define Subsystems and Modules

- **`deploy/aprilcamd.service`** — Purpose: supervise the `aprilcamd`
  process under systemd. Boundary: owns nothing about cameras; only knows
  the daemon's entry point, its service user, and the FHS directories
  systemd must provision for it. Serves SUC-002, SUC-004.
- **`deploy/aprilcam-camera-bridge` + `deploy/aprilcam-bridge@.service`** —
  Purpose: keep one CSI camera's frames flowing into its labelled
  v4l2loopback device. Boundary: owns the `libcamerasrc`→`v4l2sink`
  pipeline, the startup stagger, and the stall watchdog; knows nothing
  about the daemon, gRPC, or AprilCam config — its only contract with the
  rest of the system is "this `/dev/videoN` produces YUY2 frames." Serves
  SUC-001, SUC-004.
  Runs as a `User=` with `SupplementaryGroups=video` (needs real GStreamer
  device access outside any systemd sandboxing the daemon unit itself
  might later gain) — deliberately a static system user rather than
  `DynamicUser=`, matching v1's proven configuration exactly, since the
  bridge process's only job is talking to `/dev/video*` and there is
  nothing to isolate it from.
- **`deploy/v4l2loopback-aprilcam.conf` (modprobe.d) + modules-load.d
  entry** — Purpose: declare the two loopback devices and their labels at
  boot, before any bridge or daemon process starts. Boundary: pure kernel
  module configuration, no process, no code. Serves SUC-001 (a
  prerequisite, not a use case actor performs, but load-bearing for it).
- **`deploy/provision-pi.sh`** — Purpose: bring a fresh Pi to the point
  where the three artifacts above can be installed. Boundary: apt
  packages, `video` group membership, directory scaffolding for the wheel
  cache; idempotent (safe to re-run), never installs or starts the units
  themselves (that stays a separate, explicit step per the runbook, so a
  re-provision never silently bounces a running daemon). Serves SUC-003.
- **`deploy/README.md`** — Purpose: the one ordered runbook a human or a
  future programmer follows to go from "fresh Pi" to "daemon running and
  verified." Boundary: documentation only. Serves SUC-003.
- **`camera/backends/libcamera.py` (fix, not rewrite)** — unchanged
  purpose ("enumerate labelled v4l2loopback devices via `/sys`"); this
  sprint validates its existing one-sentence purpose against a real
  `/sys/class/video4linux` tree on vidar and fixes whatever the real card
  labels (`aprilcam-imx296-88000`, `aprilcam-imx296-80000`) reveal that a
  faked tree didn't. Serves SUC-005.
- **`camera/backends/v4l2_loopback.py` (fix, not rewrite)** — unchanged
  purpose ("read a v4l2loopback device via `v4l2-ctl`"); validated against
  real YUY2 frames from vidar's bridge for the first time. Serves SUC-005.
- **`camera/backends/__init__.py::resolve_capture_backend` (fix, not
  rewrite)** — unchanged purpose (the libcamera-vs-everything-else branch
  point); validated end to end for the first time against a real
  `hardware_key`/`device_index` pair instead of an injected fake list.
  Serves SUC-005.

No new module is introduced inside `src/aprilcam/` — every code-side
change is a targeted fix to an existing module whose purpose does not
change, only whose correctness against real hardware is now provable. The
new subsystem is entirely `deploy/`, which sits outside the package
(operational assets, not importable code) and depends on nothing inside
`src/aprilcam/` except by invoking its console-script entry points.

### Step 4 — Diagrams

**Component diagram** — required (3+ modules touched, one of them a new
subsystem with a new external dependency: SSH-reachable Pi hardware).

```mermaid
graph TB
    subgraph Mac["Mac (dev workstation)"]
        CLI["aprilcam CLI\n(camera list/image, tags)"]
        MCP["MCP server"]
    end

    subgraph Pi["vidar.local (Raspberry Pi 5, Ubuntu 24.04)"]
        subgraph Systemd["systemd"]
            Daemon["aprilcamd.service\npython -m aprilcam.daemon"]
            BridgeA["aprilcam-bridge@70.service"]
            BridgeB["aprilcam-bridge@71.service"]
        end
        LoopA["/dev/video70\n(v4l2loopback, aprilcam-imx296-88000)"]
        LoopB["/dev/video71\n(v4l2loopback, aprilcam-imx296-80000)"]
        SensorA["imx296 CSI camera (i2c@88000)"]
        SensorB["imx296 CSI camera (i2c@80000)"]
        ModCfg["/etc/modprobe.d/aprilcam.conf\n/etc/modules-load.d/aprilcam.conf"]
    end

    CLI -- "TCP 5280 (gRPC)" --> Daemon
    MCP -- "TCP 5280 (gRPC)" --> Daemon
    Daemon -- "mDNS advertise\n_aprilcam._tcp" -.-> CLI
    Daemon -- "V4l2LoopbackBackend\n(v4l2-ctl read)" --> LoopA
    Daemon -- "V4l2LoopbackBackend\n(v4l2-ctl read)" --> LoopB
    BridgeA -- "libcamerasrc | v4l2sink" --> LoopA
    BridgeB -- "libcamerasrc | v4l2sink" --> LoopB
    BridgeA -- reads --> SensorA
    BridgeB -- reads --> SensorB
    ModCfg -. "declares at boot" .-> LoopA
    ModCfg -. "declares at boot" .-> LoopB
```

No ERD (no data-model change) and no dependency-direction graph beyond
what the component diagram already shows (the daemon depends on the
loopback devices existing; it has zero code or process dependency on the
bridge units — they communicate only through the kernel device node, which
is the intentional decoupling v1 already proved out and this sprint keeps).

### Step 5 — Complete the Document

**What Changed**

- New `deploy/` directory (five artifacts, Step 3 above) — nothing here
  existed in v2 before this sprint (`docs/wiki/daemon.md`: "the repo ships
  no unit files yet").
- Targeted fixes inside `camera/backends/{libcamera,v4l2_loopback,
  __init__}.py` — exact scope determined by what real hardware reveals in
  ticket 004 below; no interface changes are anticipated, but the ticket
  is scoped to allow them if the real `/sys` tree or real YUY2 stream
  disagrees with what the faked-hardware unit tests assumed.
- `docs/knowledge/raspberry-pi-camera-setup.md` (new, ported from
  `archive/docs/knowledge/`), `docs/wiki/daemon.md` systemd section
  (replaced), `docs/design/design.md` §9 item 2 and
  `camera/DESIGN.md`'s matching Open Questions entry (both marked
  resolved, per the project's existing convention for closed gaps — see
  design.md §9 item 1's "Resolved, sprint 036" precedent).
- Both linked issues moved to resolved, with a note (in each) of exactly
  which workstreams v2 already delivered before this sprint.

**Why**

The daemon, discovery, and streaming architecture (sprints 019/020/028)
was always designed for a headless Pi deployment — `Directories`'
`os.access`-based cascade, the unconditional TCP+mDNS bind, and the
libcamera branch point all exist *because* a future Pi deployment was
anticipated. This sprint is where that anticipation gets cashed in: no
architectural change is needed, only the deploy assets and hardware
verification nothing has forced anyone to produce until real Pi hardware
was available.

**Impact on Existing Components**

- `camera/backends/*`: corrected, not redesigned — see Migration Concerns.
- `daemon/__main__.py`, `config/directories.py`, `daemon/mdns.py`: none
  expected. Their behavior (unconditional TCP+mDNS bind, `os.access`-based
  directory cascade) is exactly what this sprint depends on already being
  correct; the live bring-up is this sprint's proof of that, not a reason
  to change them.
- `pyproject.toml`: possible addition of an explicit Linux/headless-safe
  note or a piwheels fallback instruction in the deploy README if the
  opencv-contrib-python aarch64/cp312 wheel resolution needs it — audited
  in ticket 001, not assumed.

**Design Rationale**

- **Decision: dedicated system user (`aprilcam`), not `ros`/`jtl`/an
  existing login account, for the daemon and bridge units.**
  *Context*: v1's units ran as whichever human login happened to own the
  checkout (`eric` or `jtl`), and vidar already has three human/service
  accounts (`jtl`, `ros`, `ansible`) plus the Ansible-managed `ros`
  camera stack.
  *Alternatives considered*: (a) run as the existing `ros` user, since it
  already has camera group membership and is the account other camera
  services use; (b) run as `jtl`, matching the disabled v1 units already
  on the box; (c) a dedicated `aprilcam` system user.
  *Why this choice*: (a) is rejected because AprilCam is not
  Ansible-managed and must not become a hidden dependency of the `ros`
  account's ownership — mixing an unmanaged service into a managed
  account's identity makes both harder to reason about and risks a future
  Ansible run clobbering something AprilCam-specific. (b) is rejected
  because `jtl` is a human login for a *different*, now-superseded v1
  install (`/home/jtl/aprilcam`) that this sprint retires — keeping the
  new service under that identity would perpetuate the exact conflation
  this sprint is meant to clean up. (c) wins: `useradd --system --no-create-home
  --shell /usr/sbin/nologin aprilcam`, added to the `video` group, gives
  the daemon and bridge units an identity that exists for no reason other
  than running AprilCam, is trivially provisioned by `provision-pi.sh`,
  and cannot be confused with a human's login session or another
  project's automation.
  *Consequences*: the provision script must create this user and its
  group membership (new, v1 never did this); the systemd units'
  `ConfigurationDirectory=`/`StateDirectory=`/`RuntimeDirectory=`
  directives provision `/etc/aprilcam`, `/var/lib/aprilcam`, `/run/aprilcam`
  owned by this user automatically, so — unlike v1 — **no
  `APRILCAM_SYSTEM=1` or any other env-var gate is needed**: v2's
  `Directories` cascade decides "is this root usable" via `os.access`, not
  `euid == 0`, so a non-root `aprilcam` user with systemd-provisioned,
  self-owned directories resolves the FHS roots automatically, the same
  way root would.

- **Decision: the daemon reads a v4l2loopback device; it does not link
  libcamera/GStreamer in-process.**
  *Context*: v1 tried running `libcamerasrc` and the daemon in the same
  process and hit repeated PiSP/libcamera segfaults under load
  (`archive/scripts/setup-vidar-cameras.sh`'s "no GStreamer/libcamera runs
  inside the daemon (it segfaults there)").
  *Alternatives considered*: (a) an in-process libcamera capture backend;
  (b) the out-of-process bridge, unchanged from v1.
  *Why this choice*: (b) — v1 already paid for this lesson in crash
  reports; nothing about v2's daemon process model changes the underlying
  libcamera/PiSP stability characteristics, so re-litigating (a) would be
  re-discovering the same segfaults for no architectural benefit. This is
  also why `camera/backends/__init__.py`'s "not ported this ticket" list
  explicitly excluded "an actual libcamera capture backend" — the decision
  predates this sprint and is reaffirmed, not revisited, here.
  *Consequences*: the bridge is a wholly separate process/unit family from
  the daemon, coupled only through the kernel device node — exactly the
  decoupling the component diagram shows.

- **Decision: pipx + a locally-built wheel, not a venv or a Pi-side
  `pip install -e`.**
  *Context*: the Pi has pipx but not `uv`; the project's install docs
  (`docs/wiki/*`) already standardize on `pipx install 'aprilcam[daemon]'`
  for daemon hosts.
  *Alternatives considered*: (a) `git clone` + a raw venv on the Pi
  (v1's `setup-vidar-cameras.sh` did this); (b) pipx installing a wheel
  built on the Mac and copied over.
  *Why this choice*: (b) matches how every other daemon host is documented
  to install AprilCam (`docs/wiki/daemon.md`'s `pipx install` instruction)
  — a bespoke venv-from-source install on just this one Pi would be a
  second, undocumented install method to maintain. Building on the Mac
  (which has `uv`) and shipping a pinned wheel avoids needing `uv` on the
  Pi at all, and pins exactly what version is running (`pipx install
  "aprilcam[daemon]==<version>"`) rather than tracking a mutable checkout.
  *Consequences*: the provision/runbook steps must build the wheel on the
  Mac and `scp` it over — one more manual step than "clone on the Pi,"
  but consistent with the project's documented install tiers and avoids
  installing build tooling on a resource-constrained Pi.

### Migration Concerns

- **v1 remnants on vidar** (`/etc/systemd/system/aprilcam-bridge@.service`,
  `/etc/systemd/system/aprilcamd.service`, both currently disabled and
  inactive, plus the `/home/jtl/aprilcam` checkout) must be disabled and
  removed (units) once the v2 units are verified working — the sprint does
  not leave both installed side by side. The `jtl` checkout itself is left
  in place (not this sprint's to delete a human's home directory contents)
  but is documented as superseded.
- **ROS camera stack coexistence.** `ros-cameras.service` and its bound
  units (`camera-color-relay.service`, `web-video-server.service`) plus
  `camera-watchdog.timer` currently own the CSI cameras and are
  Ansible-managed. This sprint's live-verification ticket must stop (not
  disable-permanently, unless directed) `ros-cameras.service` and its
  bound units, and stop `camera-watchdog.timer`, for the duration of
  AprilCam bring-up and the reboot-persistence check (which additionally
  requires disabling them at boot so they don't race AprilCam for the
  cameras across the reboot). Nothing in this sprint edits the
  Ansible-managed unit files themselves — only their enabled/active state,
  which is recorded exactly (before/after) in the verification ticket so
  it can be restored. See Open Questions for whether ROS camera duty on
  vidar is restored after this sprint or left to AprilCam permanently —
  that decision belongs to the stakeholder, not this plan.
- **No data migration** — no persisted schema changes.
- **Deployment sequencing**: module config → reboot-or-modprobe → bridge
  units → daemon unit, in that order (matches v1's proven `setup-vidar-
  cameras.sh` sequencing, Step 3 above); installing the daemon unit before
  the loopback devices exist would just mean the daemon starts with two
  absent cameras, which it does not fail on (UC-01 "empty is success"),
  but the acceptance criteria require both cameras usable, so the runbook
  states the order explicitly rather than relying on that graceful
  degradation to mask a misordered install.

## Revision

Ticket 003's live session on vidar surfaced two real defects outside its
own file scope (`camera/backends/*.py`, `camera/identity.py`), each now
covered by a new ticket (006, 007) and a linked issue:

1. **`deploy/aprilcam-camera-bridge`'s watchdog never detects a stalled
   loopback** (Finding 4) — it judges a stall on `v4l2-ctl
   --stream-mmap`'s exit status, which is 0 even when the loopback has no
   producer, so a `libcamerasrc` SIGSEGV is never noticed and the unit
   never restarts. No architectural change: this is a bug fix confined to
   the `deploy/aprilcam-camera-bridge` artifact already introduced by
   ticket 001 (Step 3's "Camera bridging" responsibility, unchanged
   purpose) — ticket 006.
2. **`-d vidar.local` times out from the Mac** (Finding 7) — the Mac's
   resolver returns vidar's IPv6 link-local addresses before its IPv4
   address, and `client/discovery.py` hands the raw hostname to
   `grpc.insecure_channel`, which picks the unreachable IPv6 candidate
   and waits out the full connect timeout. **Direction: fix client-side
   address resolution only** (`client/discovery.py` resolves and prefers
   an IPv4 candidate before falling back), leaving `daemon/rpc.py`'s
   `0.0.0.0`-only bind exactly as the 028-003 stakeholder decision
   recorded it. A dual-stack daemon bind was considered and rejected for
   this ticket: it would reopen a decision this sprint has no new
   information to relitigate, when the reported symptom is fully
   addressed by a narrower, lower-risk change to one already-thin client
   module — ticket 007.

Neither finding changes this sprint's subsystem boundaries, the
component diagram, or any Design Rationale entry above; both are
targeted fixes to code the sprint's plan already anticipated might need
correction once real hardware and a real network were involved. Ticket
004's `depends-on` and acceptance criteria are updated accordingly (both
new tickets must complete first), and its acceptance criteria are further
revised for a hardware finding unrelated to either defect above: camera
i2c@88000 has a physical CSI fault (ticket 003 Finding 6, reproduced four
times, pre-existing under ROS before this sprint touched vidar) — ticket
004 now requires only camera i2c@80000 end to end, with i2c@88000
required to be enumerated and reported as unusable without crashing the
daemon or affecting the working camera.

### Open Questions

1. **RESOLVED (ticket 040-005): AprilCam, not ROS, owns vidar's cameras
   going forward.** The stakeholder decided vidar keeps running the v2
   AprilCam daemon and bridge units permanently, superseding this
   question's "conservative default" of restoring ROS at the end of
   ticket 004's live-verification session (ticket 004's own recorded
   "End state on vidar.local" — ROS re-enabled+active, AprilCam
   disabled+inactive — reflected that now-superseded default, not this
   decision; it is not rewritten, only annotated, per this project's
   "extend, don't delete" convention for closed history). The team-lead
   executes the actual switch on vidar directly (out of this ticket's own
   scope, which is documentation-only):
   ```bash
   systemctl disable --now camera-watchdog.timer web-video-server \
       camera-color-relay ros-cameras
   systemctl enable --now aprilcam-bridge@0 aprilcam-bridge@1 aprilcamd
   ```
   — the same four-unit-disable / three-unit-enable sequence ticket 004's
   Findings item 9 established as correct (disabling only two of the four
   ROS units is insufficient to keep ROS down across a reboot, because
   `camera-color-relay.service`/`web-video-server.service`'s `BindsTo=`
   pulls `ros-cameras.service` back up at boot otherwise).

   **Risk carried forward, not solved by this decision**: `ros-cameras`
   and its bound units are Ansible-managed, so a future Ansible run
   against vidar may re-enable and restart them, since `systemctl
   enable`/`disable` state is exactly the kind of drift an Ansible
   playbook is likely to reconcile back to its declared state. This
   project's switch-over never touches the underlying Ansible-managed
   unit files, only their enabled/active state, so Ansible has no
   *declared* conflict to detect — but a playbook that unconditionally
   re-asserts "these ROS units are enabled+active" would still silently
   undo the switch. **Recovery**: re-run the AprilCam-enable half of the
   sequence above — it is idempotent and does not depend on ROS's prior
   state. If Ansible keeps re-fighting the switch on every run, the
   durable fix is on the Ansible side (exclude vidar's camera units from
   that playbook's scope, or make the camera-owner choice itself an
   Ansible-managed variable) — out of this sprint's ability to fix from
   `deploy/`, and not attempted here. See
   `docs/knowledge/raspberry-pi-camera-setup.md`'s "ROS coexistence and
   switch-over" section for the full procedure and this same risk,
   written for future operators.
2. **Is a single shared imx296 sensor ever needed by both ROS and AprilCam
   concurrently?** Out of scope for this sprint (Scope: "no arbitration/
   handoff automation"), but worth stakeholder awareness: a CSI sensor can
   only be opened by one libcamera application at a time, so any future
   requirement for simultaneous ROS+AprilCam access to the same camera
   would need real arbitration, not just careful sequencing.
3. **Wheel build reproducibility**: this sprint assumes the Mac's `uv
   build --wheel` produces a pure-enough wheel that pip on aarch64/cp312
   resolves all of `daemon`'s compiled dependencies (`opencv-contrib-
   python`, `grpcio`, `numpy`) from PyPI without a piwheels fallback.
   Ticket 001 verifies this directly; if it doesn't hold, the provision
   script/runbook gains a documented piwheels `--extra-index-url`, per
   v1's precedent — not a stakeholder decision, but flagged here since it
   was named as a risk in both source issues.

## Use Cases

### SUC-001: A CSI camera's frames reach the daemon through a bridge process
Parent: UC-01 (daemon startup / camera enumeration)

- **Actor**: The `aprilcam-camera-bridge` script, running under
  `aprilcam-bridge@<position>.service`.
- **Preconditions**: v4l2loopback is loaded with the project's labelled
  devices (`/dev/video70`/`71`, card labels `aprilcam-imx296-88000`/
  `-80000`); the corresponding imx296 CSI sensor is not held open by
  another process (e.g. ROS).
- **Main Flow**:
  1. The unit starts, staggered by camera position (4s × position) to
     avoid two `libcamerasrc` instances racing at PiSP init.
  2. The script resolves the libcamera camera id for its position from
     `cam -l` and launches `libcamerasrc ! queue ! videoconvert !
     v4l2sink` into its assigned loopback device.
  3. A watchdog polls the loopback device every ~10s; if a read stalls
     (deadlocked/crashed `libcamerasrc`), it kills the pipeline and exits
     non-zero so `Restart=always` relaunches a fresh one.
- **Postconditions**: the loopback device continuously carries live YUY2
  frames for as long as the sensor is available and the unit is enabled.
- **Acceptance Criteria**:
  - [ ] Both bridge units reach `active (running)` on vidar with the ROS
        camera stack stopped.
  - [ ] `v4l2-ctl -d /dev/video70 --get-fmt-video` (and `/dev/video71`)
        report 1280x720 YUY2 with frames advancing.
  - [ ] Killing the `libcamerasrc` process under a bridge unit causes the
        watchdog to detect the stall and the unit to restart within
        `RestartSec`.

### SUC-002: The daemon starts unattended under systemd on boot
Parent: UC-01 (daemon startup)

- **Actor**: systemd, on behalf of the `aprilcam` service user.
- **Preconditions**: `aprilcam[daemon]` is installed via pipx for the
  `aprilcam` system user; `aprilcamd.service` is enabled.
- **Main Flow**:
  1. systemd provisions `/etc/aprilcam`, `/var/lib/aprilcam`,
     `/run/aprilcam` (via `ConfigurationDirectory=`/`StateDirectory=`/
     `RuntimeDirectory=`), owned by the `aprilcam` user.
  2. systemd runs `ExecStart=.../python -m aprilcam.daemon` in the
     foreground.
  3. `Directories()` resolves all three roots via the "default" tier
     (each already accessible to the `aprilcam` user — no env var
     needed) and logs the resolution at INFO.
  4. The daemon enumerates cameras (including the two loopback devices),
     binds the unix socket, binds TCP `0.0.0.0:5280`, and starts mDNS
     advertising.
- **Postconditions**: the daemon is running, camera-enumerated, and
  reachable over TCP and mDNS, with all output captured in journald.
- **Acceptance Criteria**:
  - [ ] `systemctl status aprilcamd` shows `active (running)` after
        `systemctl enable --now aprilcamd`.
  - [ ] `journalctl -u aprilcamd` shows the `Directories` resolution log
        line with `source=default` for all three roots, the TCP bind, and
        mDNS advertising starting — no traceback.
  - [ ] After `systemctl reboot`, the daemon is `active (running)` again
        with no manual intervention.

### SUC-003: A fresh Pi is provisioned and the daemon installed, idempotently
Parent: UC-01 (deployment)

- **Actor**: A programmer (or future operator) running
  `deploy/provision-pi.sh` and following `deploy/README.md`.
- **Preconditions**: SSH access to the target Pi with sudo; a wheel built
  on the dev machine.
- **Main Flow**:
  1. Run `provision-pi.sh user@host`: installs apt packages, creates the
     `aprilcam` system user and its `video` group membership, installs
     the v4l2loopback module config, creates wheel-cache directories.
     Safe to re-run — no step fails or duplicates state on a second run.
  2. Copy the wheel; `pipx install "aprilcam[daemon]==<version>"` as the
     `aprilcam` user (or via `sudo -u aprilcam`).
  3. Install and enable the three systemd units (module config already
     active, bridge units, daemon unit) in the documented order.
- **Postconditions**: the Pi is ready to serve AprilCam without further
  manual steps; running the script again on an already-provisioned Pi
  changes nothing.
- **Acceptance Criteria**:
  - [ ] Running `provision-pi.sh` twice against the same host produces no
        errors and no duplicate users/groups/files on the second run.
  - [ ] `deploy/README.md`'s steps, followed in order on vidar, produce a
        working daemon with no undocumented manual step.

### SUC-004: Retiring the v1 units so exactly one AprilCam stack runs
Parent: UC-01 (deployment)

- **Actor**: A programmer, during the live-verification ticket.
- **Preconditions**: the v2 units (SUC-002, SUC-001) are verified working
  on vidar.
- **Main Flow**:
  1. Stop and disable the v1 `aprilcamd.service` and
     `aprilcam-bridge@.service` units (already inactive/disabled per the
     sprint's starting state, but confirmed rather than assumed).
  2. Remove or mask the v1 unit files so a stray `systemctl start
     aprilcamd` cannot resurrect the old, `jtl`-owned install.
  3. Confirm only the v2 units respond to `systemctl status aprilcamd`/
     `aprilcam-bridge@70`/`aprilcam-bridge@71`.
- **Postconditions**: vidar runs exactly one AprilCam stack (v2, `aprilcam`
  user); the v1 `jtl` checkout is left on disk but documented as
  superseded, not deleted.
- **Acceptance Criteria**:
  - [ ] `systemctl list-unit-files | grep aprilcam` shows only the v2 unit
        names as enabled.
  - [ ] No process owned by `jtl` is running after verification completes.

### SUC-005: The Pi capture path correctly resolves and reads real hardware
Parent: UC-01 (camera enumeration), UC-25 (probe)

- **Actor**: The daemon's `CameraDiscovery`/`resolve_capture_backend`
  code, running against vidar's real `/sys` tree and real loopback
  devices — no fakes.
- **Preconditions**: both bridge units are running and producing frames
  (SUC-001); the daemon has started (SUC-002).
- **Main Flow**:
  1. `list_loopback_cameras()` scans vidar's real
     `/sys/class/video4linux/video*/name` and finds both
     `aprilcam-imx296-*` labels.
  2. `camera/identity.py`'s `_probe_libcamera` stamps
     `hardware_key=f"libcamera:{camera_id}"` for each.
  3. `resolve_capture_backend` matches the hardware key back to a real
     `device_index` and constructs `V4l2LoopbackBackend(f"/dev/video{n}")`.
  4. `V4l2LoopbackBackend.open()` queries the real stream format via
     `v4l2-ctl --get-fmt-video`, confirms YUY2, and starts reading; `read()`
     returns real, correctly color-converted BGR frames.
- **Postconditions**: `camera list -d vidar.local` reports both cameras
  `present`/`usable`; `camera image` returns a real, viewable frame for
  each.
- **Acceptance Criteria**:
  - [ ] Both cameras appear with the correct model/slug derived from their
        real card labels.
  - [ ] A captured frame from each camera is visually correct (not a
        garbled/swapped-channel image) — checked by eye during the live
        ticket.
  - [ ] Any defect the real hardware reveals in enumeration, backend
        selection, or frame reading is fixed and covered by an offline
        unit test using a faked `/sys` tree or faked subprocess output
        that reproduces the same condition, so the fix is guarded going
        forward without needing hardware to re-verify it.

### SUC-006: Live tag detection against a calibrated playfield (best-effort, honestly reported)
Parent: UC-04 (tag detection)

- **Actor**: A programmer, running `aprilcam tags -d vidar.local` (or the
  MCP golden path) from the Mac.
- **Preconditions**: SUC-005 passed; a physical AprilTag/ArUco playfield
  or at least a few tags are in view of one of vidar's cameras.
- **Main Flow**:
  1. If a playfield is available: calibrate it via the daemon's existing
     calibration workflow, then confirm `get_tags`/`aprilcam tags` return
     real detections with plausible pixel positions (and world
     coordinates, if calibrated).
  2. If no playfield/tags are available at execution time: skip this use
     case's detection claim and record in the ticket exactly what was and
     wasn't verified — never inferred from SUC-005's frame-capture success.
- **Postconditions**: either real tag detections are demonstrated and
  recorded, or the ticket explicitly states "not verified: no
  playfield/tags in view" — one of the two, never silent.
- **Acceptance Criteria**:
  - [ ] The ticket file states, in its own words, which of the two Main
        Flow branches actually happened and what was observed.
  - [ ] If detections were observed, at least one full detection record
        (tag id, pixel position, yaw) is quoted in the ticket.

### SUC-007: A remote daemon on vidar is discovered and reached from the Mac
Parent: UC-13 (daemon discovery/connection)

- **Actor**: The AprilCam CLI/MCP client, running on the Mac.
- **Preconditions**: the vidar daemon is running and mDNS-advertising
  (SUC-002); the Mac and vidar share a LAN with mDNS reachable.
- **Main Flow**:
  1. `aprilcam daemon list` (or an explicit `-d vidar.local`) resolves the
     daemon's TCP endpoint.
  2. `aprilcam camera list -d vidar.local` returns live camera state over
     that connection.
- **Postconditions**: the Mac operator can address vidar's daemon by name,
  with no manually-configured IP address.
- **Acceptance Criteria**:
  - [ ] `aprilcam daemon list` includes `vidar.local` (or its advertised
        name) with no `-d` flag needed to discover it.
  - [ ] `aprilcam camera list -d vidar.local` succeeds and matches
        SUC-005's findings.

## GitHub Issues

(GitHub issues linked to this sprint's tickets. Format: `owner/repo#N`.)

## Definition of Ready

Before tickets can be created, all of the following must be true:

- [ ] Sprint planning document is complete (sprint.md, including its
      Architecture and Use Cases sections)
- [ ] Architecture review passed (or skipped, for changes with no
      architectural impact)
- [ ] Stakeholder has approved the sprint plan

## Tickets

| # | Title | Depends On | Issue(s) |
|---|-------|------------|----------|
| 001 | Deploy assets: daemon unit, camera bridge, module config, provision script, runbook | — | plan-daemon-runs-on-a-remote-raspberry-pi |
| 002 | Audit daemon extra for headless-Linux/aarch64 install safety | — | plan-daemon-runs-on-a-remote-raspberry-pi |
| 003 | Validate and fix the Pi capture path against real vidar hardware | 001 | live-tag-detection-and-systemd-persistence |
| 006 | Fix camera bridge watchdog to detect a stalled loopback feed | 001 | camera-bridge-watchdog-never-detects-a-stalled-loopback |
| 007 | Fix `-d vidar.local` hostname connect timeout (IPv6 link-local vs IPv4-only bind) | — | remote-daemon-by-mdns-hostname-times-out-ipv6-link-local-vs-ipv4-only-bind |
| 004 | Live install, verification, and v1 retirement on vidar.local | 001, 002, 003, 006, 007 | plan-daemon-runs-on-a-remote-raspberry-pi, live-tag-detection-and-systemd-persistence |
| 005 | Documentation: Pi camera knowledge doc, wiki systemd section, design-doc gap closure | 004 | plan-daemon-runs-on-a-remote-raspberry-pi, live-tag-detection-and-systemd-persistence |

Tickets execute serially in the order listed. 001 and 002 have no
dependency between them and could in principle run in either order (or in
parallel, if the team-lead chooses to), but both must complete before 003.
006 and 007 were added after 003 found two live defects outside its own
file scope (watchdog exit-code blindness; hostname connect timeout); they
do not depend on each other, only 006 depends on 001 (it fixes an
artifact 001 shipped), and both must complete before 004 since 004's
acceptance criteria now require the fixed hostname connect and rely on
the watchdog behaving correctly during its reboot-persistence check.
