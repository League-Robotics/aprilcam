---
name: execute-sprint
description: Executes sprint tickets — dispatches programmer agents in dependency order, in parallel worktrees when the sprint opts in, otherwise serially
---


# Execute Sprint Skill

This skill executes all tickets in an active sprint. It supports two
modes: **parallel worktree execution** when the sprint's `worktree`
frontmatter flag is `true`, and the **serial path** otherwise. The
serial path is the historical default and is preserved verbatim below
as the fallback — sprints without the flag see exactly the same
process they always have.

## Inputs

- Active sprint with tickets in `open` status
- Execution lock acquired (`acquire_execution_lock`)
- Sprint branch exists and is checked out

## §0. Mode Selection

Before dispatching any ticket, read the sprint's `worktree` flag (the
`worktree` field returned by `get_sprint_status(sprint_id)`, sourced
from `Sprint.worktree`):

- If `worktree` is `true` **and** all parallel preconditions (§ Parallel
  Preconditions, below) are met **and** `check_independence` finds at
  least one group with more than one ticket: use the **Parallel Path**.
- Otherwise (flag is `false`, a precondition fails, or every group
  produced by `check_independence` is a singleton): use the **Serial
  Path** (§1-§5 below), unmodified.

Mode selection is evaluated once per execution session, and precondition
failures fall back to serial execution for the affected tickets rather
than raising a hard error — parallel execution is strictly an
optimization, never a requirement for making progress.

### Concurrency invariant (read this before implementing the Parallel Path)

**The execution lock remains a project-wide singleton.** This sprint
does NOT relax, partition, or make re-entrant-across-sprints the
execution lock in any way. Exactly one sprint may hold the lock at a
time, exactly as today.

**All controller git operations are sequential.** Creating a worktree,
creating a branch, validating a worktree, merging a ticket branch, and
cleaning up a worktree are all performed by the controller one at a
time, never concurrently with each other — even when they belong to
different tickets in the same group. The controller has a single
working directory and a single HEAD; concurrent git operations against
it would corrupt state. **Only the programmer agents' implementation
work** — writing code, running tests inside their own worktree — runs
concurrently. This is the single most important invariant in this
document: violating it turns a parallel-ticket-execution feature into
an (unsupported, unsafe) concurrent-sprint-execution feature by
accident.

## Parallel Path

### Preflight sweep (session/execution start)

Before any ticket work begins in this session — parallel or serial, though
this only has teeth on the parallel path since the serial path never
creates worktrees — call `reconcile_worktrees(repo_root, sprint_dir)`.
This is the standing reaper (see `clasi.worktree.reconcile_worktrees`)
that reconciles the sprint's worktree audit record against live `git
worktree list` output, auto-cleaning the unambiguous cases and reporting
the rest.

Report a one-line summary of what the sweep cleaned (e.g. "reconcile:
cleaned 2, escalated 0, rogue 0").

If the sweep returns any `escalated` entries, the controller must resolve
every one of them — see "Escalation handling" below — before starting any
new ticket work in this sprint. **Never auto-resume ambiguous work without
explicit stakeholder confirmation.** This preflight sweep is the first of
three trigger points at which `reconcile_worktrees` runs (the other two
are the per-creation gate below and the close-time safety net — see
"Close"); all three invoke the same reconciliation mechanism, just at
different moments in the sprint's lifecycle.

### Preconditions

All of the following must hold before the controller creates any
worktree for a given batch of tickets:

1. **Sprint phase is `executing`** — checked via `get_sprint_phase(sprint_id)`.
2. **Execution lock is held by this sprint** — already acquired via
   `acquire_execution_lock`; re-entrant for this sprint, but still a
   project-wide singleton (see Concurrency invariant above — this is
   NOT relaxed by this feature).
3. **No ticket is currently `in-progress`** — all previously dispatched
   tickets have reached a terminal state (`done` or an escalated
   failure) before a new batch starts.
4. **`worktree: true`** on the sprint.

If any precondition fails, fall back to the Serial Path (§1-§5) for the
affected tickets. This is a soft fallback, not an error condition.

### Grouping

Read all `open` tickets in the sprint and their plan files (ticket body
and/or separate plan file, per the sprint's convention). Call
`check_independence` (see `clasi.worktree`) on the full candidate set to
produce an ordered list of groups: tickets within a group are mutually
independent and may run in parallel; groups themselves run serially, in
an order consistent with `depends-on`.

If every group `check_independence` returns is a singleton (no group
has more than one ticket), there is no parallelism opportunity even
with the flag on — use the Serial Path instead (see "Serial fallback"
note at the end of this section).

### Per-group loop

For each group, in order:

1. **Per-creation gate**: immediately before this group's worktrees are
   created (step 2 below), call `reconcile_worktrees(repo_root,
   sprint_dir)` again. Report the one-line cleaned/escalated/rogue
   summary as at the preflight sweep. **If any `escalated` entries
   remain unresolved, this is a BLOCKING CONDITION: the controller STOPS
   and must NOT create any new worktrees for this group** until every
   escalated entry has been resolved (see "Escalation handling" below).
   This is not a warning to log and proceed past — worktree accumulation
   is exactly what this gate exists to prevent, and it only works if the
   controller actually halts here rather than treating a pile of
   unresolved worktrees as background noise.
2. **Setup (sequential, per ticket in the group)**: for every ticket in
   the group, create its worktree (`create_worktree`), create its
   per-ticket branch (`create_ticket_branch`), write an audit record,
   and set the ticket's status to `in-progress` via
   `update_ticket_status`.
3. **Dispatch (concurrent)**: dispatch one programmer agent per ticket
   in the group using concurrent background Agent tool calls, each
   pointed at that ticket's own worktree directory. This is the only
   step in the loop that runs concurrently.
4. **Wait**: wait for every dispatch in the group to return before
   proceeding.
5. **Per-ticket validate → merge → cleanup (sequential, never
   concurrent — this is the single-HEAD merge serialization
   constraint)**: for each ticket in the group, one at a time:
   a. `validate_worktree`. On failure, retry by re-dispatching the
      programmer agent, up to 3 total attempts. If all attempts fail,
      mark the audit record `failed`, run
      `cleanup_worktree(keep_branch=True)` (retain the branch for
      inspection), and escalate to the stakeholder.
   b. `merge_ticket_branch`. On `MergeConflictError`: mark the audit
      record `conflict`, retain the worktree (do not clean it up), and
      escalate to the stakeholder with the conflicting files and the
      worktree path. On success: mark the audit record `merged`, call
      `move_ticket_to_done`, then **immediately** — no deferral to
      sprint close — call `cleanup_worktree(keep_branch=False)`, and
      mark the audit record `cleaned_up`.
6. **Advance**: move on to the next group only when every ticket in the
   current group has reached `merged`/`cleaned_up` or has been
   explicitly escalated. Do not start the next group while any ticket
   in the current group is unresolved.

### Escalation handling

Both the preflight sweep and the per-creation gate call the same
`reconcile_worktrees` mechanism, and both can return `escalated` entries
that the controller must resolve before proceeding. "Resolve" means one
of exactly three actions, chosen per entry:

- **Recover**: re-dispatch the programmer agent into the *existing*
  worktree (do not create a new one) to finish or fix the work, then
  continue the normal validate → merge → cleanup flow for that ticket.
  Use this when the escalated work looks salvageable — e.g. an
  `in_progress` entry whose worktree still has uncommitted work in
  progress and no crash indication.
- **Abandon**: call `cleanup_worktree(keep_branch=True)` to remove the
  worktree directory while retaining the branch for later human
  inspection. Update the audit record accordingly. Use this when the
  work is not worth recovering but should not be silently discarded —
  the branch remains as a paper trail.
- **Inspect**: take no automated action on the worktree or branch.
  Acknowledge the entry and move it into an explicitly-tracked "known,
  deferred" state (e.g. a note in the audit record or sprint log) so
  that it does not silently reappear as a fresh `escalated` entry on
  every subsequent sweep. Use this when a human needs to look at the
  work before any decision can be made.

**Never auto-resume ambiguous work without explicit stakeholder
confirmation.** The reaper itself never guesses at recover/abandon/
inspect — it only classifies and safely auto-cleans the unambiguous
cases (`merged-not-cleaned`, `clean-but-abandoned`); everything it
returns in `escalated` is, by construction, a case the controller (and
where needed, the stakeholder) must decide on explicitly.

This subsection also covers two related recovery paths that surface
through the same `escalated`/audit machinery:

- **Orphaned worktree (controller crash)**: if the controller crashes or
  is killed mid-lifecycle, a worktree can be left in a non-terminal audit
  state (`worktree_created`, `branch_created`, `in_progress`) with no
  programmer agent actually running against it. The next sweep
  (whichever trigger point runs first — preflight, per-creation gate, or
  close) surfaces it as `escalated`. Resolve it with the same
  recover/abandon/inspect choice above: recover means re-dispatching a
  programmer into that worktree as if picking up where the crash left
  off; abandon and inspect are as described above.
- **Abandoned branch**: a `ticket/<sprint-id>-*` branch can exist with no
  live worktree registered against it — either because the worktree was
  already cleaned up but the branch was deliberately retained
  (`failed`/`conflict`), or because the audit entry is missing entirely
  (a rogue branch created outside the tracked lifecycle). `reconcile_
  worktrees` reports entries with no live worktree in `rogue` rather than
  `escalated` (there's no directory left to act on), but the same
  resolution question applies to the branch: retain it for inspection, or
  delete it once a human confirms it is safe to discard. Do not delete a
  `failed`/`conflict` branch without that confirmation.

**Serial fallback within the parallel path**: even when `worktree:
true` and preconditions hold, any group that `check_independence`
returns as a singleton runs through the ordinary single-ticket dispatch
described in §3 of the Serial Path below — there is nothing to
parallelize for a group of one.

### Close

When all groups are done, invoke `close-sprint` as usual (see §5 of the
Serial Path). Its reconciliation safety net — the close-time extension
of `_prune_sprint_worktrees` (ticket 008) — is **the final reconcile
pass**: the third of the three `reconcile_worktrees` trigger points
(preflight sweep at session start, the per-creation gate before each
group, and this close-time pass), catching anything the per-group loop
did not already clean up. It does not replace the immediate cleanup
described above.

---

## Serial Path

The following process (§1-§5) is preserved verbatim from the
serial-only version of this skill. It applies whenever §0 selects the
serial path, and it is otherwise unchanged.

### 1. Read Tickets

Read all tickets from the sprint's `tickets/` directory. Parse
frontmatter for `status`, `depends-on`, and `id`.

### 2. Order by Dependencies

Build a dependency graph from `depends-on` fields and produce a flat,
topologically-sorted list of tickets. Tie-breaks by ticket id ascending.

There are no execution groups. Tickets run one at a time.

### 3. Dispatch Programmer Agents Serially

For each ticket in dependency order:

1. Verify the ticket is `open` and all of its `depends-on` tickets are
   `done`. If not, stop and report the inconsistency.
2. Update the ticket status to `in-progress` via
   `update_ticket_status(path, "in-progress")`.
3. Invoke the programmer agent via the Agent tool with:
   - Path to the ticket file
   - Path to the ticket plan (if separate)
   - Sprint ID and ticket ID
   - Sprint branch name (the agent works on this branch directly)
   - Architecture context, sourced per `Project.design_docs_opt_in`:
     - **Not opted in**: the relevant Architecture section of the
       sprint's `sprint.md` (as today).
     - **Opted in**: the path(s) to the relevant canonical subsystem
       doc(s), co-located as `<subsystem_path>/DESIGN.md`
       (e.g. `src/clasi/tools/DESIGN.md`) — or, for the system-level
       document, `docs/design/design.md` — *plus* the path to this
       sprint's edited overlay copy of that same doc under
       `clasi/sprints/NNN-slug/design/<name>.md` — both paths, not just
       one. The canonical doc gives the agent the subsystem's settled,
       pre-sprint understanding; the overlay copy gives it this sprint's
       planned changes to that understanding. Identify which doc(s)
       apply by checking which docs the sprint's `design/` directory
       contains (the same doc_names the sprint-planner passed to
       `seed_sprint_design_overlay`) and matching against the ticket's
       scope; a ticket touching a subsystem with no corresponding
       overlay file gets only the canonical doc path (nothing changed
       there this sprint). If the sprint carries no `design/` directory
       at all (trivial/compact sprint, or opted-out project), fall back
       to the not-opted-in behavior above.
4. Wait for the programmer agent to complete before moving on.
5. Verify `status: done` is set in the ticket's frontmatter.
6. Call `move_ticket_to_done(ticket_path)` where `ticket_path` is the
   relative path: `docs/clasi/sprints/NNN-slug/tickets/NNN-slug.md`.
   This is a team-lead responsibility — the programmer sets the
   frontmatter; the team-lead moves the file.
7. Continue with the next ticket.

**Do not** invoke a second programmer agent until the first has
returned. Do not create git worktrees. Do not branch off the sprint
branch.

### 4. Handle Failures

If a programmer agent fails, escalate to the stakeholder. Do not skip
the ticket and continue — the dependency chain assumes each prior
ticket is complete.

If a programmer agent leaves a ticket in `in-progress` (e.g. because
tests failed and the agent reported back without marking it done):
fix the issue in-process or with a follow-up programmer dispatch on
the same ticket. Either way, the ticket must end at `done` before
moving to the next one.

**Ticket completion is mandatory.** When a programmer completes a
ticket, its status must be set to `done` and `move_ticket_to_done`
called. There is no valid reason to leave a completed ticket in an
incomplete state. If the stakeholder says "leave it open", that means
leave the sprint open — the ticket itself must still be marked done.

### 5. Close Sprint

After all tickets are `done`:

1. Verify all tickets have `status: done`.
2. Run the full test suite on the sprint branch.
3. Present sprint summary to stakeholder.
4. Invoke the `close-sprint` skill.

## Output

- All tickets implemented and marked done
- All tests passing on sprint branch
- Sprint ready for review and close
