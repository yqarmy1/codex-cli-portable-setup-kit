#!/usr/bin/env sh
set -eu
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$script_dir/rollback.ps1" "$@"
