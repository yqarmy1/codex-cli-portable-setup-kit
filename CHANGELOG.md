# Changelog

All notable changes to this project are documented here.

## [6.2.0] - 2026-08-31

### Added

- Detailed execution-profile documentation with plain-English behavior,
  before/after examples, tuning guidance, and a reusable core instruction.
- Behavior-first launch copy and a fast promotion plan centered on the outcome
  rather than the installer's transport mechanics.

### Changed

- Repositioned the project around **Less talk. More execution.** The primary
  purpose is now explicit: make Codex inspect, edit, test, fix, and finish before
  reporting concisely.
- Strengthened user and project instructions so actionable requests trigger
  tool use, continued implementation, failure diagnosis, and retesting instead
  of plans, progress-only responses, or avoidable questions.
- Changed `model_verbosity` from `medium` to `low` while keeping
  `model_reasoning_effort` at `high`.
- Rebuilt the README, GitHub social preview, release narrative, and promotion
  assets around execution-first behavior.

## [6.1.0] - 2026-08-30

### Added

- Complete English public documentation and repository metadata.
- Public-release validation for language, local path leakage, starter rules, and
  safe portable defaults.
- `PAYLOAD_INDEX.json`, metadata generation, full test automation, security
  policy, contributing guide, architecture guide, and promotion playbook.
- GitHub Actions verification on Windows.

### Changed

- Renamed and rewrote the portable agent instructions for natural conversation;
  responses no longer require a `Current / Result / Next` prefix.
- Changed portable defaults to `approval_policy="on-request"` and
  `sandbox_mode="workspace-write"`.
- Changed the desktop locale to English and normalized public CLI messages.
- Replaced machine-specific command approvals with an empty public starter rules
  profile.
- Generalized local identities and preserved multilingual classifier coverage
  through Unicode-escaped fixtures.
- Updated verification so package-only mode works without `-ProjectRoot` and
  native Codex stderr warnings do not become PowerShell failures.

### Removed

- Historical local path inventories, receipts, repair transcripts, and patches
  that were not required for installation.
- Environment-specific hosts, project names, command allowlists, and local source
  paths from the public payload.

## [6.0.0] - 2026-08-30

- Added resilient optional-plugin discovery and installation.
- Preserved core installation when a bundled plugin is unavailable.

## [5.0.0] - 2026-08-30

- Added safe project-root detection, pre-install SHA-256 verification,
  transactional rollback, portable path replacement, and compatibility pinning.
