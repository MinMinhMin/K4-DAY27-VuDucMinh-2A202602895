# HITL PR Review Lab Solution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the five lab exercises with stable confidence routing, resumable human interaction, structured audit events, and a working Streamlit UI.

**Architecture:** Keep the staged exercise modules as the public lab interfaces. Exercise 4 remains the canonical durable graph used by Streamlit; all routing reads the shared thresholds, and the UI resumes graph interrupts with the payload shape expected by each node.

**Tech Stack:** Python 3.11+, LangGraph, Pydantic, aiosqlite, Streamlit, Rich, unittest.

**Spec:** `docs/superpowers/specs/2026-10-16-hitl-lab-solution-design.md`

## Global Constraints

- Keep `common/`, `audit/`, and the existing `ReviewState`/`PRAnalysis`/`AuditEntry` contracts unchanged.
- Use `AUTO_APPROVE_THRESHOLD` (0.73) and `ESCALATE_THRESHOLD` (0.58); do not copy stale literals from exercise comments.
- Preserve top-level `app.py`, `common/`, `audit/`, and `exercises/` paths.
- Keep `streamlit` declared; it is already in `pyproject.toml`.
- Do not stage or regenerate the pre-existing local `uv.lock` change.
- Tests must stub the LLM and GitHub calls; local verification must not post comments to public PRs.
- Keep the total commit count at or below 7, including the existing worktree-ignore and spec commits.

## Review Focus

- Confidence exactly at 0.58 routes to human approval, while exactly 0.73 routes to auto approval; Task 1 pins the boundary values in `test_route_confidence_boundaries`.
- A resumed approval node does not post before the reviewer responds, and reject/edit do not post; Task 1 covers resume and `node_commit` with a mocked poster.
- An empty LLM question list still produces a usable escalation interrupt, and Q&A is preserved for synthesis; Task 2 covers fallback and synthesis prompts.
- Audit rows use only the allowed decision values and identify a reviewer only after response; Task 2 checks event fields before and after interrupts.
- Streamlit resume uses the same thread and returns exact dictionaries for both interrupt kinds; selecting a recent row rehydrates its saved interrupt/final state. Task 3 tests these with stubbed Streamlit/checkpointer dependencies.

---

### Task 1: Confidence routing and human approval

**Files:**
- Modify: `exercises/exercise_1_confidence.py`
- Modify: `exercises/exercise_2_hitl.py`
- Create: `tests/__init__.py`
- Create: `tests/test_exercises_1_2.py`

**Interfaces:**
- Consumes: `PRAnalysis`, `ReviewState`, and shared threshold constants from `common.schemas`.
- Produces: `build_graph()` in Exercise 1; Exercise 2 approval interrupts with `kind`, `confidence`, `confidence_reasoning`, `summary`, serialized `comments`, and a 2000-character `diff_preview`; resume values are `{"choice": ..., "feedback": ...}`.

- [ ] **Step 1: Write tests** for confidence boundaries, structured analyzer output, approval payload shape, MemorySaver compilation, and approve/reject/edit behavior with a mocked comment poster.
- [ ] **Step 2: Run tests and confirm they fail** because the Exercise 1 TODOs and Exercise 2 interrupt/resume flow are incomplete.
- [ ] **Step 3: Implement Exercise 1** using structured `PRAnalysis`, the shared thresholds, the six required nodes, and the three conditional terminal branches.
- [ ] **Step 4: Implement Exercise 2** with the documented interrupt payload, `MemorySaver`, a stable `thread_id`, a `while` resume loop, and posting isolated in `node_commit`.
- [ ] **Step 5: Run Task 1 tests** and confirm routing, payloads, resume behavior, and no-post rejection pass.
- [ ] **Step 6: Commit** as `feat: complete confidence and approval exercises`.

### Task 2: Escalation and durable audit trail

**Files:**
- Modify: `exercises/exercise_3_escalation.py`
- Modify: `exercises/exercise_4_audit.py`
- Create: `tests/test_exercises_3_4.py`

**Interfaces:**
- Consumes: Exercise 3/4 graph state and node contracts from the spec; `audit(state, entry)` persists an `AuditEntry` through `write_audit_event`.
- Produces: escalation interrupt payload with `pr_url`, `confidence`, `confidence_reasoning`, `summary`, `risk_factors`, and `questions`; synthesis returns a refined `PRAnalysis`; Exercise 4 emits the specified audit rows and retains `AsyncSqliteSaver` resume behavior.

- [ ] **Step 1: Write tests** for context-specific escalation prompt instructions, payload fallback, synthesis inclusion of initial analysis/diff/Q&A, and audit row values at each node.
- [ ] **Step 2: Run tests and confirm they fail** on the current escalation and audit `NotImplementedError`/TODO behavior.
- [ ] **Step 3: Implement Exercise 3** with the shared escalation threshold in its prompt, interrupt/resume answer handling, refined structured analysis, and the `escalate → synthesize → commit` path.
- [ ] **Step 4: Implement Exercise 4** with awaited audit writes, elapsed-time measurements, risk derivation, allowed decisions, reviewer identity on response entries, and pre/post interrupt rows.
- [ ] **Step 5: Run Task 2 tests** using a temporary SQLite database and stubbed LLM/GitHub calls; confirm the audit replay query sees the expected sequence.
- [ ] **Step 6: Commit** as `feat: complete escalation and audit exercises`.

### Task 3: Streamlit integration

**Files:**
- Modify: `app.py`
- Create: `tests/test_app.py`

**Interfaces:**
- Consumes: `build_graph(checkpointer)` and the `approval_request`/`escalation` payloads from Exercise 4.
- Produces: `run_graph(pr_url, thread_id, resume_value=None)` that starts or resumes the same thread; UI renderers return the exact dictionaries expected by their interrupt nodes; sidebar lists recent audit sessions.

- [ ] **Step 1: Write tests** for graph invocation vs resume, approval button return values, escalation answer mapping, recent-session query fields and row selection, checkpoint rehydration, and auto-result confidence/summary/PR link.
- [ ] **Step 2: Run tests and confirm they fail** against the current incomplete Streamlit skeleton.
- [ ] **Step 3: Import the Exercise 4 graph** and complete `run_graph` using the persistent SQLite checkpointer and stable thread configuration.
- [ ] **Step 4: Complete approval and escalation renderers** so buttons/forms return the exact resume payload shapes.
- [ ] **Step 5: Populate selectable recent sessions from `audit_events`**; selecting a row restores its `thread_id`, `pr_url`, and saved interrupt/final state. Show final result details, including the auto-approve summary and PR link.
- [ ] **Step 6: Run Task 3 tests** with mocked Streamlit/checkpointer dependencies and confirm no live PR calls are made.
- [ ] **Step 7: Commit** as `feat: complete Streamlit review interface`.

### Final verification

- [ ] Run all unit tests with `uv run --offline python -m unittest discover -s tests -v`.
- [ ] Compile all project Python modules with `uv run --offline python -m compileall -q app.py exercises common audit tests`.
- [ ] Confirm no assignment `NotImplementedError` or unfinished TODO remains in the five target files.
- [ ] Review the final diff and Git status; confirm `uv.lock`, `.env`, and database files are not in the solution commits.
- [ ] If a final-review fix is needed, keep it within one additional commit (maximum seven total); do not run commands that post public PR comments.
