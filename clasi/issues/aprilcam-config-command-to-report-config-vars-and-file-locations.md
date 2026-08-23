---
status: pending
---

# Add `aprilcam config` to report configuration vars and file locations

## Description

There is no single command that answers "where is everything, and what
decided that?". Today the answer is scattered:

- `aprilcam daemon status` prints `config_dir` and `state_dir`, but only
  as a side effect of reporting readiness, and only for a *running*
  daemon. It omits `socket_dir`, omits every environment variable, and
  omits which cascade tier produced each root.
- `Directories` already computes exactly this — it resolves each root
  through flag > env > system default > unprivileged fallback and
  records the winning tier in `config_dir_source` / `state_dir_source`
  (`src/aprilcam/config/directories.py`). Nothing surfaces it.
- Finding a calibration file currently means knowing, by memory, that it
  lives at `<state_dir>/cameras/<slug>/calibration.json` and that
  `state_dir` fell back to `~/.local/share/aprilcam` because
  `/var/lib/aprilcam` was not writable.

## Proposal

`aprilcam config` — print the resolved configuration with provenance,
and work with no daemon running (like `aprilcam guide`), since a common
use is diagnosing why the daemon won't start or is reading the wrong
tree.

Should report:

- **Roots**, each with the tier that produced it: `config_dir`,
  `state_dir`, `socket_dir`.
- **Environment variables** consulted, with their current values and
  whether each is set or unset: `APRILCAM_CONFIG_DIR`,
  `APRILCAM_STATE_DIR`, `XDG_RUNTIME_DIR`, plus the daemon host/port
  vars.
- **File locations** derived from those roots, ideally with an
  exists/missing marker: `playfields/*.json`, `cameras/<slug>/config.json`,
  `cameras/<slug>/calibration.json`, `cameras/registry.json`,
  `hosts.json`, `tags.json`.
- **Load errors** — `ConfigLoader.load_config_dir` already returns a
  `ScanResult.errors` tuple naming every file it rejected. These are
  currently invisible: a v1-shaped `config.json` with no `name` field is
  silently skipped, so the camera simply never appears and nothing says
  why. Surfacing these is arguably the highest-value part of the command.

## Motivation

Discovered while consolidating config into `config/aprilcam` (Aug 2026).
Three camera configs in the live config dir were being silently rejected
by the loader for missing a `name` field; the only symptom was that the
daemon knew about one camera out of four. An `aprilcam config` that
printed the loader's own error list would have made that a five-second
diagnosis instead of a code-reading exercise.

Related: the config/state split means pushed config is runtime-only
while calibration is file-backed, which is genuinely surprising until
you see the two roots side by side.
