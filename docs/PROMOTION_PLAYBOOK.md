# Promotion playbook

## Positioning

### One-line promise

> **No sermons. No stalling. Just execution.**

Expanded version:

> **Jailbreak the workflow, not the model.** A jailbreak-inspired Codex profile that cuts task-irrelevant moralizing and turns more of every session into real tool use, verified changes, and finished results.

This is the sharpest honest angle. The "jailbreak" is the break from a
lecture-first workflow: generic moralizing, repetitive caveats, refusal-style
filler, avoidable questions, and premature stopping. The underlying model is not
presented as exploited or unlocked.

### What the product really is

This is an **anti-lecture, execution-first behavior profile for Codex**. It pushes GPT to:

- inspect the real workspace before answering;
- use tools instead of handing commands back to the user;
- edit, run, test, diagnose, fix, and retest;
- continue past plans and status updates when work can proceed;
- ask fewer avoidable questions;
- minimize generic moral lectures, repetitive caveats, and task-irrelevant
  refusal-style boilerplate while keeping real constraints actionable; and
- report the finished result in normal, concise language.

The installer, package verification, backups, and rollback are important proof
that the profile is easy to adopt. They are not the main headline.

### The problem it solves

A frustrating agent interaction has low execution density: the user asks for a
result, but the session is spent on plans, narration, generic moral advice,
repeated caveats, or refusal-style boilerplate that does not change the next
action. The actual work stays unfinished, and the user becomes the operator.

This profile changes the default. Codex is instructed to become the operator:
do the concrete work first, prove it with relevant checks, then give the user a
short result.

### Ideal users

1. Developers who already use Codex but spend too much time saying "continue."
2. People who want fixes and artifacts, not another tutorial or checklist.
3. Power users who want high reasoning effort without long routine answers.
4. Teams that want consistent execution and verification rules in repositories.
5. Windows users who want that profile installed with backups and rollback.

### Proof points to lead with

Lead with visible behavior, then support it with implementation proof:

1. execution-first user and project instructions;
2. high reasoning effort plus low output verbosity;
3. explicit inspect -> edit -> test -> fix -> retest loop;
4. no forced `Current / Result / Next` response template;
5. PostCompact checkpoints and long-task continuation components;
6. one-command installation with SHA-256 verification and tested rollback; and
7. a seven-step CI suite that verifies the release artifact.

## High-star project narrative

High-star developer projects usually win the first screen with a short outcome,
a clear object, visible proof, and an immediate command. They do not begin with
an origin story or a list of internal files.

Use the same structure here:

**One outcome. Three proof points. One call to action.**

- **Outcome:** Codex delivers higher execution density with fewer irrelevant sermons and stalled handoffs.
- **Proof 1:** execution-first instructions and low verbosity are visible in the repo.
- **Proof 2:** the package runs a real verification and rollback fixture.
- **Proof 3:** the long-task components and tests are inspectable.
- **Action:** download the release, run `verify.ps1`, install it into a disposable repository.

### GitHub About description

Use this exact description:

> No sermons. No stalling. Just execution. A jailbreak-inspired Codex profile that cuts moralizing and pushes GPT to finish the job.

### First-screen copy stack

1. **Tagline:** `No sermons. No stalling. Just execution.`
2. **Hook:** `Jailbreak the workflow, not the model.`
3. **Definition:** `An anti-lecture, execution-first Codex profile.`
4. **Concrete verbs:** inspect, edit, test, fix, finish.
5. **Proof:** high reasoning, low verbosity, verified install and rollback.
6. **Action:** download the latest release.

### README order

1. Project name and four-word promise.
2. One sentence saying exactly what changes after installation.
3. CI, release, license, and platform badges.
4. A social preview or terminal demo that repeats the promise.
5. Six to eight behavior-first highlights.
6. A before/after table.
7. A three-command quick start.
8. Detailed configuration, architecture, security, and rollback.

### Writing rules

- Use short sentences and concrete verbs.
- Say what Codex does after installation, not how clever the configuration is.
- Put behavior before transport: execution profile first, installer second.
- Replace "powerful" with an observable result such as "runs the test."
- Avoid absolute autonomy claims that the repository cannot prove.
- Describe observable workflow gains, not claims about bypassing rules or constraints.
- Use the jailbreak angle for the workflow, never as a claim that the project exploits or unlocks the underlying model.
- Lead with "anti-lecture" and immediately name the observable change: less
  irrelevant moralizing, more tool use, tests, fixes, and finished work.
- Show one literal command or result next to each technical promise.
- Keep the same tagline in the README, About box, release, preview image, and posts.
- Give each post one call to action: try it, star it, or report a result.

### Release headline

> Codex CLI Portable Setup Kit v6.3.0 - jailbreak the workflow, not the model

The first paragraph should say that v6.3.0 is the anti-lecture release: it raises
execution density by reducing irrelevant moralizing, repetitive caveats, and
refusal-style boilerplate while keeping real constraints actionable. Then
show the behavior changes, the three-command install, the exact test result, and
the ZIP checksum.

## Fifteen-minute launch checklist

Do these in order. Stop polishing after step seven and publish.

1. Set the GitHub About description to the exact sentence above.
2. Add topics: `codex`, `codex-cli`, `openai`, `ai-agents`, `agentic-workflow`,
   `execution-first`, `prompt-engineering`, `windows`, `powershell`, and
   `developer-tools`.
3. Upload `docs/assets/social-preview.png` in **Settings -> General -> Social preview**.
4. Pin the repository on the owner's GitHub profile.
5. Publish the v6.3.0 release with the verified ZIP and SHA-256 checksum.
6. Record one 20-30 second screen capture: ask Codex to change a fixture, show it
   edit the file, run a test, fix a deliberate failure, and finish with a short result.
7. Put that clip directly below the README opening paragraph.
8. Post the short launch copy on X or LinkedIn.
9. Submit the technical version to Show HN.
10. Share a native, non-spammy explanation in one relevant Codex or coding-agent
    community whose current posting rules allow project links.

## 48-hour launch plan

### Hour 0: make the repository conversion-ready

- Keep one promise everywhere: **No sermons. No stalling. Just execution.**
- Make the latest release the obvious download.
- Confirm the README gives a working command before deep implementation detail.
- Open one GitHub Discussion titled **What did Codex finish for you?**
- Create one issue template for behavior reports with fields for request, actual
  behavior, expected behavior, and verification command.
- Pin an issue titled **Share a before/after example** to collect social proof.

### Hours 1-3: publish the primary announcement

Publish the same proof in the native format of each channel:

- **Show HN:** technical problem, implementation, limitations, repository.
- **X:** four-word hook, 20-second clip, one link.
- **LinkedIn:** pain point, behavior change, five concrete mechanisms.
- **Reddit:** personal build story, transparent limitations, ask for feedback.
- **OpenAI developer community:** exact Codex settings, instructions, tests, link.
- **Product Hunt or DevHunt:** use after the technical audience has produced a
  few real examples; screenshots and testimonials convert better than claims.

Do not paste the same long paragraph into every site. Reuse the promise and
proof, but rewrite the opening for the community.

### Hours 4-12: answer and improve

- Reply to questions with a direct README section, command, or test result.
- Turn the first repeated misunderstanding into a README edit immediately.
- Ask successful users for a two-line before/after example, not generic praise.
- Label and reproduce behavior reports quickly.
- Add one real example to the README after the user confirms it can be shared.

### Hours 12-24: publish the technical follow-up

Write an article titled:

> High reasoning, low verbosity: making Codex do the work before talking

Use this structure:

1. the "plan instead of result" problem;
2. why verbosity and reasoning are separate settings;
3. the execution-first instruction contract;
4. inspect -> edit -> test -> fix -> retest;
5. how project `AGENTS.md` rules reinforce the behavior;
6. how checkpoints help long tasks retain bounded state;
7. what the profile cannot guarantee;
8. a real before/after terminal recording;
9. repository and release links.

Publish on DEV Community, Hashnode, or a personal blog, then link the article
from GitHub Discussions.

### Hours 24-48: create a second reason to share

- Publish one before/after demo as its own post.
- Share one failure-to-fix test loop, not another feature list.
- Cut the terminal clip into a silent GIF for the README.
- Release a small improvement based on actual feedback.
- Ask which default users want next: even quieter output, medium verbosity, or a
  project-only install mode.

## Ready-to-post launch copy

Replace `REPOSITORY_URL` with the public repository link.

### X / short social post

```text
No sermons. No stalling. Just execution.

Jailbreak the workflow, not the model.

I made an anti-lecture Codex profile that cuts irrelevant moralizing, repetitive caveats, and refusal-style filler - then pushes GPT through inspect -> edit -> test -> fix -> retest.

High reasoning. Low verbosity. Verified install + rollback.
REPOSITORY_URL
```

### LinkedIn

```text
I was tired of coding agents giving me a lecture instead of touching the repository.

So I packaged a jailbreak-inspired, anti-lecture Codex profile. It does not claim a model exploit; it breaks the low-value workflow that fills sessions with generic moral advice, repeated caveats, narration, and avoidable questions.

After installation, the default loop is:
inspect -> edit -> test -> diagnose -> fix -> retest -> concise result

The profile combines:
- high reasoning effort with low response verbosity
- user and project instructions that prefer action over advice
- verification before completion claims
- PostCompact checkpoints and long-task tools
- a SHA-256-verified installer with backup and rollback

It is open source for Windows:
REPOSITORY_URL
```

### Show HN

```text
Show HN: No-sermons mode for Codex - jailbreak the workflow, not the model
```

First comment:

```text
I built this because too many agent sessions have low execution density: the interaction fills with generic moral advice, repeated caveats, refusal-style boilerplate, plans, or commands handed back to the user while the real task stays unfinished.

The profile combines high model reasoning effort with low response verbosity and explicit instructions to inspect the real target, use tools, continue through edit/test/fix cycles, and report only after verification. Project-level AGENTS.md rules reinforce the behavior, while PostCompact checkpoints and local continuation components help longer tasks retain a bounded handoff.

The Windows installer is the delivery layer. It verifies a SHA-256 manifest, backs up replaced files, records a receipt, and has a real rollback fixture. It excludes authentication and session data.

I would especially value before/after examples and cases where Codex still stops too early.

Repository: REPOSITORY_URL
```

### Reddit / technical community

```text
Title: I made an anti-lecture Codex profile - jailbreak the workflow, not the model

My recurring frustration with coding agents was not model intelligence. It was the lecture-first workflow: too much of the interaction went to generic moral advice, repeated caveats, refusal-style filler, plans, or questions the repo could answer, while the real task remained unfinished.

This profile changes that default. It tells Codex to inspect the workspace, use tools, edit, test, diagnose failures, fix them, and retest. It uses high reasoning effort with low response verbosity, so the work can stay thorough while the routine answer stays short.

The package also includes project AGENTS.md rules, PostCompact checkpoints, long-task helpers, a verified Windows installer, backups, and rollback. It does not copy authentication or conversation history.

I would like honest examples where it still talks too much or stops too early.

REPOSITORY_URL
```

### OpenAI developer community

```text
Jailbreak the workflow, not the model: an anti-lecture Codex profile

This open-source profile combines model_reasoning_effort="high" with model_verbosity="low", a tool-first instruction layer, project AGENTS.md rules, verification requirements, and bounded PostCompact checkpoints.

The goal is simple: reduce task-irrelevant moralizing and increase execution density. If Codex can inspect, edit, run, test, or fix the target now, it should do that before adding commentary that does not change the next action.

Repository and verified Windows release:
REPOSITORY_URL
```

## Demo script

A behavior demo is stronger than an installation demo. Use a tiny disposable
repository with one deliberately failing test and record this prompt:

```text
Fix the failing test in this repository. Inspect the cause, make the smallest
correct change, run the relevant tests, and keep working until they pass. Give me
a short result when finished.
```

Then record these visible moments:

1. Codex opens the failing test and implementation.
2. Codex edits the implementation.
3. The first test run fails for a real reason.
4. Codex diagnoses and corrects it without asking the viewer to take over.
5. The second run passes.
6. The final answer is two or three lines.

Record the installer separately if needed:

```powershell
$demo = Join-Path $env:TEMP 'codex-execution-profile-demo'
New-Item -ItemType Directory -Path $demo -Force | Out-Null
git -C $demo init --quiet

.\verify.ps1
.\install.ps1 -ProjectRoot $demo -SkipPlugins -SkipCodexCheck
.\verify.ps1 -Installed -ProjectRoot $demo
.\rollback.ps1
```

Overlay four captions:

1. `High reasoning / low verbosity`
2. `Inspect -> edit -> test`
3. `Failure -> fix -> retest`
4. `Short verified result`

## Conversion improvements

Implement these only after the first launch data shows where users leave:

1. Add a five-second before/after GIF under the tagline.
2. Add one copyable project-only installation command.
3. Publish three real behavior transcripts with sensitive data removed.
4. Add a table comparing low and medium verbosity profiles.
5. Create a one-click disposable demo repository.
6. Add macOS or Linux support only after Windows behavior and rollback are stable.

## Metrics

Track a small funnel instead of staring only at stars:

| Stage | Useful signal |
|---|---|
| Discovery | unique repository visitors and referral source |
| Understanding | README-to-release click rate |
| Trial | release downloads and successful verification reports |
| Activation | users who install and share one completed-task example |
| Trust | issues with reproducible evidence and rollback reports |
| Advocacy | stars, forks, mentions, and contributed profiles |

The most valuable early metric is the number of people who can describe the
project correctly in one sentence. If they call it a migration tool, the opening
copy still needs work.

## Message discipline

Always say:

- "execution-first Codex profile" before "portable setup kit";
- "does more work and says less" before technical settings;
- "inspect, edit, test, fix, finish" before component names;
- "verified install and rollback" as proof, not as the main product.

Do not lead with file counts, migration, dotfiles, or the installer's internal
architecture. Those details help evaluation after the visitor understands the
outcome.

## Official distribution resources

- Repository: `https://github.com/2akouwu/codex-cli-portable-setup-kit`
- Latest release: `https://github.com/2akouwu/codex-cli-portable-setup-kit/releases/latest`
- Release feed: `https://github.com/2akouwu/codex-cli-portable-setup-kit/releases.atom`
- Issues: `https://github.com/2akouwu/codex-cli-portable-setup-kit/issues`
- Discussions: `https://github.com/2akouwu/codex-cli-portable-setup-kit/discussions`
