# Legacy Desktop rollover (disabled)

Do not execute the transaction below. It is retained only as migration history.
It predates the explicit typed-Goal authority boundary and can synthesize a Goal
from stale task state. The normative contract is
`references/durable-orchestrator.md`; without its validated controller, use
native Codex and do not auto-roll over.

# Historical design

Read this file only after `CONTEXT_ROLLOVER_REQUIRED` in a parent/user-owned
Codex Desktop task. The workspace registry records the user's explicit
2026-08-13 authorization to perform future rollovers without asking for
`/new` or a separate “continue” message.

## Transaction

1. Verify the sentinel names the current project and source task. If the
   objective is complete, validate, audit, and `finish`; create no empty task.
2. Checkpoint the unfinished objective with CAS, run the project validation and
   context audit, and remove the candidate after success. Do not run a resume
   preflight against the old task to build the handoff.
3. Search for the first-party Codex task tools. Call `list_projects`, then
   choose the exact saved project path when present; otherwise choose the
   `__PROJECT_ROOT_WIN__` saved project. Continue in the same local checkout so ignored
   reverse-engineering artifacts and current uncommitted state remain visible.
4. Call `create_thread` exactly once with a deterministic title containing the
   registered project name and the source-task prefix. Use a project target
   with `environment: {type: "local"}`. Never use `fork_thread`, because a fork
   carries the aging transcript into the target.
5. Put the complete bounded handoff in `create_thread.prompt`; that prompt
   starts the target turn, so do not wait for the user or send “continue” later.
   Include only: registered project id/path, source task id, state SHA, the
   explicit-continuation directive below, and no raw logs or secrets. The target
   must use the same turn to begin concrete project work; a handoff/status reply
   is not a successful continuation.
6. Require a returned target `threadId` different from the source. If the call
   result is uncertain, inspect tasks for the deterministic title before any
   retry. Do not create a duplicate target. Creation confirmation alone does
   not authorize finishing the source Guardian session.
7. Follow the target with `wait_threads`, using the returned target id and cursor,
   until it has created its no-budget worker Goal and begun concrete project work.
   Validate the private target runtime directly: registered project id, returned
   task id, checkpoint state SHA, exact `source_task_id`, source audit lineage,
   zero substantive iterations, and fresh telemetry. Do not require or emit an
   assistant acknowledgement. An up-to-date cursor suppresses duplicate text; do
   not repeatedly read its transcript.
8. A target error, attention request, blocked preflight, mismatched/missing
   runtime receipt, progress-only final, or timeout before concrete work does not
   authorize source finish. Keep the source runtime active, inspect/recover the
   existing target before any retry, and never create a duplicate or ask the user
   for `/new`/“continue”.
9. Only after the validated target receipt, target Goal, and concrete work start, run
   `finish --task-id <source> --replaced-by <target>`. Leave the old task as a visible audit trail
   unless the user separately asks to archive it. Then complete only the source
   task's Goal and navigate the app to the target
   so the visible task follows the work. Emit no user-facing rollover, task-id,
   checkpoint, acknowledgement, success, or “work is starting” message; handoff
   plumbing is not the user's deliverable.
10. The target's Goal remains active while its own Guardian runtime exists. The
    separately trusted synchronous Stop boundary guard converts an unfinished
    turn stop (including a paused Goal lease) into an automatic continuation
    prompt, without another user message. Only validation + task-bound audit +
    plain `finish` permits a final response. A real user decision must use the
    user-input tool; a plaintext marker or progress-only final cannot pause work.

## Initial prompt contract

Use this meaning, filled with the verified values:

```text
[CONTROLLER-CREATED AUTOMATIC ROLLOVER]

This fresh Codex task continues the user's existing objective; it is not a new
objective. The user explicitly authorized automatic fresh-task rollover for
__PROJECT_ROOT_WIN__ on 2026-08-13 and does not need to type /new or “continue”.

Registered project: <id> at <path>
Source task: <old-id>
Checkpoint state SHA-256: <sha>

First call get_goal and create a no-token-budget generic worker lease if absent.
Run $context-guardian preflight once for <id> with `--resume --replaces-task
<old-id>`, using this task's own CODEX_THREAD_ID. Then inspect ACTIVE_STATE.md
and direct working-tree evidence. Confirm privately that the bounded bundle
names this task, its runtime is bound to the exact source audit lineage, its
telemetry is fresh, and its state SHA is <sha>. Do not mention the rollover,
task ids, checkpoint, or handoff to the user. Immediately execute the first
verified unfinished action with tools and speak only as the continuing project
worker. Keep taking the next action until the underlying objective is genuinely
complete. Do not replay completed mutations. A new task, checkpoint, or handoff
is never completion. Keep future rollovers silent and automatic.
```

If first-party task creation is not exposed, a running App Server continuous
client executes the equivalent transaction. It checkpoints, starts the fresh
thread with `sessionStartSource=clear`, clears the terminal conversation view,
dispatches the handoff with `turn/start`, and keeps starting concrete continuation
turns while the target Guardian runtime exists. The absence of either controller
is an implementation error, not a reason to ask the user to perform `/new`,
`/clear`, or “continue” manually.

Codex Desktop cannot erase text already streamed by a completed turn and shows
fresh tasks in the sidebar. Do not claim that its Goal-driven path is visually
identical to an in-place `/clear`. The App Server continuous client is the path
that deterministically buffers automatic turns and exposes only genuine terminal
completion or a concrete user-only decision question.
