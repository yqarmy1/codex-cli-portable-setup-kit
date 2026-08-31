<h1 align="center">Codex CLI Portable Setup Kit</h1>

<p align="center">
  <strong>Reproduce your Codex CLI setup anywhere.</strong><br>
  Config, instructions, skills, hooks, and project automation - installed with
  SHA-256 verification, transactional backups, and one-command rollback.
</p>

<p align="center">
  <a href="https://github.com/2akouwu/codex-cli-portable-setup-kit/actions/workflows/verify.yml"><img alt="Verification" src="https://github.com/2akouwu/codex-cli-portable-setup-kit/actions/workflows/verify.yml/badge.svg"></a>
  <a href="https://github.com/2akouwu/codex-cli-portable-setup-kit/releases/latest"><img alt="Latest release" src="https://img.shields.io/github/v/release/2akouwu/codex-cli-portable-setup-kit?display_name=tag&sort=semver"></a>
  <a href="LICENSE"><img alt="MIT license" src="https://img.shields.io/github/license/2akouwu/codex-cli-portable-setup-kit"></a>
  <img alt="Windows 10 and 11" src="https://img.shields.io/badge/Windows-10%20%7C%2011-0078D4?logo=windows">
  <img alt="PowerShell 5.1 and 7" src="https://img.shields.io/badge/PowerShell-5.1%20%7C%207-5391FE?logo=powershell">
</p>

<p align="center">
  <a href="#quick-start">Quick start</a> |
  <a href="#highlights">Highlights</a> |
  <a href="#how-it-works">How it works</a> |
  <a href="#security-model">Security</a> |
  <a href="docs/PROMOTION_PLAYBOOK.md">Launch playbook</a>
</p>

![Codex CLI Portable Setup Kit preview](docs/assets/social-preview.png)

> [!NOTE]
> This is a community-maintained setup kit. It is not Codex itself and is not an
> official OpenAI product.

## Highlights

- **One portable package.** Move Codex configuration, `AGENTS.md`, skills,
  hooks, local tools, and project conventions together.
- **Verified before write.** Every packaged payload file is checked against
  `MANIFEST.sha256` before the installer changes a destination.
- **Transactional by default.** Existing files are backed up and each operation
  is journaled before replacement.
- **Rollback is a first-class command.** Restore replaced files, remove files
  added by the installer, and recover the previous Git hook binding.
- **Machine paths adapt automatically.** Portable placeholders become the
  selected project root and user home during installation.
- **Public-ready defaults.** The package excludes credentials and runtime data,
  uses `on-request` approval with `workspace-write`, and ships no silent allow
  rules.
- **Natural interaction.** The English agent instructions do not force a
  repetitive `Current / Result / Next` prefix into normal responses.
- **Tested as a release artifact.** Package integrity, public-release rules,
  Python components, installed-state checks, and rollback fixtures run as one
  verification suite.

## Quick start

### 1. Download and extract

Download the [latest release ZIP](https://github.com/2akouwu/codex-cli-portable-setup-kit/releases/latest)
and extract the complete directory. Do not run individual files from inside the
ZIP viewer.

### 2. Verify the package

Open PowerShell in the extracted directory:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\verify.ps1
```

Expected result:

```text
PACKAGE_VERIFY=PASS files=472
```

### 3. Install into a project

```powershell
.\install.ps1 -ProjectRoot 'D:\work\your-project'
```

The target directory must already exist. The installer refuses to use its own
package directory as the project root.

Prefer a guided flow? Extract the ZIP and double-click `START-HERE.bat` or
`install.cmd`, then enter or drag the target project directory into the prompt.

### Unattended installation

```powershell
.\install.ps1 `
  -ProjectRoot 'D:\work\your-project' `
  -SkipPlugins
```

Use `install.ps1` rather than `install.cmd` for scripted installation.

## Why not copy `.codex` manually?

Copying dotfiles works until a path changes, an existing file is overwritten, or
you need to prove exactly what was installed. This kit turns that informal copy
operation into a repeatable migration.

| Capability | Manual copy | Portable Setup Kit |
|---|---:|---:|
| Verify source files before writing | No | SHA-256 manifest |
| Back up every replaced destination | Manual | Automatic |
| Adapt user and project paths | Search and replace | Installer substitution |
| Configure project trust and Git hooks | Manual | Included |
| Preserve an operation receipt | No | JSON receipt + pointer |
| Reverse a completed install | Manual | `rollback.ps1` / `ROLLBACK.sh` |
| Exercise the release in CI | Usually no | One seven-step test suite |

## What this tool does

The kit has one job: reproduce the **portable behavior** of a tuned Codex CLI
workspace without copying machine-bound state.

It performs five ordered operations:

1. **Validate** the package structure and SHA-256 manifest.
2. **Back up** every existing destination that will be replaced.
3. **Install** Codex user configuration, instructions, skills, hooks, and project
   automation.
4. **Adapt** portable paths, establish the project configuration layer, and bind
   the packaged Git hook when the target is a Git repository.
5. **Record** a rollback receipt that can restore the pre-install state.

Use it to bootstrap a second Windows development computer, standardize a team
repository, reproduce a tested agent workflow in a disposable project, or keep
auditable install and rollback evidence instead of maintaining a handwritten
dotfile checklist.

The package intentionally does not act as cloud sync, copy authentication,
migrate conversations, or replace npm and Git.

## What gets installed

| Scope | Destination | Installed content | Purpose |
|---|---|---|---|
| Codex user | `%CODEX_HOME%` or `~/.codex` | `config.toml`, `AGENTS.md`, portable instructions, starter rules, skills | Shared model, UI, instruction, tool, and skill defaults |
| Agent user | `~/.agents` | User-level skills | Makes selected reusable skills available outside one repository |
| Project | Explicit `-ProjectRoot` | `AGENTS.md`, `.codex`, `.agents`, `.gitignore`, context workflow | Adds repository instructions, hooks, context tools, and Git guard |
| Git repository | Local Git config | `core.hooksPath=.agents/git-hooks` | Activates the packaged pre-commit guard for that repository |
| Codex CLI | Global npm installation, only when needed | Compatibility-pinned `@openai/codex` | Makes Codex available when it is missing |
| Optional plugins | Current Codex installation | Browser, Visualize, and Sites when available | Extends the environment without making plugin failure fatal |

### Included project automation

- **PostCompact checkpoint hook** records a bounded checkpoint after Codex
  compacts a conversation and returns a clear handoff message.
- **Context Guardian** validates and manages bounded recovery state for long
  tasks without treating stale state as a new request.
- **Codex Continuous** provides a local continuous CLI client with model
  selection, interruption handling, checkpoints, and completion classification.
- **Codex Orchestrator** packages local orchestration primitives and verification
  tools for durable or interactive Codex workflows.
- **Git guard and pre-commit hook** keep runtime, private, and generated artifacts
  out of the root control-plane repository.
- **Reusable skills** cover Cloudflare, Workers, Agents SDK, Durable Objects,
  sandboxing, Turnstile, Wrangler, email, and web-performance workflows.

## How it works

```text
release ZIP or clone
        |
        v
project-root validation
        |
        v
SHA-256 manifest verification
        |
        v
timestamped backup + operation journal
        |
        v
user files -> skills -> project files -> path substitution
        |
        v
project trust + Git hooksPath + optional plugins
        |
        v
receipt.json + last-receipt.txt
```

`install.ps1` records each file operation before replacing its destination. If a
later step fails, the operation list is replayed in reverse and the previous Git
hook binding is restored. On success, the same operation list is saved in the
receipt for a later explicit rollback.

See [Architecture](docs/ARCHITECTURE.md) for component boundaries, destination
mapping, and the complete data flow.

## Command reference

| Goal | Command |
|---|---|
| Verify extracted package | `.\verify.ps1` |
| Install interactively | `.\install.ps1 -ProjectRoot 'D:\work\project'` |
| Verify installed state | `.\verify.ps1 -Installed -ProjectRoot 'D:\work\project'` |
| Roll back latest install | `.\rollback.ps1` |
| Roll back a receipt | `.\rollback.ps1 -Receipt 'C:\path\to\receipt.json'` |
| Regenerate metadata | `.\scripts\Update-PackageMetadata.ps1` |
| Run the release suite | `.\scripts\Test-All.ps1` |

## Installer options

| Option | Default | Description |
|---|---|---|
| `-ProjectRoot` | Safely detected only from clear project markers | Target repository or project directory |
| `-CodexHome` | `%CODEX_HOME%` or `~/.codex` | Target Codex user directory |
| `-AgentsHome` | `~/.agents` | Target user-agent directory |
| `-ReceiptPath` | Timestamped backup directory | Explicit receipt destination for automation and tests |
| `-CodexVersion` | `0.147.0` | Codex CLI version to install when required |
| `-UpgradeCodex` | Off | Reinstalls Codex at `-CodexVersion` even when Codex exists |
| `-SkipPlugins` | Off | Skips optional Browser, Visualize, and Sites plugin attempts |
| `-SkipCodexCheck` | Off | Skips Codex discovery and installation for controlled fixtures |

The bundled `codex-continuous` compatibility matrix was validated with
`openai-codex` SDK `0.144.4` and Codex CLI `0.146.0` / `0.147.0`. Select a newer
CLI explicitly after testing that pair:

```powershell
.\install.ps1 `
  -ProjectRoot 'D:\work\your-project' `
  -CodexVersion '0.151.0' `
  -UpgradeCodex
```

## Verification

Verify an extracted release:

```powershell
.\verify.ps1
```

Verify the installed project and user environment:

```powershell
.\verify.ps1 -Installed -ProjectRoot 'D:\work\your-project'
```

Run the full development and release suite:

```powershell
.\scripts\Test-All.ps1
```

The suite checks package metadata, the package manifest, public-release rules,
an install/rollback fixture, Context Guardian, Codex Continuous, and Codex
Orchestrator.

## Rollback

Restore the latest installation receipt:

```powershell
.\rollback.ps1
```

Restore a specific receipt:

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

The public edition starts with reviewable defaults:

```toml
approval_policy = "on-request"
sandbox_mode = "workspace-write"
```

The public starter rules contain no pre-approved command prefixes. The installer
verifies its manifest before writing, blocks manifest path traversal, records
reversible operations, and excludes machine-bound runtime data.

The release does not include or migrate:

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
|   |-- codex-home/             Codex configuration, instructions, rules, skills
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

## Community

- Read [CONTRIBUTING.md](CONTRIBUTING.md) before proposing code or payload
  changes.
- Use the ready-to-post copy and 48-hour distribution plan in the
  [Promotion playbook](docs/PROMOTION_PLAYBOOK.md).
- Review [CHANGELOG.md](CHANGELOG.md) for release history.

## License

MIT - see [LICENSE](LICENSE).
