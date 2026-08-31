# Architecture

## Purpose

The product is an execution-first behavior profile for Codex. It combines model
settings, user instructions, project rules, tools, and continuation components
to favor real inspection, edits, tests, fixes, and concise verified results over
advice or progress-only responses.

The Windows installer is the delivery architecture. It separates that portable
behavior from machine-bound runtime state, applies the profile transactionally,
and records enough information to restore the pre-install state.

## Components

### Behavior stack

| Layer | Responsibility |
|---|---|
| `config.portable.toml` | Selects the model, high reasoning effort, low output verbosity, plugins, and documentation MCP |
| `portable-agent-instructions.md` | Defines the inspect, execute, verify, fix, and concise-report loop |
| Project `AGENTS.md` | Reinforces execution discipline and repository evidence rules |
| Skills and plugins | Supply reusable workflows and additional tools for doing the work directly |
| PostCompact and context components | Record bounded checkpoints and classify unfinished long-running work |
| Manifest, journal, and receipt | Verify and deliver the behavior stack with a reversible transaction |

### Entry points

| File | Responsibility |
|---|---|
| `install.cmd` / `START-HERE.bat` | Double-click entry point; launches `launcher.ps1` and preserves the exit result |
| `launcher.ps1` | Collects and normalizes the target project path, logs output, and invokes `install.ps1` without embedding user input in a nested command string |
| `install.ps1` | Validates, backs up, installs, adapts, records, and automatically rolls back on failure |
| `verify.ps1` / `verify.cmd` | Verifies the package manifest or an installed environment |
| `rollback.ps1` / `ROLLBACK.sh` | Replays a successful installation receipt in reverse |

### Payload layers

```text
payload/
|-- codex-home/
|   |-- config.portable.toml
|   |-- AGENTS.md
|   |-- instructions/
|   |-- rules/
|   `-- skills/
|-- agents-home/
|   `-- skills/
`-- project/
    |-- AGENTS.md
    |-- .codex/
    |-- .agents/
    |-- .github/
    `-- .gitignore
```

The Codex user layer applies broadly. The project layer applies only to the
selected repository and uses deeper instructions and configuration to specialize
behavior there.

## Forward transaction

### 1. Resolve inputs

The installer normalizes quoted paths, resolves absolute paths, verifies that the
target directory exists, and rejects the installer package itself or any of its
children as `ProjectRoot`.

Without `-ProjectRoot`, detection succeeds only when the package parent or current
directory contains a clear project marker such as `.git`, `.codex`, `AGENTS.md`,
`package.json`, `pyproject.toml`, `Cargo.toml`, `go.mod`, or `.gitignore`.

### 2. Verify package integrity

`MANIFEST.sha256` contains one SHA-256 entry per release file, excluding the
manifest itself. For every entry, the verifier:

1. validates the record format;
2. resolves the path beneath the package root;
3. rejects traversal outside that root;
4. verifies that the file exists;
5. compares the actual SHA-256 digest.

Installation stops before creating a backup or changing a destination when any
entry fails.

### 3. Create backup state

The installer creates:

```text
%CODEX_HOME%/migration-backups/codex-cli-portable-setup-kit/<timestamp>/
```

Before each destination is replaced, `Install-ExactPath` records:

- destination path;
- whether the destination existed;
- backup path when it existed.

The operation is appended before replacement so an error during the copy still
has a reversible record.

### 4. Apply user and project layers

The installer applies content in this order:

1. Codex user config, instructions, rules, and skills;
2. user-agent skills;
3. project `.agents`, `.codex`, `AGENTS.md`, `.gitignore`, and context workflow;
4. placeholder substitution;
5. trusted-project configuration;
6. repository-local Git hook binding;
7. optional plugin attempts.

The project root is inserted into project templates through
`__PROJECT_ROOT_WIN__` and `__PROJECT_ROOT_POSIX__`. Starter rules support user,
project, computer, and username placeholders for private forks, although the
public starter file contains no machine-specific rules.

### 5. Record success

The JSON receipt stores:

- schema and install timestamp;
- package, project, Codex home, and agent home paths;
- requested and detected Codex versions;
- manifest entry count;
- backup root;
- ordered file operations;
- prior and applied Git hook state;
- optional plugin results.

`last-receipt.txt` points to the latest successful receipt.

## Failure transaction

Any exception inside the install block starts `Restore-InstallState`:

1. restore or unset the previous `core.hooksPath` value;
2. reverse the operation list;
3. remove each installed destination;
4. copy its backup into place when it existed before installation;
5. aggregate restore errors instead of hiding them.

If restoration succeeds, the installer reports that all recorded changes were
rolled back. If restoration also fails, the error includes the backup root for
manual inspection.

## Explicit rollback

`rollback.ps1` loads a named receipt or the latest receipt pointer, reverses the
operation list, restores original destinations, and restores the previous Git
hook binding. `ROLLBACK.sh` is a Git Bash adapter that forwards its arguments to
the PowerShell rollback engine.

## Project runtime features

### PostCompact checkpoint

`.codex/hooks.json` binds `PostCompact` to the project-relative Node script. The
hook reads the event JSON, produces a bounded checkpoint, writes runtime state
beneath the project context directory, and returns Codex-compatible JSON.

### Context Guardian

The Context Guardian skill and scripts validate checkpoints, active state,
completion receipts, and rollover conditions. Current user intent remains the
source of truth; recovery state is bounded to the registered project.

### Codex Continuous

The continuous client wraps the Codex app-server/SDK flow with:

- persistent thread handling;
- interruption and cancellation logic;
- model and reasoning-effort selection;
- checkpoint and rewind support;
- interactive rendering;
- completion and continuation classification;
- compatibility checks between SDK and CLI versions.

### Codex Orchestrator

The orchestrator supplies local state and workflow primitives for interactive or
durable execution. Its test suites cover domain serialization, temporal workflow
behavior, command outboxes, local runtime control, and adapter boundaries.

## Data boundaries

Portable release data includes declarative config, instructions, rules, source
code, tests, and skill documentation. It excludes:

- authentication and OAuth state;
- sessions and conversation history;
- raw logs and runtime caches;
- cookies, tokens, credentials, private keys, and databases;
- machine-specific desktop binaries;
- source-computer context runtime state.

## Verification layers

| Layer | Command | Evidence |
|---|---|---|
| Package integrity | `.\verify.ps1` | Manifest count and `PACKAGE_VERIFY=PASS` |
| Public release | `.\tests\Test-PublicRelease.ps1` | English/public-data/default checks |
| Config | `.\tests\Test-Config.ps1` | Portable config fields and strict load when Codex is available |
| Install/rollback | `.\tests\Test-Rollback.ps1` | Fixture install, hook runtime, installed verification, content restoration, Git hook restoration |
| Bundled Python tools | `python -m unittest ...` | Context and orchestration behavior |
| Full release | `.\scripts\Test-All.ps1` | Aggregated result and exit status |
