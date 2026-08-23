---
date: 2026-08-21
sprint: 031
category: missing-instruction
---

## What Happened

While executing sprint 031 (11 tickets, serial dispatch), the team-lead
pasted each programmer subagent's full completion report into the chat
verbatim after every ticket finished — acceptance criteria quoted in
full, every changed file listed, full test-command output, commit
hashes, design rationale. Across 11 tickets this produced a large
volume of repeated, densely-formatted text. The stakeholder said "that
was a dump," "I can't digest that," and asked what it was for and
whether it was being saved anywhere.

## What Should Have Happened

After each ticket/subagent completes, the team-lead should give a
short status line in chat — ticket id, done/blocked, and a one-clause
reason only if something is notable (a real finding, a scope
correction, a blocker). Full detail should only appear in chat when it
requires the stakeholder's decision (an exception, an ambiguous
tradeoff) or when something went wrong. The durable record already
exists elsewhere and doesn't need duplicating in conversation:

1. Git commits, one per ticket, referencing the ticket ID.
2. The ticket files themselves, moved to `tickets/done/` with
   acceptance criteria checked and completion notes recorded.
3. For documentation tickets specifically, the actual doc files
   written to the repo (e.g. `src/aprilcam/*/DESIGN.md`).

## Root Cause

**Missing instruction.** Nothing in the team-lead role, the
`execute-sprint` skill, or the Exception Routing rules says how much of
a completed subagent's report to relay to the stakeholder — the
default behavior (relay the agent's own final report near-verbatim)
was never actually specified as correct, it just happened by default
because subagent reports are detailed by design (they're written to be
verifiable, not to be chat-legible at 11x volume). There was no
existing guidance distinguishing "detail that belongs in the durable
record" from "detail that belongs in the conversation."

## Proposed Fix

Adopt, as a standing team-lead behavior: after any ticket/programmer-agent
completion during sprint execution, post a one-line status update
(ticket id + outcome + notable reason if any) rather than the
subagent's full report. Reserve full detail for: exceptions requiring
stakeholder input, a request for it, or a genuine one-time summary at
sprint close. This is now recorded as a `[[feedback]]`-type memory
for cross-session recall; no code or process-artifact change is needed
beyond that, since the underlying reports themselves are correctly
detailed for their actual purpose (ticket files, commits) — the fix is
purely about what gets relayed into chat.
