---
status: pending
---

# `aprilcam calibrate --joint` crashes with NameError

`src/aprilcam/cli/calibrate_cli.py` references `field_width` and
`field_height` at lines ~287, 288, 294, and 312, but neither name is
ever assigned in the module — `args.width`/`args.height` are parsed but
never bound to those names. Any `aprilcam calibrate --joint` invocation
dies with `NameError: name 'field_width' is not defined` on the first
`calibrate_single` call. The non-joint branch is unaffected because it
takes dimensions from the playfield definition.

Likely collateral from the sprint-012 move to playfield-def-sourced
dimensions, where the joint path wasn't updated.

## Resolution options

1. Quick fix: bind the names from the playfield definition (or
   `args.width`/`args.height`) in the joint branch.
2. Preferred if [[move-calibration-execution-into-daemon]] lands first:
   the joint CLI path is superseded by the daemon Calibrate RPC —
   remove it instead of fixing it.
