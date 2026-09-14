---
id: '001'
title: 'Deploy assets: daemon unit, camera bridge, module config, provision script,
  runbook'
status: done
use-cases: []
depends-on: []
github-issue: ''
issue: plan-daemon-runs-on-a-remote-raspberry-pi-every-client-discovers-it-and-connects-over-tcp.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Deploy assets: daemon unit, camera bridge, module config, provision script, runbook

## Description

v2 has never shipped deploy assets — `docs/wiki/daemon.md`'s systemd section
says "the repo ships no unit files yet" and shows only a two-line
illustrative `ExecStart=`. This ticket creates the real `deploy/` directory
(sprint.md Architecture, Step 3): a daemon systemd unit, a per-camera
libcamera→v4l2loopback bridge (script + templated unit), the v4l2loopback
module config, an idempotent provision script, and a runbook README. This
is offline, foundation work — no SSH to vidar required — that ticket 004
then deploys and exercises live.

Carry forward v1's proven design (`archive/deploy/*`,
`archive/scripts/setup-vidar-cameras.sh`,
`archive/docs/knowledge/v4l2loopback-vanishes-after-kernel-upgrade.md`),
adapted to v2's much simpler process model:

- v2's daemon entry point takes **no flags** (`python -m aprilcam.daemon`
  is exactly `daemon/__main__.py:main()` — no `--tcp`/`--unix`/`--force`
  exist in v2). Do not invent flags that don't exist.
- v2's `Directories` resolves FHS roots via `os.access`, not `euid == 0`
  (`config/directories.py`) — **no `APRILCAM_SYSTEM=1` or equivalent env
  var is needed or should be added.** `ConfigurationDirectory=aprilcam`/
  `StateDirectory=aprilcam`/`RuntimeDirectory=aprilcam` in the unit, plus
  `User=aprilcam`, is sufficient: systemd creates and chowns those
  directories to the service user before `ExecStart` runs, which makes
  them accessible/writable to that user, which is all `Directories`
  checks.
- Service user: a dedicated system user `aprilcam` (see sprint.md Design
  Rationale) — not `ros`, `jtl`, or any human login. The provision script
  creates it (`useradd --system --no-create-home --shell
  /usr/sbin/nologin aprilcam`) and adds it to the `video` group.
- The bridge (script + `aprilcam-bridge@.service`) is unchanged in spirit
  from v1: stagger by camera position (4s × position), `libcamerasrc !
  queue max-size-buffers=6 leaky=downstream ! videoconvert !
  video/x-raw,format=YUY2 ! v4l2sink`, and a watchdog that polls
  `v4l2-ctl --stream-mmap --stream-count=1` every ~10s and exits non-zero
  (for `Restart=always` to relaunch) if a read stalls — v1's fix for the
  libcamerasrc-SIGSEGV-then-gst-launch-spins-forever failure mode. Keep
  the bridge running as a static system user (not the daemon's dedicated
  user necessarily — reuse `aprilcam` for simplicity, `SupplementaryGroups=video`),
  never `DynamicUser=`.
- v4l2loopback module config matches vidar's already-working values
  exactly (so this ticket's config file, if copied to a fresh Pi, would
  reproduce vidar's current state): `video_nr=70,71
  card_label=aprilcam-imx296-88000,aprilcam-imx296-80000
  exclusive_caps=1,1 max_buffers=16`, loaded via `/etc/modules-load.d/`
  + `/etc/modprobe.d/`. Do not hardcode these as vidar-specific magic
  numbers with no explanation — comment why `max_buffers=16` matters
  (v1's knowledge doc: fewer buffers starves the v4l2-ctl reader to
  0.2-0.6fps).
- `provision-pi.sh` must be idempotent (safe to re-run): create the
  `aprilcam` user/group membership only if absent, install packages via
  `apt-get install` (already idempotent), write module config only if
  changed, never blindly overwrite a running install.

## Acceptance Criteria

- [x] `deploy/aprilcamd.service` exists: `User=aprilcam`,
      `SupplementaryGroups=video`, `ConfigurationDirectory=aprilcam`,
      `StateDirectory=aprilcam`, `RuntimeDirectory=aprilcam`,
      `ExecStart=<venv-or-pipx-path>/python -m aprilcam.daemon`
      (foreground, no flags), `Restart=on-failure`, output to journal.
- [x] `deploy/aprilcam-camera-bridge` (script) and
      `deploy/aprilcam-bridge@.service` (template unit) exist, carry
      forward the stagger/queue/watchdog design from
      `archive/scripts/setup-vidar-cameras.sh`, and are parameterized by
      camera position + loopback device path (not hardcoded to `70`/`71`
      alone — vidar's are 70/71, but the template should read
      `%i`/an argument, matching v1's `%i` convention).
- [x] v4l2loopback module config file(s) under `deploy/` reproduce vidar's
      current `/etc/modprobe.d/aprilcam.conf` /
      `/etc/modules-load.d/aprilcam.conf` content exactly.
- [x] `deploy/provision-pi.sh` installs required apt packages (`v4l-utils`,
      `gstreamer1.0-tools` and libcamera GStreamer plugin package(s) if not
      already present, `pipx` if absent), creates the `aprilcam` system
      user and adds it (and itself, idempotently) to `video`, installs the
      module config, and is safe to run twice with no duplicate
      users/errors on the second run.
- [x] `deploy/README.md` documents the full ordered runbook: build wheel
      (Mac) → provision Pi → copy + pipx install wheel → install module
      config → install/enable bridge units → install/enable daemon unit →
      verify. States the required order explicitly (module config before
      bridge before daemon) per sprint.md's Migration Concerns.
- [x] Every unit file's `ExecStart=`/`ExecStartPre=` references an entry
      point or script path that actually exists in v2 (verified by
      grepping `pyproject.toml`'s `[project.scripts]` and this repo's
      actual module paths — no v1-only flags, no `aprilcam.daemon`
      arguments that don't exist).
- [x] `bash -n` (and `shellcheck`, if available on this machine) passes
      clean on `provision-pi.sh` and `aprilcam-camera-bridge`.
- [x] Any small offline test that adds value (e.g. a test asserting the
      unit file's `ExecStart` line matches a real importable module) is
      added under the existing test tree; this is inspection-based
      validation, not a new testing framework.

## Implementation Plan

**Approach**: Read `archive/deploy/{README.md,aprilcamd.service,
provision-pi.sh}` and `archive/scripts/setup-vidar-cameras.sh` in full for
the proven pipeline/stagger/watchdog parameters, then write v2-shaped
equivalents from scratch — do not copy files verbatim (CLAUDE.md's "convert,
don't rewrite blindly — and don't copy blindly either"). Drop everything
v1-specific that no longer applies in v2: `APRILCAM_SYSTEM=1`,
`--force`/`--tcp`/`--unix` daemon flags, the `/tmp/aprilcamd.lock`
machine-global-lock discussion (v2's singleton is `<socket_dir>/daemon.lock`
via `daemon/lifecycle.py`, unrelated to `PrivateTmp`), and any reference to
`APRILCAM_DATA_DIR` (v2 uses `APRILCAM_CONFIG_DIR`/`APRILCAM_STATE_DIR`;
socket dir is not configurable at all — see `config/directories.py`'s
module docstring).

**Files to create**:
- `deploy/aprilcamd.service`
- `deploy/aprilcam-camera-bridge`
- `deploy/aprilcam-bridge@.service`
- `deploy/v4l2loopback-aprilcam.conf` (modprobe.d) and
  `deploy/aprilcam-modules-load.conf` (modules-load.d), or one file per
  v1's convention — match filenames to what's actually on vidar today
  (`/etc/modprobe.d/aprilcam.conf`, `/etc/modules-load.d/aprilcam.conf`)
  so a diff against the live host is trivial during ticket 004.
- `deploy/provision-pi.sh`
- `deploy/README.md`

**Files to modify**: none expected in `src/aprilcam/` for this ticket.

**Testing plan**: `bash -n`/shellcheck on both scripts; manual/inspection
review of each unit file against `pyproject.toml`'s `[project.scripts]`
and `daemon/__main__.py`; run `scripts/test-gate.sh` to confirm zero
regressions (no source changes expected, but confirms the gate still
passes before hardware-touching tickets begin).

**Documentation updates**: none yet — `docs/wiki/daemon.md`'s systemd
section update is ticket 005's job, done after live verification confirms
these assets actually work as written.
