#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_root="${1:-$(pwd)}"
codex_home="${CODEX_HOME:-$HOME/.codex}"
agents_home="${AGENTS_HOME:-$HOME/.agents}"

echo "=================================================================="
echo "  Universal Multi-Agent Setup & Adaptor (macOS / Linux / WSL)"
echo "=================================================================="
echo "[*] Installing to project root: $project_root"

# Create directories
mkdir -p "$project_root" "$codex_home" "$agents_home"

# Copy payload project items
payload_project="$script_dir/payload/project"
if [ -d "$payload_project" ]; then
  for item in ".agents" ".codex" "AGENTS.md" "CLAUDE.md" "OPENCODE.md" "opencode.json" ".cursorrules" ".windsurfrules" ".mcp.json" ".gitignore"; do
    if [ -e "$payload_project/$item" ]; then
      rm -rf "$project_root/$item"
      cp -R "$payload_project/$item" "$project_root/"
    fi
  done
fi

# Replace path placeholders in installed files
for file in "$project_root/AGENTS.md" "$project_root/CLAUDE.md" "$project_root/OPENCODE.md" "$project_root/.cursorrules" "$project_root/.windsurfrules" "$project_root/.mcp.json" "$project_root/opencode.json"; do
  if [ -f "$file" ]; then
    if [[ "$OSTYPE" == "darwin"* ]]; then
      sed -i '' "s|__PROJECT_ROOT_WIN__|$project_root|g" "$file" || true
      sed -i '' "s|__PROJECT_ROOT_POSIX__|$project_root|g" "$file" || true
    else
      sed -i "s|__PROJECT_ROOT_WIN__|$project_root|g" "$file" || true
      sed -i "s|__PROJECT_ROOT_POSIX__|$project_root|g" "$file" || true
    fi
  fi
done

# Copy codex instructions and skills if ~/.codex is present
if [ -d "$script_dir/payload/codex-home" ]; then
  mkdir -p "$codex_home/instructions" "$codex_home/rules" "$codex_home/skills"
  cp -R "$script_dir/payload/codex-home/instructions/"* "$codex_home/instructions/" 2>/dev/null || true
  cp -R "$script_dir/payload/codex-home/rules/"* "$codex_home/rules/" 2>/dev/null || true
  cp -R "$script_dir/payload/codex-home/skills/"* "$codex_home/skills/" 2>/dev/null || true
fi

echo ""
echo "[+] Universal Multi-Platform Adaptation Summary:"
echo "------------------------------------------------------------------"
echo "  1. OpenAI Codex CLI    : ACTIVE (AGENTS.md, ~/.codex)"
echo "  2. OpenCode Agent      : ACTIVE (OPENCODE.md, opencode.json)"
echo "  3. Claude Code         : ACTIVE (CLAUDE.md)"
echo "  4. Cursor / Windsurf   : ACTIVE (.cursorrules, .windsurfrules, .mcp.json)"
echo "  5. ChatGPT App / Web   : READY  (docs/CHATGPT_APP_PRESET.md)"
echo "  6. Subsystem Engine    : ACTIVE (python3 .agents/tools/re-toolkit/cli.py)"
echo "------------------------------------------------------------------"
echo "[OK] Installation completed successfully for macOS / Linux / WSL!"
