---
id: '005'
title: Reconcile systemd unit and deploy docs; remove repo .env CWD-relative override
status: done
use-cases:
- SUC-001
- SUC-006
depends-on:
- '004'
github-issue: ''
issue: split-config-from-state-into-fhs-xdg-roots.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Reconcile systemd unit and deploy docs; remove repo .env CWD-relative override

## Description

Final ticket: reconcile the two real "launch-location-dependent config"
cases the issue calls out — the Pi's systemd unit and the repo's Mac-dev
`.env`.

**`deploy/aprilcamd.service`** already declares `ConfigurationDirectory=
aprilcam` (confirmed present from prior work — this ticket does not add
it). It also currently pins:
```
Environment=APRILCAM_DATA_DIR=/home/eric/aprilcam-data
```
— a non-FHS, user-specific absolute path that bypasses the FHS
auto-selection this sprint relies on. **Per stakeholder decision, remove
this `Environment=` line.** With it gone, `StateDirectory=aprilcam` (already
present) provisions `/var/lib/aprilcam` and the daemon's FHS branch (root
euid under systemd, or explicit `APRILCAM_SYSTEM=1` if the unit runs
`DynamicUser=yes`) resolves `data_dir` to `/var/lib/aprilcam` and
`config_dir` to `/etc/aprilcam` automatically — both provisioned by
systemd's `*Directory=` directives before `ExecStart` runs. Confirm
whether the unit needs an explicit `Environment=APRILCAM_SYSTEM=1` line
depending on how the service user is configured (see the unit's existing
`User=eric` line — a static non-root user, so `APRILCAM_SYSTEM=1` is
likely required for FHS selection to trigger, since euid won't be 0).

**Pi-side data migration** (document as a required deployment step in
`deploy/README.md`, not automated by this ticket): before removing the
`Environment=APRILCAM_DATA_DIR` override on a Pi with existing data under
`/home/eric/aprilcam-data`, the operator must:
1. Run `aprilcam migrate-dirs` on the Pi while the override is still in
   effect (or rely on the legacy-layout fallback/warning from ticket 004)
   to relocate config files (`config.json`, `playfields/*.json`) into
   `/etc/aprilcam`.
2. Manually relocate the remaining state files (`calibration.json`,
   `registry.json`, `hosts.json`, `info.json`, `paths.json`) from
   `/home/eric/aprilcam-data` into `/var/lib/aprilcam` (a plain file move
   — `migrate-dirs` does not touch state files by design).
3. Remove the `Environment=APRILCAM_DATA_DIR=...` line from the unit.
4. Restart the unit; verify the daemon opens cameras and `get_tags`
   returns world coordinates from the new FHS locations.

**`deploy/README.md`** gains: an `APRILCAM_CONFIG_DIR` row/section, an FHS
table row for the config category (`/etc/aprilcam`), and the Pi migration
steps above as a documented upgrade path for existing deployments.

**Repo `.env`**: remove line 15, `APRILCAM_DATA_DIR=./data/aprilcam`, so a
fresh Mac dev checkout falls through to XDG defaults
(`~/.local/share/aprilcam` state, `~/.config/aprilcam` config) per the
locked stakeholder decision. Note in `deploy/README.md` or a code comment
that developers with existing `./data/aprilcam` content should run
`aprilcam migrate-dirs` (relying on the ticket-004 legacy fallback to keep
things working in the interim) to relocate config into
`~/.config/aprilcam`.

See `../architecture-update.md` §6-7 ("`deploy/aprilcamd.service` and
`deploy/README.md`", "Repo `.env`") and Migration Concerns
("`deploy/aprilcamd.service`'s hardcoded `APRILCAM_DATA_DIR` — resolved")
for full design detail.

## Acceptance Criteria

- [x] `deploy/aprilcamd.service` no longer contains
      `Environment=APRILCAM_DATA_DIR=/home/eric/aprilcam-data`.
- [x] `deploy/aprilcamd.service` still declares both
      `ConfigurationDirectory=aprilcam` and `StateDirectory=aprilcam`
      (confirmed present, not removed).
- [x] If the service user configuration requires it (non-root `User=`
      without `DynamicUser`), the unit sets `Environment=APRILCAM_SYSTEM=1`
      so FHS directory selection triggers correctly under systemd.
- [x] `deploy/README.md` documents `APRILCAM_CONFIG_DIR`, adds the FHS
      config row (`/etc/aprilcam`), and includes the four-step Pi
      migration procedure (migrate-dirs → manual state move → remove
      override → restart + verify).
- [x] Repo `.env` no longer contains `APRILCAM_DATA_DIR=./data/aprilcam`.
- [x] A fresh `Config.load()` on a clean checkout (no `.env` override, no
      `APRILCAM_*` env vars set) resolves to XDG defaults
      (`~/.local/share/aprilcam` state, `~/.config/aprilcam` config) on a
      non-root Mac dev machine.
- [x] Full `uv run pytest` green — including a systemd-unit smoke test
      (directive presence, absence of the removed `Environment=` line) if
      such a test exists or is added; no test touches a real Pi or real
      `/etc`/`/var/lib`.

## Testing

- **Existing tests to run**: full `uv run pytest` (final sprint-wide
  regression pass — this ticket closes the sprint).
- **New tests to write**:
  - Parse `deploy/aprilcamd.service` and assert:
    `ConfigurationDirectory=aprilcam` present, `StateDirectory=aprilcam`
    present, no `Environment=APRILCAM_DATA_DIR=` line present.
  - Parse repo `.env` (or the packaged `.env.example` if that's the
    canonical test target) and assert no `APRILCAM_DATA_DIR=` line
    remains that points at a CWD-relative path.
  - `Config.load()` smoke test simulating a clean non-root checkout (no
    `.env`, no `APRILCAM_*` env, tmp `HOME`) resolves `data_dir`/
    `config_dir` to the expected XDG paths under the tmp `HOME`.
  - If `deploy/README.md` content is asserted anywhere in the test suite
    (unlikely, but check), update accordingly; otherwise this is a
    docs-only verification via manual review.
- **Verification command**: `uv run pytest`
