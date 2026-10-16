# HITL PR Review Lab Solution Design

## Goal

Complete all five lab exercises so the repository contains the required
confidence-based PR review agent, human approval and escalation flows, SQLite
audit trail, and Streamlit interface while retaining the assignment's module
layout and callable interfaces.

## Scope and constraints

- Implement the TODOs in `exercises/exercise_1_confidence.py` through
  `exercises/exercise_4_audit.py` and `app.py`.
- Keep `common/`, `audit/`, the existing `ReviewState`/`PRAnalysis`/`AuditEntry`
  contracts, and the thresholds in `common/schemas.py` unchanged.
- Use `AUTO_APPROVE_THRESHOLD` (0.73) and `ESCALATE_THRESHOLD` (0.58), never
  the stale literals in exercise comments.
- Keep `streamlit` as a declared dependency; it is already present in
  `pyproject.toml`.
- Preserve the repo's top-level `app.py`, `common/`, `audit/`, and
  `exercises/` layout so the documented module commands continue to work.
- Preserve the pre-existing local `uv.lock` change and do not include it in
  solution commits.

## Exercise behavior

1. Exercise 1 calls the configured LLM with `with_structured_output(PRAnalysis)`,
   stores the result, routes by the shared thresholds, and wires all three
   terminal nodes.
2. Exercise 2 pauses only in `node_human_approval`, with the documented
   approval payload. `MemorySaver` and a stable `thread_id` support a `while`
   resume loop. Posting remains in `node_commit`, which posts only on approve.
3. Exercise 3 asks the LLM for 2–4 context-specific escalation questions below
   the shared escalation threshold, returns the documented escalation payload,
   and synthesizes a refined `PRAnalysis` from the original diff, initial
   analysis, and reviewer answers. The existing graph contract continues from
   `escalate` to `synthesize` to `commit`.
4. Exercise 4 writes a structured `AuditEntry` at every specified node. It
   records elapsed time, derives risk with `risk_level_for`, uses only allowed
   decision values, and records reviewer identity only on human-response
   events. Approval and escalation each record an event before and after
   `interrupt()`; the asynchronous SQLite checkpointer and resume loop remain
   intact.
5. Exercise 5 imports the Exercise 4 graph, invokes/resumes it with the same
   thread, renders approval buttons and escalation question forms with the
   exact resume payload shapes, and populates recent sessions from
   `audit_events`. Final results show useful status; auto approval includes
   confidence, summary, and a link to the PR.

## Flow and side effects

The graph's three confidence branches are auto-approve, human approval, and
escalation. Human approval and escalation are represented by LangGraph
interrupts so either terminal UI can resume them. GitHub comments are posted
only by the existing commit nodes after an approval or completed escalation
flow; no external PR is modified as part of implementation or local checks.

The documented graph contract in Exercises 3 and 4 is authoritative for the
current skeleton: after escalation answers, the graph synthesizes and commits
the refined review. The README's separate phrase about showing the refined
review for a second final confirmation does not match that graph and is not
added as a new interrupt in this solution.

## Verification

Check that no assignment TODOs or `NotImplementedError` remain in the target
files, all modules compile/import, and local behavior matches the documented
payloads and routing boundaries. Live LLM/GitHub demo commands require the
student's own API credentials and may post public comments; they are left for
the student to run interactively. `uv.lock` is not regenerated or committed.
