# Codex CLI Portable Setup Kit

> Reproduce a tested Codex CLI working environment on another Windows computer
> or in another repository, with integrity checks, transactional backups,
> verification, and rollback.

This community-maintained setup kit packages portable Codex configuration,
project instructions, hooks, local automation tools, and reusable skills into a
single Windows installer. It is designed for developers who have already tuned
a Codex workflow and want a repeatable way to install the same **portable
behavior** elsewhere without copying sessions, authentication, caches, or other
machine-bound state.

This repository is not Codex itself and is not an official OpenAI product.

![Codex CLI Portable Setup Kit social preview](docs/assets/social-preview.png)

## What this tool does

The kit performs five jobs:

1. **Validates the package before changing the computer.** Every tracked payload
   file is checked against `MANIFEST.sha256`.
2. **Backs up every destination it will replace.** Existing user and project
   files are copied to a timestamped backup directory.
3. **Installs a portable Codex environment.** It places configuration,
   instructions, skills, hooks, and project automation in their target locations.
4. **Adapts paths to the target machine.** Project-root and user-home placeholders
   are resolved during installation, and the project is added as a trusted Codex
   configuration layer.
5. **Produces a rollback receipt.** A failed installation rolls back
   automatically; a successful installation can be reverted later with
   `rollback.ps1` or `ROLLBACK.sh`.

The result is a reproducible Codex workspace with project-aware instructions,
context checkpoints, Git safeguards, reusable skills, and optional plugin setup.

## Who this is for

Use this kit when you want to:

- bootstrap a new Windows development computer with the same Codex conventions;
- give a repository the same project instructions, hooks, and local agent tools;
- move a Codex workflow without moving login credentials or conversation data;
- test a standardized team setup in an isolated project;
- keep installation and rollback evidence instead of copying dotfiles manually.

It is not a cloud sync service, credential migrator, package manager replacement,
or substitute for reviewing the configuration you install.

## What gets installed

| Scope | Destination | Installed content | Purpose |
|---|---|---|---|
| Codex user | `%CODEX_HOME%` or `~/.codex` | `config.toml`, `AGENTS.md`, portable agent instructions, starter rules, skills | Shared model, UI, instruction, tool, and skill defaults |
| Agent user | `~/.agents` | User-level skills | Makes selected reusable skills available outside one repository |
| Project | The explicit `-ProjectRoot` | `AGENTS.md`, `.codex`, `.agents`, `.gitignore`, context workflow | Adds repository-specific instructions, hooks, context tools, and Git guard |
| Git repository | Local Git config | `core.hooksPath=.agents/git-hooks` | Activates the packaged pre-commit guard for that repository |
| Codex CLI | Global npm installation, only if needed | Compatibility-pinned `@openai/codex` | Makes Codex available when it is missing |
| Optional plugins | Current Codex installation | Browser, Visualize, and Sites when available | Extends the installed environment without making plugin failure fatal |

### Included project automation

- **PostCompact checkpoint hook** — records a bounded checkpoint after Codex
  compacts a conversation and returns a clear handoff message.
- **Context Guardian** — validates and manages bounded recovery state for long
  tasks without treating stale state as a new request.
- **Codex Continuous** — a local continuous CLI client with model selection,
  interruption handling, checkpoints, and completion classification.
- **Codex Orchestrator** — local orchestration primitives and verification tools
  for durable or interactive Codex workflows.
- **Git guard and pre-commit hook** — protects the root control-plane repository
  from accidental runtime, private, and generated artifacts.
- **Reusable skills** — Cloudflare, Workers, Agents SDK, Durable Objects,
  sandboxing, Turnstile, Wrangler, email, and web-performance workflows.

## How installation works

```text
ZIP or clone
    |
    v
ProjectRoot validation
    |
    v
SHA-256 manifest verification
    |
    v
Timestamped backup + operation journal
    |
    v
User files -> skills -> project files -> path substitution
    |
    v
Project trust + Git hooksPath + optional plugins
    |
    v
receipt.json + last-receipt.txt
```

`install.ps1` records each file operation before replacing its destination. If a
later step fails, the operation list is replayed in reverse and the previous Git
hook binding is restored. On success, the same operation list is saved in the
receipt for a later explicit rollback.

See [Architecture](docs/ARCHITECTURE.md) for the component and data-flow details.

## Quick start

### Prerequisites

- Windows 10 or Windows 11
- Windows PowerShell 5.1 or PowerShell 7
- Git, if the target is a Git repository
- Node.js/npm only when Codex CLI is not already installed
- A target project directory that already exists

### Recommended: explicit PowerShell installation

1. Download the latest release ZIP and extract the entire archive.
2. Open PowerShell in the extracted directory.
3. Run:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\verify.ps1
.\install.ps1 -ProjectRoot 'D:\work\your-project'
```

The installer refuses to use its own package directory as the project root.

### Explorer: double-click installation

Extract the entire ZIP, then double-click `install.cmd` or `START-HERE.bat`.
Enter or drag the target project directory into the PowerShell prompt. The
launcher writes the complete output to `install-last.log` and keeps the window
open so the exit result remains visible.

### Unattended installation

```powershell
.\install.ps1 `
  -ProjectRoot 'D:\work\your-project' `
  -SkipPlugins
```

Do not pass the project path to `install.cmd`; use `install.ps1` for scripted
installation.

## Installer options

| Option | Default | Description |
|---|---|---|
| `-ProjectRoot` | Safely detected only from clear project markers | Target repository or project directory |
| `-CodexHome` | `%CODEX_HOME%` or `~/.codex` | Target Codex user directory |
| `-AgentsHome` | `~/.agents` | Target user-agent directory |
| `-ReceiptPath` | Timestamped backup directory | Explicit receipt destination for automation/tests |
| `-CodexVersion` | `0.147.0` | Codex CLI version to install when required |
| `-UpgradeCodex` | Off | Reinstalls Codex at `-CodexVersion` even when Codex exists |
| `-SkipPlugins` | Off | Skips optional Browser, Visualize, and Sites plugin attempts |
| `-SkipCodexCheck` | Off | Skips Codex discovery/install; intended for controlled fixtures |

The bundled `codex-continuous` compatibility matrix was validated with
`openai-codex` SDK `0.144.4` and Codex CLI `0.146.0`/`0.147.0`. A newer CLI can
be selected explicitly after validating that pair:

```powershell
.\install.ps1 `
  -ProjectRoot 'D:\work\your-project' `
  -CodexVersion '0.151.0' `
  -UpgradeCodex
```

## Verification

Verify the extracted package before installation:

```powershell
.\verify.ps1
```

Verify an installed project and user environment:

```powershell
.\verify.ps1 -Installed -ProjectRoot 'D:\work\your-project'
```

Or use the double-click-friendly wrapper:

```text
verify.cmd "D:\work\your-project"
```

For development and release checks:

```powershell
.\tests\Test-PublicRelease.ps1
.\tests\Test-Rollback.ps1
```

## Rollback

Rollback uses the latest receipt pointer by default:

```powershell
.\rollback.ps1
```

You can target a specific receipt:

```powershell
.\rollback.ps1 -Receipt 'C:\path\to\receipt.json'
```

From Git Bash:

```bash
./ROLLBACK.sh
```

Rollback removes files added by the installer, restores files that existed
before installation, and restores or unsets the repository's previous
`core.hooksPath` value.

## Security model

The public edition defaults to:

```toml
approval_policy = "on-request"
sandbox_mode = "workspace-write"
```

The public starter rules file contains no pre-approved command prefixes. Review
new rules before accepting them, and test a rule with `codex execpolicy check`.
The installer verifies its manifest before writing, blocks path traversal in the
manifest, records reversible operations, and excludes machine-bound runtime data.

The kit does **not** include or migrate:

- Codex authentication or OAuth data;
- sessions, history, logs, caches, or SQLite databases;
- cookies, access tokens, API keys, or private keys;
- browser runtime data or desktop binary paths;
- context runtime state from the source computer.

Read [SECURITY.md](SECURITY.md) before publishing a customized fork.

## Customizing the payload

1. Edit files under `payload/`.
2. Keep user-facing documentation and messages in English. Encode non-English
   classifier fixtures with Unicode escapes when multilingual behavior is tested.
3. Regenerate `PAYLOAD_INDEX.json` and `MANIFEST.sha256`:

```powershell
.\scripts\Update-PackageMetadata.ps1
```

4. Run the complete verification command:

```powershell
.\scripts\Test-All.ps1
```

5. Test installation and rollback in a disposable fixture before publishing.

## Repository layout

```text
.
|-- install.ps1                 Transactional installer
|-- launcher.ps1                Interactive PowerShell launcher
|-- verify.ps1                  Package and installed-state verification
|-- rollback.ps1                Receipt-based rollback engine
|-- ROLLBACK.sh                 Git Bash rollback entry point
|-- payload/
|   |-- codex-home/             Codex user configuration, instructions, rules, skills
|   |-- agents-home/            User-level agent skills
|   `-- project/                Project instructions, hooks, tools, and workflows
|-- tests/                      Installer and release tests
|-- scripts/                    Metadata and test automation
`-- docs/                       Architecture and promotion playbook
```

## Official Codex documentation

- [Codex CLI](https://learn.chatgpt.com/docs/codex/cli)
- [Configuration basics](https://learn.chatgpt.com/docs/config-file/config-basic)
- [Custom instructions with AGENTS.md](https://learn.chatgpt.com/docs/agent-configuration/agents-md)
- [Rules](https://learn.chatgpt.com/docs/agent-configuration/rules)
- [Hooks](https://learn.chatgpt.com/docs/hooks)
- [Build skills](https://learn.chatgpt.com/docs/build-skills)
- [Build plugins](https://learn.chatgpt.com/docs/build-plugins)

## Contributing and promotion

- Read [CONTRIBUTING.md](CONTRIBUTING.md) to propose code or payload changes.
- Use the ready-to-post launch copy and 48-hour distribution plan in
  [docs/PROMOTION_PLAYBOOK.md](docs/PROMOTION_PLAYBOOK.md).
- See [CHANGELOG.md](CHANGELOG.md) for release history.

## License

MIT — see [LICENSE](LICENSE).
