---
source_file: tags-DESIGN.md
source_hash: 2eec9bef99f880985aff8612052bbb3a9b41be66e7b3240d40cb0e99c4efb709
---
# Diff: tags-DESIGN.md

Comparison of the sprint overlay copy of `tags-DESIGN.md` against its pristine (seed-commit) canonical version.

```diff
--- tags-DESIGN.md (pristine)
+++ tags-DESIGN.md (current)
@@ -13,12 +13,25 @@
 install, which is exactly when it's needed — an operator generates tags
 *before* there is a field to look at.
 
-This is why OpenCV is a **base** dependency rather than gated behind the
-`daemon` extra the rest of the vision stack lives behind (design.md §2's
-single-vision-authority principle names `tags/` generation as its one
-carved-out exception). Being importable everywhere is not a license to do
-pixel work outside the daemon, though: generation only ever writes images, it
-never reads or processes them.
+**Revised, sprint 036.** Sprint 028 originally recorded OpenCV as a
+**permanent, unconditional base** dependency specifically so `aprilcam
+taggen` would work on a bare install with zero setup — the whole point of
+`tags/` being "the one capability that works before there is a field to
+look at." Sprint 036 reversed that: `opencv-contrib-python` moved to the
+`daemon` extra (closing an unrelated, accidental packaging gap that let
+`cv2` leak into `client/renderer.py` and `cli/locate.py` — see
+`calibration/DESIGN.md`), and this module's own `cv2` import became
+**lazy, at the point of use** rather than module scope. The command is
+still importable and discoverable everywhere (`tags/generator.py` itself
+imports cleanly with no `cv2` in the process), and `design.md` §2's
+single-vision-authority principle still names `tags/` as its one
+import-boundary exception — but *running* `tags generate`/`aprilcam
+taggen` without the `daemon` extra installed now fails with a clear
+"install `aprilcam[daemon]`" message instead of silently working, because
+`cv2` genuinely is not there to import. §4 and §6 below detail the
+mechanism and the trade this reversal accepts. Being importable everywhere
+is not a license to do pixel work outside the daemon, though: generation
+only ever writes images, it never reads or processes them.
 
 The two supported dictionaries are fixed system-wide (spec §4.10): AprilTag
 **36h11** and ArUco **4x4** — a `TagFamily` value fully determines the
@@ -45,10 +58,15 @@
 
 - **`generator.py` — `TagGenerator`.** The concrete class, deriving from
   `tags.interfaces.TagGenerator` the same way `client.discovery.Discovery`
-  derives from `client.interfaces.Discovery`. Imports `cv2` and nothing else
+  derives from `client.interfaces.Discovery`. Nothing else
   from this tree beyond `errors`, `types`, and its own `interfaces` module —
   no `daemon`, `client`, or `vision` import anywhere in the call path,
-  matching the offline-utility contract in §1.
+  matching the offline-utility contract in §1. `cv2` is imported **lazily**
+  (sprint 036) — inside the functions that actually need it, not at module
+  scope — with `ImportError` caught and re-raised as a clear
+  "install `aprilcam[daemon]`" message; simply `import
+  aprilcam.tags.generator` or constructing a `TagGenerator` touches no
+  `cv2` symbol at all.
 
 Note: `tags/__init__.py` is currently empty (no re-exports) — callers import
 directly from `tags.generator` and `tags.interfaces`.
@@ -97,6 +115,15 @@
 `TagGenerator.generate()`'s algorithm, in the order it actually runs
 (`generator.py`):
 
+0. **Import `cv2`, or fail clearly (sprint 036).** The first thing
+   `generate()` (and `valid_ids()`, which it calls) does is import `cv2`
+   inside the function body; an `ImportError` here is caught and
+   re-raised as a message naming `pip install 'aprilcam[daemon]'` as the
+   fix, never a bare traceback. The family→dictionary mapping
+   (`_DICTIONARY_BY_FAMILY`, previously a module-level dict built from
+   `cv.aruco.DICT_APRILTAG_36h11`/`DICT_4X4_50` at import time) moves
+   inside this same lazy-import boundary so that importing this module —
+   or constructing a `TagGenerator` — never touches a `cv2` symbol.
 1. **Validate every id up front.** `self.valid_ids(family)` computes the
    dictionary's range (`cv.aruco.getPredefinedDictionary(...).bytesList.shape[0]`,
    0-based); each requested id is checked against it, raising `InvalidTagId`
@@ -138,7 +165,8 @@
   CLI `choices=` string).
 - `aprilcam.errors` — `InvalidTagId`, `UnwritableOutputPath`.
 - `cv2` (`cv.aruco`) — the only external vision dependency; nothing else
-  from this codebase.
+  from this codebase. Imported lazily (sprint 036), from the `daemon`
+  extra — see §4, step 0.
 
 The one caller today is `cli/tags.py`'s `cmd_generate` (`aprilcam tags
 generate --family FAMILY --ids SPEC --size-cm CM --out DIR [--dpi DPI]`,
@@ -164,14 +192,31 @@
   contracts. This was recorded as **not decided against** — genuinely open,
   a future sprint's call. Today an operator needing a multi-tag print sheet
   runs `tags generate` once per id/range and assembles a sheet externally.
-- **`docs/design/specification.md` §4.10 has a stale sentence.** It still
-  describes OpenCV as tolerated in `tags/` "or replaced with pre-rendered
-  dictionaries to make the base install fully cv2-free" — the opposite of
-  UC-21's actual, recorded decision (cv2 as a permanent base dependency).
-  Sprint 028 noted this explicitly as pre-existing drift it did not
-  introduce and did not fix, since `specification.md`/`design.md` upkeep
-  belongs to the standing architecture-consolidation process, not an
-  individual sprint. Worth fixing opportunistically, not urgent.
+- **`docs/design/specification.md` §4.10's stale sentence is now partially
+  re-aligned, not fully resolved.** It describes OpenCV as tolerated in
+  `tags/` "or replaced with pre-rendered dictionaries to make the base
+  install fully cv2-free." Sprint 028 recorded this as the opposite of
+  the then-current decision (cv2 as a permanent, unconditional base
+  dependency); sprint 036's reversal (lazy import, `daemon`-extra-gated)
+  lands somewhere between the two — the base install is `cv2`-free at
+  *import* time but `tags generate` still needs the `daemon` extra to
+  actually run, which is neither "permanent base dependency" nor
+  "pre-rendered dictionaries." `specification.md` §4.10 itself is still
+  unedited; reconciling its exact wording remains standing
+  architecture-consolidation work, not this sprint's ticket scope.
+- **The lazy-import trade accepted by sprint 036, stated plainly.** A
+  fresh base-only install can no longer generate a tag image without an
+  extra install step (`pip install 'aprilcam[daemon]'`) — a real,
+  if small, regression in the "just works" experience sprint 028
+  deliberately optimized for. This was accepted because the alternative
+  (keeping `opencv-contrib-python` an unconditional base dependency) is
+  what let `cv2` leak into `client/renderer.py` and `cli/locate.py`
+  (`calibration/DESIGN.md`'s resolved gap) and contradicts the
+  higher-authority, stakeholder-facing constraint in the project overview
+  ("OpenCV is installed only via the `daemon` extra; the base package
+  requires Pillow instead") — that constraint names no `tags/` exception,
+  unlike `design.md` §2's own architectural principle, which does. Given
+  the conflict, the stakeholder-facing constraint took precedence.
 - Both `tags/interfaces.py` and `tags/generator.py`'s own module docstrings
   (as originally authored) describe the concrete `TagGenerator` as not yet
   existing ("No concrete implementation exists yet (out of this pass's
```
