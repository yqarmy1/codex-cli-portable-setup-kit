# How the execution-first profile works

## Plain-English summary

This profile is for people who are tired of an AI assistant explaining the work
instead of doing it.

After installation, Codex receives a consistent set of model settings,
instructions, project rules, tools, and continuation helpers. Together they
steer it toward one operating loop:

```text
understand the request
        -> inspect the real target
        -> make the change
        -> run the relevant check
        -> diagnose and fix failures
        -> report the finished result briefly
```

The installer makes that behavior repeatable. Portability, backups, and rollback
are delivery features; the execution profile is the product.

## What each layer actually does

| Layer | Packaged setting or file | Practical effect |
|---|---|---|
| Model | `model = "gpt-5.6-sol"` | Selects the configured Codex model. |
| Reasoning | `model_reasoning_effort = "high"` | Gives complex implementation and debugging tasks a higher reasoning budget. |
| Output length | `model_verbosity = "low"` | Makes normal answers shorter and reduces routine commentary. It does not set reasoning effort to low. |
| User instructions | `portable-agent-instructions.md` | Says actionable requests are work to perform, requires tool use and verification, and discourages stopping at plans or promises. |
| Project instructions | `AGENTS.md` | Applies the same execution discipline inside the installed repository and adds repository hygiene rules. |
| Tools and plugins | Browser, Visualize, Sites, skills, Docs MCP | Gives Codex more ways to inspect, build, test, research, and deliver instead of only suggesting manual steps. |
| Project hooks | `.codex/hooks.json` and `post-compact.mjs` | Records a bounded checkpoint after native conversation compaction. |
| Continuation tools | Context Guardian, Codex Continuous, Codex Orchestrator | Provide validated state, completion classification, and handoff primitives for longer workflows. |
| Delivery | manifest, backup journal, receipt, rollback | Installs the profile repeatably and restores the prior files if requested. |

The official Codex configuration reference documents `model_verbosity` values
of `low`, `medium`, and `high`, and describes `model_reasoning_effort` as a
separate control. This profile deliberately combines **high reasoning** with
**low verbosity**: more problem-solving effort, less talking about the effort.

- [Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)
- [AGENTS.md guidance](https://learn.chatgpt.com/docs/agent-configuration/agents-md)
- [Codex hooks](https://learn.chatgpt.com/docs/hooks)
- [Codex skills](https://learn.chatgpt.com/docs/build-skills)

## The behavior contract

### 1. Action over advice

When the user asks for a file change, code fix, test, package, or repository
operation, the profile tells Codex to use its tools on the real target. A reply
that only lists commands is unfinished when Codex can run those commands itself.

### 2. Completion over progress

A plan is useful internally, but it is not the requested result. The profile
says to continue while there is a concrete next action available: inspect,
change, test, diagnose, correct, and retest.

### 3. Investigation before questions

Codex checks the repository, nearby files, tests, call sites, and existing
conventions before asking the user. It uses a reversible default when that
choice does not materially change the requested outcome. Questions remain for
facts or decisions that only the user can provide.

### 4. Evidence before claims

The profile asks Codex to run checks that directly cover the change and read the
literal exit status and result. When a test fails, the expected next step is to
diagnose and correct it, not to return an unfinished repair as if it were done.

### 5. Short, normal communication

Routine narration is minimized. Final answers use natural language and lead with
the result. They include changed paths, commands, test output, or remaining
choices only when those details help the user verify or continue the work. No
fixed `Current / Result / Next` prefix is installed.

## Before and after

### Repository bug

**Without the profile**

> The issue is probably in the parser. You can inspect `parser.ts`, change the
> branch, and run the unit tests.

**With the profile**

> Fixed the empty-input branch in `parser.ts`. Added the regression case and ran
> the parser suite: 18 passed, exit 0.

The important difference is not the writing style. The second response exists
because Codex inspected, edited, and tested the repository first.

### Packaging task

**Without the profile**

> Here is a five-step guide for creating the archive and checksum.

**With the profile**

> Created `release-v1.2.0.zip` and `SHA256SUMS.txt`; archive verification passed.

### Failed test

**Without the profile**

> The test failed because a fixture path is missing. Please create it and rerun.

**With the profile**

> The first run exposed a missing fixture path. I corrected the fixture lookup
> and reran the suite: all checks passed.

## Reusable core instruction

The packaged instruction file is more complete, but this block captures the
core behavior for another Codex profile or repository-level `AGENTS.md`:

```markdown
Treat actionable requests as work to perform, not advice to describe.
Use tools to inspect, edit, run, and verify the real target. Continue through
inspect -> change -> test -> diagnose -> fix -> retest while a concrete next
action is available. Do not stop at a plan, progress update, or promise when you
can take the next action directly. Investigate locally before asking avoidable
questions. Keep interim narration brief; report the completed result and useful
evidence in natural, concise language.
```

## What it can and cannot change

A profile strongly steers Codex behavior; it does not turn every host into an
unlimited background worker. Actual execution still depends on the tools exposed
by the host, the current permissions, model availability, context limits, and
whether a step requires a user-only decision or an external confirmation.

The package therefore does three practical things rather than making an absolute
promise:

1. makes execution-first behavior the explicit default;
2. supplies tools and continuity components that support that behavior; and
3. verifies that the packaged configuration and rollback path are internally
   consistent.

## Tuning the balance

Edit the installed `~/.codex/config.toml` when you want a different balance:

```toml
# Keep deep task reasoning while shortening normal answers.
model_reasoning_effort = "high"
model_verbosity = "low"
```

Use `model_verbosity = "medium"` if you want more explanation in every response.
Keep verbosity low and ask for a detailed explanation only on the turns where
you need one if your default preference is execution over narration.

Repository-specific rules belong in the target project's `AGENTS.md`. Put the
most concrete project commands there: test entry points, build commands,
generated-file boundaries, and what proves a change is complete.

## Why the installer still matters

The same behavior is split across more than one file. The installer delivers the
user config, instructions, skills, project rules, hooks, and local tools as a
single verified unit. Before replacing anything it validates the SHA-256
manifest; during installation it records backups and operations; afterward the
receipt supports a tested rollback.

That machinery is not the headline. It exists so installing a "do more, say
less" profile does not become another manual task for the user.
