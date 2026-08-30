# Security policy

## Supported release

Security fixes are applied to the latest published release.

## Reporting a vulnerability

Open a private GitHub security advisory for vulnerabilities involving installer
path handling, manifest verification, rollback integrity, command execution, or
sensitive-data exposure. Include:

- the affected release and commit;
- the exact command and inputs used;
- the observed output and exit status;
- a minimal reproduction that contains no real credentials or private data;
- the expected behavior.

Do not put tokens, cookies, keys, receipts from a real computer, raw logs, or
private project paths in a public issue.

## Public-fork checklist

Before publishing a customized fork:

1. Search the entire tree and Git history for credentials and local identities.
2. Replace machine-specific `default.rules` content with reviewed starter rules.
3. Remove source-path inventories, receipts, logs, caches, and runtime state.
4. Confirm that `.env`, key, database, cookie, and token files are ignored.
5. Run `tests/Test-PublicRelease.ps1` and `scripts/Test-All.ps1`.
6. Build the release from a clean history so removed data is not reachable from
   older public commits.

## Installer trust boundaries

- `MANIFEST.sha256` protects the files it lists against accidental or malicious
  modification after packaging.
- `install.ps1` resolves manifest paths beneath the package root and rejects path
  traversal.
- Existing destinations are backed up before replacement, and operations are
  recorded in a receipt.
- Optional plugin installation is best-effort and does not make the core file
  transaction succeed or fail.
- A receipt contains local filesystem paths. Store it locally and do not commit it.
- This project does not distribute authentication, OAuth state, API keys, access
  tokens, cookies, private keys, or local databases.
