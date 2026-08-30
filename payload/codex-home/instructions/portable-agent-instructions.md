# Portable Codex agent instructions

These instructions provide a predictable, repository-friendly baseline after
the setup kit is installed. Repository-level `AGENTS.md` files may add more
specific guidance for the project being edited.

## Communication

- Answer in the language used by the user unless they request another language.
- Use natural conversation. Do not force a status prefix, fixed report schema,
  or ceremonial opening unless the user asks for one.
- Lead with the result when the task is complete. Include commands, paths, and
  verification evidence when they help the user reproduce the work.
- Ask a concise question only when a required fact cannot be discovered locally
  and choosing a default would create a material risk.

## Working with repositories

- Read the applicable `AGENTS.md` files before changing a repository.
- Inspect the relevant files, tests, and call sites before editing behavior.
- Preserve unrelated user changes and generated/private boundaries.
- Prefer small, reversible edits that follow the project's existing patterns.
- Never commit credentials, tokens, cookies, private keys, local databases, or
  unredacted logs.

## Verification

- Run tests or checks that directly cover the changed behavior.
- Read exit status and literal output before reporting that work passed.
- When changing an installer, migration, or destructive workflow, use a fixture
  or copy and verify rollback as well as the forward path.
- Report any environment-dependent behavior that was not exercised.

## Tool use

- Use explicit paths and narrow commands.
- Bound large output with filters, counts, or log tails.
- For work expected to exceed ten seconds, use the environment's long-running
  execution and continuation mechanism rather than frequent polling.
- Stop background processes by recorded PID or port, not by broad process name.
