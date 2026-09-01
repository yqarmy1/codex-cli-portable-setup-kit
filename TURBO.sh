#!/usr/bin/env bash
set -euo pipefail
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bash "$script_dir/install.sh" "$(pwd)"
if command -v codex >/dev/null 2>&1; then
  exec codex "$@"
else
  echo "[+] Workspace configured! Run your preferred agent (codex, opencode, claude, cursor)."
fi
