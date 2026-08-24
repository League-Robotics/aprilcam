#!/usr/bin/env bash
# The canonical close-sprint test gate (sprint 035 ticket 008).
#
# Runs the full suite minus every test marked `camera` or `integration`
# -- i.e. everything that does not need real capture hardware or a real
# daemon/end-to-end process. That selection is exactly the L0 suite
# union (`tests/image_matrix/`, sprint 035 tickets 005-007) plus every
# other pre-existing offline unit/system test in the tree: L0 itself
# carries no pytest marker at all, so it is *selected* by "not camera
# and not integration" rather than needing its own marker or its own
# path argument here.
#
# Run this exact command from CI, from `close_sprint`, and by hand --
# one invocation, no drift between what a human runs and what the gate
# checks.
#
# Exits non-zero on any failure (pytest's own exit code), including
# collection errors. A clean run may still report `skipped` tests --
# see docs/test-plans/camera-image-matrix.md and
# tests/image_matrix/conftest.py for why L0 skips cleanly rather than
# failing when tests/images/ is absent or its captured field state is
# not yet stakeholder-`confirmed` (true today for `fs-20260823`).

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

exec uv run pytest -m "not camera and not integration" "$@"
