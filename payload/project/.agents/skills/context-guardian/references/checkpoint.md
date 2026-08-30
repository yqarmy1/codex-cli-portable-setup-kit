# Checkpoint protocol

Read this file only when a checkpoint is required. Do not load it during
preflight or routine pulse operations.

## Candidate

1. Build a complete candidate JSON outside `.context/history`.
2. Set `base_sha256` to the state SHA from the current preflight or the most
   recent successful `refresh`.
3. Replace the current objective, confirmed evidence, uncertainties, next
   actions, validation, and lookup pointers. Do not append chronology.
4. Preserve schema-required project invariants, including structured
   authorization. Do not copy stale task-local conclusions over fresh facts.
5. Exclude secrets, credentials, tokens, cookies, real account data, raw logs,
   and binary content.

## CAS and lock behavior

Checkpoint is compare-and-swap (CAS): never force an update when
`base_sha256` no longer matches. On state-SHA drift, inspect the bounded current
state, merge only verified evidence, run `refresh --project <project-id>` to
repair the runtime pointer without another bundle, rebuild the candidate with
the fresh SHA, and retry.

The command uses a crash-recoverable exclusive lock, atomic state/view writes,
and history archival. Do not delete, bypass, or replace the lock manually;
follow the precise lock error and retry only after its stated recovery path is
safe.

## After applying

Verify the generated active view and audit result. Delete the candidate from
its known staging path after success. Checkpoint resets only the manual
iteration counter. Lifetime transcript telemetry is preserved because a
checkpoint does not erase or replace model history. Only a successfully
created fresh thread starts a new telemetry budget.
