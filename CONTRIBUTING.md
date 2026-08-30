# Contributing

Thank you for improving the Codex CLI Portable Setup Kit.

## Development requirements

- Windows PowerShell 5.1 or PowerShell 7
- Git
- Python 3.11 or newer for the bundled Python test suites
- Node.js for the PostCompact hook fixture
- Codex CLI for strict installed-config checks; tests degrade to `not-run` when
  Codex is intentionally absent

## Change workflow

1. Create a focused branch.
2. Add or update a test that demonstrates the expected behavior.
3. Make the smallest implementation change that passes the test.
4. Keep public documentation and CLI messages in English.
5. Keep secrets, user paths, hostnames, device identifiers, runtime state, and
   private project names out of the repository.
6. Regenerate package metadata:

   ```powershell
   .\scripts\Update-PackageMetadata.ps1
   ```

7. Run:

   ```powershell
   .\scripts\Test-All.ps1
   ```

8. Review `git diff --check` and the complete diff before opening a pull request.

## Multilingual test fixtures

The continuous-mode completion classifier deliberately recognizes multiple
languages. Preserve that behavior, but encode non-English fixture characters as
Unicode escapes in source files so the public code and documentation remain
English-readable.

## Pull request checklist

- [ ] The change has a narrow purpose and an issue or clear problem statement.
- [ ] New behavior has direct tests.
- [ ] Package integrity verification passes.
- [ ] Installation and rollback pass in a disposable fixture.
- [ ] `PAYLOAD_INDEX.json` and `MANIFEST.sha256` are current.
- [ ] No credential, private data, local receipt, or absolute user path is present.
- [ ] Documentation and release notes explain user-visible behavior.
