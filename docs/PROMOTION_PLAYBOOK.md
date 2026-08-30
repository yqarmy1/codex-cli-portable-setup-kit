# Promotion playbook

## Positioning

### One-line promise

> Move a tested Codex CLI workflow to another Windows machine or repository in
> minutes, with integrity verification, automatic backup, and real rollback.

### The problem it solves

Codex power users often accumulate config, instructions, rules, skills, hooks,
and project automation across several locations. Manual copying is easy to get
wrong, omits hidden dependencies, and rarely includes a reliable way back. This
kit turns that setup into a reviewable, versioned, testable migration artifact.

### Ideal users

1. Developers moving from one Windows computer to another.
2. Teams standardizing Codex conventions across repositories.
3. Power users who maintain custom skills, hooks, and context workflows.
4. Open-source maintainers who want a reproducible contributor setup.

### Proof points to lead with

- one explicit install command;
- SHA-256 verification before writes;
- timestamped backup and automatic rollback;
- user, agent, and project layers in one package;
- no credentials, sessions, caches, or history in the release;
- tested PostCompact, context, and Git-hook behavior.

## Fifteen-minute launch checklist

1. Pin the repository to your GitHub profile.
2. Confirm the About description and repository topics.
3. Publish the tagged ZIP in GitHub Releases.
4. Upload `docs/assets/social-preview.png` in **Settings -> General -> Social
   preview**.
5. Record a 20–30 second terminal clip:
   - run `verify.ps1`;
   - install into a disposable project;
   - run installed verification;
   - run rollback;
   - show restored sentinel files.
6. Add the clip or GIF directly below the README opening paragraph.
7. Post one launch message with the clip, exact outcome, and repository link.

## 48-hour launch plan

### Hour 0: make the repository conversion-ready

- Use the title **Codex CLI Portable Setup Kit** everywhere.
- Keep the README opening focused on the result, not the implementation history.
- Publish a GitHub Release so visitors have one obvious download button.
- Add these topics:
  - `codex`
  - `codex-cli`
  - `openai`
  - `windows`
  - `powershell`
  - `developer-tools`
  - `automation`
  - `dotfiles`
  - `ai-agents`
  - `hooks`
  - `skills`
  - `migration-tool`
- Pin one issue titled **Show your migrated setup** to invite examples and social
  proof.

### Hours 1–3: publish the primary announcement

Post the same core proof in different native formats rather than copying one
large block everywhere:

- X: short result + terminal clip + repository link.
- LinkedIn: problem, why manual copying fails, three technical proof points.
- Reddit: detailed build story, limitations, and a question for other Codex users;
  choose communities whose self-promotion rules permit it.
- Hacker News: `Show HN` title plus a concise technical explanation in the first
  comment.
- DEV Community or Hashnode: tutorial showing the transaction and rollback model.
- OpenAI developer community: describe what you built, the workflow it enables,
  and the exact repository/release link.

### Hours 4–12: answer and improve

- Reply to every substantive question with a concrete command or link to the
  relevant architecture section.
- Turn repeated questions into README improvements.
- Label the first external bug reports quickly.
- Ask early users for one sentence describing the time saved or failure avoided.
- Add real user phrasing to the README only with their permission.

### Hours 12–24: publish the technical follow-up

Write a short article titled:

> How I made a Codex CLI setup portable without copying credentials or sessions

Use this structure:

1. where Codex behavior comes from;
2. why manual dotfile copying is incomplete;
3. the three-layer payload model;
4. manifest verification;
5. operation journal and reverse rollback;
6. a five-command demo;
7. repository and release links.

### Hours 24–48: create a second reason to share

- Publish a small follow-up release based on real feedback.
- Post the terminal demo as a standalone clip.
- Share one architecture diagram or rollback test result.
- Invite contributors to add macOS/Linux support as a clearly scoped roadmap
  discussion instead of claiming unsupported platforms.

## Ready-to-post launch copy

### X / short social post

```text
I open-sourced Codex CLI Portable Setup Kit for Windows.

It moves config + AGENTS.md + skills + hooks + project automation to another machine/repo, verifies every file first, backs up what it replaces, and can roll everything back from a receipt.

No auth, sessions, cookies, keys, or history are included.

Demo + download: REPOSITORY_URL
```

### LinkedIn

```text
Copying a Codex setup is more than moving one config.toml.

The behavior can span user instructions, AGENTS.md, rules, skills, project config, hooks, Git hooks, and local orchestration tools. I packaged that portable layer into a Windows setup kit with:

- SHA-256 verification before any write
- timestamped backups and an operation journal
- automatic rollback when installation fails
- explicit verification of the installed state
- no credentials, sessions, cookies, keys, databases, or history

The repository includes the installer, full architecture, rollback fixture, release ZIP, and a guide for customizing the payload.

REPOSITORY_URL
```

### Show HN

```text
Show HN: A transactional Windows installer for portable Codex CLI setups
```

First comment:

```text
Codex behavior can live across user config, AGENTS.md, rules, skills, hooks, project config, and Git hooks. This project packages the portable parts, verifies a SHA-256 manifest before writing, records every replacement, and restores the old state in reverse if any step fails.

The release excludes authentication, sessions, history, logs, caches, cookies, keys, and databases. The rollback test installs into a disposable fixture, runs the PostCompact hook, verifies the target, then restores original sentinel hashes and Git hooksPath.

Repository: REPOSITORY_URL
```

### Reddit / technical community

```text
Title: I made my Codex CLI setup portable and added transactional rollback

I wanted to move a tuned Codex workflow without copying machine-bound state. The tricky part was that the behavior was split across config.toml, AGENTS.md, rules, skills, project hooks, Git hooks, and local context tools.

The setup kit now verifies every packaged file, backs up each destination, adapts target paths, writes a receipt, and automatically reverses recorded operations on failure. A fixture test proves install -> hook -> verify -> rollback, including restored file hashes and Git hooksPath.

The public edition uses on-request approvals and workspace-write sandboxing, and excludes auth, sessions, cookies, keys, databases, caches, and history.

I would value feedback on the payload boundaries and what platform to support next.

REPOSITORY_URL
```

## Demo script

Use a disposable directory and keep the clip under 30 seconds:

```powershell
$demo = Join-Path $env:TEMP 'codex-portable-demo'
New-Item -ItemType Directory -Path $demo -Force | Out-Null
git -C $demo init --quiet

.\verify.ps1
.\install.ps1 -ProjectRoot $demo -SkipPlugins -SkipCodexCheck
.\verify.ps1 -Installed -ProjectRoot $demo
.\rollback.ps1
```

Overlay four captions:

1. `461+ files verified before writes`
2. `Existing config backed up`
3. `Project hooks and skills installed`
4. `Rollback restored original hashes`

Use the manifest count printed by the current release rather than hard-coding the
sample caption when you record the final clip.

## Conversion improvements

Prioritize these in order:

1. **Terminal GIF/video in the README** — proves the workflow faster than text.
2. **Social preview image** — improves every shared repository link.
3. **One-click GitHub Release download** — removes clone/build friction.
4. **Pinned proof issue** — collects successful installs and requested platforms.
5. **Two screenshots** — package verification and restored rollback result.
6. **Short troubleshooting section** — converts users who encounter execution
   policy, path, or Codex-version questions.

## Metrics

Track a small funnel for the first seven days:

| Stage | Metric | First-week target |
|---|---|---:|
| Reach | Repository link impressions | 5,000 |
| Interest | GitHub page visits | 500 |
| Intent | Release downloads | 100 |
| Activation | Successful-install reports or demo completions | 20 |
| Retention | Stars from activated users | 10+ |
| Learning | Actionable issues/discussions | 5 |

Use GitHub traffic and release download counts. Add UTM parameters per channel if
you publish through a link shortener or analytics-enabled landing page.

## Message discipline

- Lead with the migration outcome, not the number of files.
- Demonstrate rollback; it is the strongest trust-building differentiator.
- State Windows-only support clearly in titles and captions.
- Show real commands and literal outputs.
- Use one call to action: **download the release and run `verify.ps1`**.
- Follow each community's self-promotion rules and contribute to discussions
  outside your own launch thread.

## Official distribution resources

- [GitHub releases](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases)
- [Repository topics](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/classifying-your-repository-with-topics)
- [Social preview image](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/customizing-your-repositorys-social-media-preview)
- [OpenAI developer community](https://developers.openai.com/community)
- [OpenAI showcase](https://developers.openai.com/showcase)
