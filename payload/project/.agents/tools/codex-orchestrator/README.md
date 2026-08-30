# Codex Durable Orchestrator

This is the isolated replacement candidate for the legacy
`codex-continuous` scheduler. It deliberately keeps the Codex agent/runtime and
replaces only the home-grown continuation loop with a Temporal workflow.

It is **fail closed** and is not wired to the project launchers until the crash,
preemption, budget, and reconciliation tests pass.

## Authority boundary

- A Goal is created only by the typed `start_goal` command. Ordinary chat text,
  quoted text, repository state, and assistant output cannot create or replace a
  Goal.
- Every real user message first advances `intent_epoch`, pauses automation, and
  invalidates the in-flight operation. Automation resumes only after an explicit
  `resume_goal` command.
- Temporal owns scheduling, lifetime budgets, ordering, and audit history.
- Codex app-server owns threads, turns, tools, and usage evidence.
- Native Codex Goal state is only a paused UI projection. It is never set active
  by the orchestrator because active native Goals can schedule work outside the
  Temporal budget gate.

## Non-idempotent app-server calls

`thread/start`, `thread/fork`, and `turn/start` have no protocol-level
idempotency guarantee. They are never configured with automatic activity
retries. An uncertain response moves the workflow to `reconcile_required`; the
adapter must read back thread/turn history using the stable operation ID before
it may dispatch anything else.

## Reviewed local runtime

The reviewed pins are Temporal CLI `1.8.2` (Windows amd64 archive SHA-256
`72e02498fa7849657c369377f7de69a8709b3d2183b6f2749f6c8bd54a984501`),
Temporal Python SDK `1.30.0`, and `openai-codex` `0.144.4`.

There is one loopback-only Temporal development server per workspace, backed
by a persistent SQLite file. Each project gets its own stable Workflow ID,
Task Queue, Worker PID record, logs, and interprocess command lock. This avoids
both port collisions and a Codex adapter consuming another project's Activity.

```text
<workspace>\.workspace\tools\codex-orchestrator\
  .venv\
  bin\temporal.exe
  temporal\state.db
  runtime-manifest.json
  pids\temporal-server.json
  pids\worker-<project-key>.json
  locks\commands-<project-key>.lock
  logs\worker-<project-key>.*.log
```

Bootstrap provisions and verifies files only; it never starts a background
process. `start-local.ps1` has finite readiness limits and records exact PIDs.
`stop-local.ps1` stops only validated recorded PIDs. The shared server requires
an explicit `-StopServer` and is not stopped while another Worker record exists.

All CLI mutations (`start-goal`, `message`, `pause`, `resume`, and `cancel`)
require a caller-stable `--command-id`. A project-scoped OS lock serializes the
Workflow sequence query plus Update. Lock contention and RPC timeout return a
non-zero retryable result; neither is treated as accepted work. `clear` remains
only as a compatibility alias for `cancel`.

## Verification gates

`scripts/verify.ps1` validates the control-plane installation without starting
services. `-Live` performs one bounded namespace request and validates recorded
process identity. It does not poll indefinitely.

The launcher replacement gate is intentionally **blocked**. No user-visible
assistant output sink or Codex app message-ingress bridge exists yet. Goal
objectives and manual messages are also Temporal payloads, and no end-to-end
`PayloadCodec` encryption/key provider is configured. Therefore
`launcher_replacement_ready` is always `false`, `-RequireLauncherReady` exits
non-zero, and no existing launcher may be repointed to this candidate.

```powershell
.\scripts\bootstrap.ps1 -WorkspaceRoot __PROJECT_ROOT_WIN__ -PythonExe <python-3.11.exe>
.\scripts\verify.ps1 -WorkspaceRoot __PROJECT_ROOT_WIN__ -ProjectRoot __PROJECT_ROOT_WIN__
.\scripts\start-local.ps1 -WorkspaceRoot __PROJECT_ROOT_WIN__ -ProjectRoot __PROJECT_ROOT_WIN__
.\scripts\verify.ps1 -WorkspaceRoot __PROJECT_ROOT_WIN__ -ProjectRoot __PROJECT_ROOT_WIN__ -Live
.\scripts\stop-local.ps1 -WorkspaceRoot __PROJECT_ROOT_WIN__ -ProjectRoot __PROJECT_ROOT_WIN__ -StopServer
```

## Obsolete draft runtime note (superseded above)

Source lives here. Rebuildable runtime files live outside Git under:

```text
__PROJECT_ROOT_WIN__\.workspace\tools\codex-orchestrator\
  .venv\
  bin\temporal.exe
  temporal\state.db
  logs\
```

The local Temporal server must bind to loopback and use `--db-filename`; the
default in-memory development server is intentionally rejected.

## Obsolete draft status (superseded above)

This directory is a migration candidate. Do not repoint `CodexContinuousMode.cmd`
until `scripts/verify.ps1` passes the full gate.
