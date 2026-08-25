---
id: 008
title: Update AGENT_GUIDE.md and ROBOT_API_GUIDE.md for persistent mount registrations
status: in-progress
use-cases:
- SUC-001
depends-on:
- '007'
github-issue: ''
issue: mount-registrations-should-persist-across-daemon-restarts.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Update AGENT_GUIDE.md and ROBOT_API_GUIDE.md for persistent mount registrations

## Description

With ticket 007 landed, mount registrations survive a daemon restart.
Two packaged guides currently tell consumers the opposite and instruct
re-registering every session — both need correcting now that the
behavior has actually changed, or this ticket would be shipping a
regression in trust (a guide telling an agent to do unnecessary
work is a minor problem; a guide *not* telling an agent about a real
remaining limitation is the bigger one to avoid re-introducing here).

- `src/aprilcam/AGENT_GUIDE.md` §6, "Mounts and Annotations Are
  Per-Session — Re-register Every Time," currently calls this "the
  rule most likely to bite you" for **both** mounts and annotations.
  After this ticket, **mounts persist; annotations still do not**
  (annotation persistence is explicitly out of scope for sprint 036 —
  a separate, unfiled future issue). The section needs to distinguish
  the two clearly, not just delete the mount half.
- `src/aprilcam/ROBOT_API_GUIDE.md`'s "This registration does not
  survive a daemon restart" section (~lines 106-116) makes the same
  now-false claim in more operational detail (a robot-control-loop
  "call `register_tag` every time you connect" instruction) — this
  needs the same correction, not just a one-line note, since a
  robot program author reading this literally would otherwise keep
  re-registering unnecessarily forever.

Do **not** touch §6's second sub-section on `mount_z`'s
calibrated-but-not-located precondition ("A second, independent
precondition — calibrated is not the same as located") — that
precondition is completely unaffected by persistence (it was already
described correctly as re-evaluated dynamically, and stays that way;
see ticket 007's own note that `mount_z_applied` computation doesn't
change).

## Acceptance Criteria

- [ ] `AGENT_GUIDE.md` §6's heading and opening paragraph no longer
      claim mounts are per-session; it explains that mount
      registrations now persist across restarts (written to
      `state_dir/mounts/registry.json`, per ticket 007), while
      annotations remain per-session and must still be re-drawn every
      session. Consider renaming §6 to something like "Mounts Persist;
      Annotations Are Still Per-Session" or splitting mounts/annotations
      into clearly separated sub-points — implementer's judgment on
      exact wording, but the persistence-vs-not distinction must be
      unambiguous to a first-time reader.
  - [ ] The one-line summary near the end of §6 ("Per-session rule:
        mount registrations (UC-12) and annotations (UC-28) do not
        survive a daemon restart...") is corrected to state mounts
        now persist.
  - [ ] `aprilcam tags mount list` is still correctly described as the
        way to check what's currently registered (unaffected by this
        change — still true whether the registration came from this
        session or a reload).
  - [ ] The `mount_z`-precondition sub-section (calibrated vs. located)
        is left unchanged in substance — only reworded if it directly
        cross-references the now-corrected per-session framing above
        it.
- [ ] `ROBOT_API_GUIDE.md`'s "### This registration does not survive a
      daemon restart" section (and its heading) is corrected: mount
      registrations now persist; a robot program does **not** need to
      call `register_tag` on every connect solely to guard against a
      prior restart (it may still be harmless to do so — upserts
      replace outright per existing `register_tag` semantics — but the
      guide should not present it as *required*). State plainly that
      annotations (mentioned nearby, if at all) are unaffected and
      still per-session, if the surrounding text implies otherwise.
- [ ] Neither guide is changed anywhere outside the mount-persistence
      framing (no scope creep into cadence/motion-semantics wording,
      which tickets 002/003 already own).
- [ ] Both guides remain internally consistent after the edit — read
      each in full after editing, not just the changed section, to
      confirm no other passage still assumes mounts are ephemeral
      (e.g. a stray earlier mention in `AGENT_GUIDE.md`'s bring-up
      order or elsewhere).

## Testing

- **Existing tests to run**: `uv run pytest tests/aprilcam -k guide -q`
  (guide-loading sanity, if such a test module exists; docs-only
  change otherwise has no direct test surface).
- **New tests to write**: none expected — pure documentation
  correction with no behavior to cover (behavior itself is ticket
  007's, already tested there).
- **Verification command**: `uv run pytest tests/aprilcam -k guide -q`
