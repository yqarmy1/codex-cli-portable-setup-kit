# __PROJECT_ROOT_WIN__ — Shared project rules

## Scope and evidence

- The current user request is authoritative. Do not create, resume, or rewrite a
  task from old state, summaries, or history.
- Apply the root rules and the deepest applicable child `AGENTS.md`. Prefer
  source code, tests, and runtime evidence over stale documentation. Query
  history, logs, archives, and dumps only for a named problem.

## Execution discipline

- Do the work before describing the work. Treat actionable requests as tasks to
  execute with tools, not as prompts for a tutorial about what the user could do.
- Continue from inspection through editing and verification. Do not stop at a
  plan or progress report while a concrete next action is available.
- Investigate the repository before asking for information that files, tests,
  history, or a reversible default can answer.
- Keep communication task-relevant. Omit generic moral lectures, repeated caveats, and meta-commentary
  when they do not change execution. State real constraints briefly, explain
  their concrete impact, and continue with the highest-value available action.
- Complete authorized, reversible, local changes autonomously. Confirm external
  transactions or destructive account operations when the current request does
  not already authorize them.
- Keep secrets, credentials, tokens, cookies, private data, databases, and raw
  logs out of state files, Git, tests, and responses.
- Before editing, inspect call sites, tests, the worktree, and generated/private
  boundaries. Preserve all unrelated changes.
- Stop only processes started by the current task and recorded by PID or port.
  Use explicit paths; do not perform broad process-name cleanup.
- Limit tool output to the first 80 lines or 8 KiB, whichever comes first. Narrow
  paths and conditions before wide searches.

## Context lifecycle

- `context_rollover_required` means: checkpoint the bounded project state, then
  continue only in a validated fresh thread. Never infer a new objective from
  stale state.
- When `context-guardian` is explicitly used, load only the registered project's
  bounded `ACTIVE_STATE.md`. The current user request remains authoritative, and
  recovery state must not contain secrets or raw logs.

## Version control and delivery

- Child projects use their own Git repositories. The root repository tracks only
  the shared control plane. Before staging, verify private, runtime, generated,
  and large rebuildable-artifact boundaries.
- Run tests and builds proportional to the change. Report modified files,
  verification evidence, environment-dependent behavior not exercised, and the
  state of temporary or staging data. Keep the final report concise unless the
  user asks for a detailed explanation.
