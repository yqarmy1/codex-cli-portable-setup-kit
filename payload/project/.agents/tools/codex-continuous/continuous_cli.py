#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import getpass
import hashlib
import importlib.metadata
import json
import os
import queue
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from claude_ui import (
    DEFAULT_KEYBINDINGS_PATH,
    EDITOR_TOKEN,
    EFFORT_TOKEN,
    EXIT_TOKEN,
    FAST_TOKEN,
    MODEL_TOKEN,
    PASTE_IMAGE_TOKEN,
    PERMISSIONS_TOKEN,
    REWIND_TOKEN,
    TRANSCRIPT_TOKEN,
    TASKS_TOKEN,
    ClaudePromptUI,
    MenuSelection,
)


def configure_stdio() -> None:
    for stream_name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


configure_stdio()


DEFAULT_ROLLOVER_RATIO = 0.55
MAX_AUTOMATIC_PROGRESS_TURNS = 24
DEFAULT_AUTO_INPUT_GRACE_MS = 1500
MAX_HANDOFF_FIELD_CHARS = 2400
MAX_GUARDIAN_BUNDLE_CHARS = 14000
MAX_COMMAND_OUTPUT_BUFFER_CHARS = 16000
MAX_CHECKPOINT_FILE_BYTES = 8 * 1024 * 1024
MAX_CHECKPOINT_TURN_BYTES = 32 * 1024 * 1024
MAX_SESSION_META_BYTES = 1024 * 1024
ROLLOVER_JOURNAL_SCHEMA_VERSION = 1
GUARDIAN_LIFECYCLE_RECEIPT_SCHEMA_VERSION = 2
GUARDIAN_WORK_PROTOCOLS = {
    "function_call": "function_call_output",
    "custom_tool_call": "custom_tool_call_output",
}
ROLLOVER_JOURNAL_PHASES = (
    "prepared",
    "target_created",
    "dispatch_started",
    "concrete_started",
    "source_retired",
    "completion_ready",
)
ROLLOVER_THREAD_SOURCE_PREFIX = "codex-continuous-rollover:"
ENABLE_LEGACY_FILE_REWIND = False
GENERIC_BLOCK_MARKERS = (
    "this content can't be shown",
    "this content can’t be shown",
)
GENERIC_BLOCK_CAUTION = "we take extra caution with cybersecurity requests"
GENERIC_POLICY_BLOCK_MARKERS = (
    "this content was flagged for possible cybersecurity risk",
)
POTENTIAL_SIDE_EFFECT_ITEMS = frozenset(
    {
        "commandExecution",
        "fileChange",
        "mcpToolCall",
        "dynamicToolCall",
        "collabAgentToolCall",
        "imageGeneration",
    }
)
READ_ONLY_COMMAND_ACTIONS = frozenset({"read", "listFiles", "search"})
CONTEXTCTL_SUBCOMMANDS = frozenset(
    {"preflight", "checkpoint", "pulse", "refresh", "audit", "finish"}
)
LIFECYCLE_ONLY_TOOL_NAMES = frozenset(
    {
        "create_goal",
        "get_goal",
        "update_goal",
        "update_plan",
        "request_user_input",
    }
)
CONTINUOUS_OBJECTIVE_COMPLETE_MARKER = "[CONTINUOUS_OBJECTIVE_COMPLETE]"
CONTINUOUS_USER_INPUT_REQUIRED_MARKER = "[CONTINUOUS_USER_INPUT_REQUIRED]"
TESTED_SDK_CLI_PAIRS = frozenset(
    {
        ("0.144.4", "0.146.0"),
        ("0.144.4", "0.147.0"),
    }
)
RECOVERABLE_TRANSPORT_CODES = frozenset(
    {
        "httpConnectionFailed",
        "responseStreamConnectionFailed",
        "responseStreamDisconnected",
        "responseTooManyFailedAttempts",
        "serverOverloaded",
        "internalServerError",
    }
)
ANSI_ESCAPE_RE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))"
)
SLASH_COMMANDS: tuple[tuple[str, str], ...] = (
    ("/model", "Set the AI model and reasoning effort"),
    ("/effort", "Set reasoning effort for the current model"),
    ("/fast", "Configure Fast mode"),
    ("/permissions", "View or update permissions"),
    ("/status", "Show session, model, and context status"),
    ("/compact", "Compact conversation history"),
    ("/diff", "Show working-tree changes"),
    ("/copy", "Copy the latest response"),
    ("/mcp", "Manage MCP servers"),
    ("/skills", "Browse available skills"),
    ("/apps", "Browse connected apps"),
    ("/plugins", "Browse plugins"),
    ("/hooks", "Inspect lifecycle hooks"),
    ("/goal", "View or clear the task goal"),
    ("/personality", "Choose response style"),
    ("/ps", "Show background terminals"),
    ("/stop", "Stop background terminals"),
    ("/branch", "Create a branch of this conversation"),
    ("/fork", "Alias for /branch"),
    ("/btw", "Ask a side question without adding it to the conversation"),
    ("/rewind", "Rewind conversation to an earlier prompt"),
    ("/undo", "alias for /rewind"),
    ("/checkpoint", "alias for /rewind"),
    ("/review", "review working-tree changes"),
    ("/rename", "rename this conversation"),
    ("/new", "start a fresh conversation"),
    ("/clear", "clear and start fresh"),
    ("/resume", "resume a saved conversation"),
    ("/mention", "attach a file or folder"),
    ("/usage", "account usage"),
    ("/init", "create an AGENTS.md scaffold"),
    ("/debug-config", "inspect config layers"),
    ("/archive", "archive this conversation"),
    ("/delete", "delete this conversation"),
    ("/logout", "sign out of Codex"),
    ("/transcript", "Open the transcript viewer"),
    ("/verbose", "Toggle detailed tool output"),
    ("/keybindings", "Customize keyboard shortcuts"),
    ("/help", "Show shortcuts"),
    ("/exit", "Exit"),
    ("/quit", "Exit"),
)
SLASH_COMMAND_NAMES = frozenset(name for name, _description in SLASH_COMMANDS)

CONTINUOUS_DEVELOPER_INSTRUCTIONS = """\
This thread is controlled by a local continuous Codex client. Work autonomously
until the user's concrete objective is genuinely complete. Keep bounded project
state current at meaningful milestones. Never ask the user to type /new: the
client performs silent clear/fresh-thread rollover. Never report task IDs,
checkpoints, handoffs, rollover success, or that work is about to continue;
continue the work itself. Evaluate concrete actions individually; if one exact
action is unavailable, state that limitation briefly and continue all unaffected
permitted engineering. Rollover bookkeeping is never completion. After a handoff, execute
the first unresolved next action immediately. For a registered context-guardian
project, prove genuine completion by running its required validation, a CAS
checkpoint with no open/next actions, task-bound audit, and plain finish
commands. For an unregistered project, append
[CONTINUOUS_OBJECTIVE_COMPLETE] only after validation proves the objective is
complete. If progress truly requires information only the user can provide, use
the user-input tool. Plain assistant text is never proof of a guarded user
blocker. Only for an unregistered project where that tool is unavailable,
append [CONTINUOUS_USER_INPUT_REQUIRED] with the exact question.
"""

STARTUP_RESUME_PROMPT = """\
[CONTROLLER-INITIATED EXPLICIT RESUME]

The user launched Codex Continuous with --resume and explicitly wants seamless
continuation of the bounded project objective. Run the project's required
context-guardian preflight with --resume, then inspect the generated Active
State and direct working-tree evidence. Continue only work proven unfinished;
do not replay completed mutations or trust stale prose. Execute the first
unresolved next action in this turn; a resume/status summary is not work. If the
objective is currently waiting on external input, use the user-input tool in
this turn. Never ask the user to create another chat or type /new.
"""

RECOVERED_ROLLOVER_PROMPT = """\
[CONTROLLER RECOVERED GUARDED ROLLOVER]

The controller process restarted after creating this exact task. Stay in this
task; do not create, fork, clear, or hand off to another task. The exact Guardian
lineage binds this task to its audited source. If its runtime is inactive because
an earlier turn called finish while explicitly leaving work unfinished, first run
the required context-guardian preflight with --resume for this exact task. Inspect
the generated Active State and direct working-tree evidence, then execute the
first concrete unfinished action without replaying mutations already present. Do not report
rollover, checkpoint, task IDs, recovery, or continuation status. A task switch
is not project completion. If the objective is genuinely complete, run the
registered validation, completed-state checkpoint, task-bound audit, and plain
finish transaction. Stop only for a genuine user-only decision.
"""

RECOVERED_ROLLOVER_DISPATCH_PROMPT = """\
[CONTROLLER RECOVERY DISPATCH]

Execute the recovered task instruction above now. Do not return a recovery,
rollover, handoff, checkpoint, task-ID, or continuation status message.
"""


def display_width(value: str) -> int:
    """Return terminal-cell width without depending on the active code page."""
    width = 0
    for character in ANSI_ESCAPE_RE.sub("", value):
        if unicodedata.combining(character):
            continue
        category = unicodedata.category(character)
        if category.startswith("C"):
            continue
        width += 2 if unicodedata.east_asian_width(character) in {"F", "W"} else 1
    return width


def sanitize_terminal_metadata(value: str | None) -> str:
    """Make untrusted tool metadata safe to render on one terminal line."""
    without_ansi = ANSI_ESCAPE_RE.sub("", value or "")
    without_controls = "".join(
        character
        if character in {"\t", "\r", "\n"}
        or character in {"\u200c", "\u200d"}
        or not unicodedata.category(character).startswith("C")
        else " "
        for character in without_ansi
    )
    return re.sub(r"\s+", " ", without_controls).strip()


def sanitize_terminal_content(value: str) -> str:
    """Strip cursor controls while preserving prose, tabs, and line breaks."""
    normalized = ANSI_ESCAPE_RE.sub("", value).replace("\r\n", "\n").replace("\r", "\n")
    return "".join(
        character
        if character in {"\t", "\n"}
        or character in {"\u200c", "\u200d"}
        or not unicodedata.category(character).startswith("C")
        else " "
        for character in normalized
    )


def fit_terminal_text(value: str, width: int, *, ellipsis: str = "…") -> str:
    """Truncate metadata by display cells; never truncate assistant prose."""
    cleaned = sanitize_terminal_metadata(value)
    if width <= 0:
        return ""
    if display_width(cleaned) <= width:
        return cleaned
    ellipsis_width = max(1, display_width(ellipsis))
    if width <= ellipsis_width:
        return ellipsis[:1]
    result: list[str] = []
    used = 0
    target = width - ellipsis_width
    for character in cleaned:
        cells = display_width(character)
        if used + cells > target:
            break
        result.append(character)
        used += cells
    return "".join(result) + ellipsis


@dataclass(frozen=True, slots=True)
class RenderCapabilities:
    tty: bool
    color: bool
    unicode: bool
    width: int
    rows: int

    @classmethod
    def detect(cls, stream: Any, *, force_plain: bool = False) -> "RenderCapabilities":
        is_dumb = os.environ.get("TERM", "").casefold() == "dumb"
        is_tty = (
            bool(getattr(stream, "isatty", lambda: False)())
            and not force_plain
            and not is_dumb
        )
        terminal = shutil.get_terminal_size(fallback=(100, 30))
        encoding = getattr(stream, "encoding", None) or "utf-8"
        try:
            "❯●◇✓⚠✘↳⎿─╭╮╰╯".encode(encoding)
            supports_unicode = True
        except (LookupError, UnicodeEncodeError):
            supports_unicode = False
        color = (
            is_tty
            and "NO_COLOR" not in os.environ
        )
        return cls(
            tty=is_tty,
            color=color,
            unicode=supports_unicode,
            width=max(20, terminal.columns),
            rows=max(8, terminal.lines),
        )


class RenderLevel(str, Enum):
    INFO = "info"
    PROGRESS = "progress"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"
    DETAIL = "detail"


@dataclass(frozen=True, slots=True)
class ToolPresentation:
    kind: str
    label: str
    detail: str = ""
    success: bool | None = None


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    path: Path
    exists: bool
    content: bytes | None
    mode: int | None


@dataclass(frozen=True, slots=True)
class FileCheckpoint:
    before: dict[Path, FileSnapshot]
    after: dict[Path, FileSnapshot]


class TerminalRenderer:
    """A restrained Claude-like presentation layer with a deterministic fallback."""

    _COLORS = {
        "brand": "\x1b[38;2;215;119;87m",
        "system": "\x1b[38;2;87;105;247m",
        "border": "\x1b[38;2;102;102;102m",
        "inactive": "\x1b[38;2;145;145;145m",
        "success": "\x1b[32m",
        "warning": "\x1b[33m",
        "error": "\x1b[31m",
    }

    def __init__(
        self,
        *,
        stream: Any | None = None,
        lock: threading.RLock | None = None,
        capabilities: RenderCapabilities | None = None,
        force_plain: bool = False,
    ) -> None:
        self.stream = stream or sys.stdout
        self.lock = lock or threading.RLock()
        self._dynamic_size = capabilities is None
        self.capabilities = capabilities or RenderCapabilities.detect(
            self.stream,
            force_plain=force_plain,
        )
        self._native_stream = self.stream is sys.stdout
        self._claude_ui: ClaudePromptUI | None = None
        self._action_draft = ""
        self._transcript: list[str] = []
        self._assistant_transcript: list[str] = []
        self.line_open = False
        self.epoch = 0

    @property
    def claude_like(self) -> bool:
        return self.capabilities.tty

    def _glyph(self, unicode_value: str, ascii_value: str) -> str:
        return unicode_value if self.capabilities.unicode else ascii_value

    def _styled(self, role: str, value: str) -> str:
        if not self.capabilities.color:
            return value
        prefix = self._COLORS.get(role)
        return f"{prefix}{value}\x1b[0m" if prefix else value

    def _write(self, value: str, *, flush: bool = True) -> None:
        try:
            self.stream.write(value)
        except UnicodeEncodeError:
            encoding = getattr(self.stream, "encoding", None) or "ascii"
            self.stream.write(value.encode(encoding, errors="replace").decode(encoding))
        if flush:
            self.stream.flush()

    def _writeln(self, value: str = "") -> None:
        self._write(value + "\n")
        clean = ANSI_ESCAPE_RE.sub("", value).rstrip()
        if clean:
            self.record_transcript(clean)
        self.line_open = False

    def record_transcript(self, value: str) -> None:
        clean = sanitize_terminal_content(value).rstrip()
        if not clean:
            return
        self._transcript.append(clean)
        if len(self._transcript) > 4000:
            del self._transcript[: len(self._transcript) - 4000]

    def ensure_newline(self) -> None:
        if self.line_open:
            self._writeln()

    def _border_width(self) -> int:
        width = self.capabilities.width
        if self._dynamic_size and self.claude_like:
            width = shutil.get_terminal_size(
                fallback=(self.capabilities.width, self.capabilities.rows)
            ).columns
        return max(8, width)

    def _fit(self, value: str, width: int) -> str:
        return fit_terminal_text(
            value,
            width,
            ellipsis=self._glyph("…", "~"),
        )

    def _terminal_rows(self) -> int:
        if self._dynamic_size and self.claude_like:
            return max(
                8,
                shutil.get_terminal_size(
                    fallback=(self.capabilities.width, self.capabilities.rows)
                ).lines,
            )
        return self.capabilities.rows

    def _full_tui_layout(self) -> bool:
        return (
            self.capabilities.unicode
            and self._terminal_rows() >= 30
            and self._border_width() >= 80
        )

    def banner(
        self,
        version: str,
        project_name: str,
        *,
        model: str | None = None,
        effort: str | None = None,
    ) -> None:
        with self.lock:
            safe_version = sanitize_terminal_metadata(version)
            safe_project = sanitize_terminal_metadata(project_name)
            if not self.claude_like:
                self._writeln(f"Codex Continuous {safe_version} | {safe_project}")
                return
            title = f" Codex Code v{safe_version} "
            if not self._full_tui_layout():
                compact = f"Codex Code v{safe_version}"
                suffix = f" · {safe_project}"
                if display_width(compact + suffix) <= self._border_width():
                    compact += suffix
                self._writeln(
                    self._styled("brand", self._fit(compact, self._border_width()))
                )
                return
            width = self._border_width()
            inner = width - 2
            title_width = display_width(title)
            lead = self._glyph("─", "-") * 3
            top = (
                self._styled("border", self._glyph("╭", "+") + lead)
                + self._styled("brand", title)
                + self._styled(
                    "border",
                    self._glyph("─", "-")
                    * max(0, inner - display_width(lead) - title_width)
                    + self._glyph("╮", "+"),
                )
            )
            divider = max(34, min(inner - 26, int(inner * 0.56)))
            right_width = inner - divider - 1
            user = sanitize_terminal_metadata(getpass.getuser())
            model_line = " · ".join(
                part for part in (model or "Codex", effort and f"{effort} effort") if part
            )
            left_rows = [
                "",
                f"Welcome back {user}!",
                "",
                "▐▛███▜▌" if self.capabilities.unicode else "[ CODEX ]",
                "▝▜█████▛▘" if self.capabilities.unicode else "[=======]",
                "  ▘▘ ▝▝" if self.capabilities.unicode else "   / \\",
                model_line,
                safe_project,
            ]
            right_rows = [
                "Tips for getting started",
                "Run /init to create an AGENTS.md file",
                "Use @ to mention files in your prompt",
                "────────────────────────────" if self.capabilities.unicode else "----------------------------",
                "Quick commands",
                "/model   switch model and effort",
                "/permissions   change permission mode",
                "/help for more",
            ]
            body_lines: list[str] = []
            for left, right in zip(left_rows, right_rows, strict=True):
                left_value = self._fit(left, divider - 2)
                if left in left_rows[1:7] and left:
                    left_padding = max(0, (divider - display_width(left_value)) // 2)
                    left_cell = " " * left_padding + left_value
                else:
                    left_cell = "  " + left_value
                left_cell += " " * max(0, divider - display_width(left_cell))
                right_cell = " " + self._fit(right, right_width - 1)
                right_cell += " " * max(0, right_width - display_width(right_cell))
                body_lines.append(
                    self._styled("border", self._glyph("│", "|"))
                    + left_cell
                    + self._styled("border", self._glyph("│", "|"))
                    + right_cell
                    + self._styled("border", self._glyph("│", "|"))
                )
            bottom = self._styled(
                "border",
                self._glyph("╰", "+")
                + self._glyph("─", "-") * inner
                + self._glyph("╯", "+"),
            )
            self._writeln(top)
            for line in body_lines:
                self._writeln(line)
            self._writeln(bottom)

    def status(self, message: str, *, level: RenderLevel = RenderLevel.INFO) -> None:
        with self.lock:
            self.ensure_newline()
            safe_message = sanitize_terminal_metadata(message)
            if not self.claude_like:
                self._writeln(f"System: {safe_message}")
            else:
                glyphs = {
                    RenderLevel.INFO: ("◇", "*", "system"),
                    RenderLevel.PROGRESS: ("◇", "*", "system"),
                    RenderLevel.SUCCESS: ("✓", "+", "success"),
                    RenderLevel.WARNING: ("⚠", "!", "warning"),
                    RenderLevel.ERROR: ("✘", "x", "error"),
                    RenderLevel.DETAIL: ("·", "-", "inactive"),
                }
                unicode_glyph, ascii_glyph, role = glyphs[level]
                glyph = self._styled(role, self._glyph(unicode_glyph, ascii_glyph))
                self._writeln(f"{glyph} {safe_message}")
            self.epoch += 1

    def assistant_delta(
        self,
        delta: str,
        *,
        prefix: bool,
        continuation: bool = False,
    ) -> None:
        with self.lock:
            safe_delta = sanitize_terminal_content(delta)
            if not safe_delta:
                return
            if prefix:
                self._assistant_transcript = []
                self.ensure_newline()
                marker = (
                    self._styled("brand", self._glyph("●", "*")) + " "
                    if self.claude_like
                    else "Codex: "
                )
                self._write(marker)
            elif continuation:
                if self.line_open:
                    self._writeln()
                if self.claude_like:
                    self._write("  ")
            elif (
                self.claude_like
                and not self.line_open
                and not safe_delta.startswith("\n")
            ):
                self._write("  ")
            self._write(safe_delta)
            self._assistant_transcript.append(safe_delta)
            self.line_open = not safe_delta.endswith("\n")

    def end_assistant(self) -> None:
        with self.lock:
            self.ensure_newline()
            if self._assistant_transcript:
                value = "".join(self._assistant_transcript).strip()
                if value:
                    self.record_transcript("● " + value)
                self._assistant_transcript = []

    def tool_started(self, presentation: ToolPresentation) -> None:
        with self.lock:
            self.ensure_newline()
            kind = sanitize_terminal_metadata(presentation.kind)
            label = sanitize_terminal_metadata(presentation.label)
            safe_detail = sanitize_terminal_metadata(presentation.detail)
            if not self.claude_like:
                self._writeln(f"  tool: {kind}  {safe_detail}")
            else:
                raw_marker = self._glyph("↳", ">")
                label = self._fit(
                    label,
                    max(1, self._border_width() - display_width(f"  {raw_marker} ")),
                )
                base_width = display_width(f"  {raw_marker} {label}")
                detail_width = max(0, self._border_width() - base_width - 3)
                detail = self._fit(safe_detail, detail_width)
                separator = self._glyph("·", "|")
                suffix = f" {separator} {detail}" if detail else ""
                marker = self._styled("system", raw_marker)
                self._writeln(f"  {marker} {label}{suffix}")
            self.epoch += 1

    def tool_completed(self, presentation: ToolPresentation) -> None:
        with self.lock:
            self.ensure_newline()
            kind = sanitize_terminal_metadata(presentation.kind)
            label = sanitize_terminal_metadata(presentation.label)
            safe_detail = sanitize_terminal_metadata(presentation.detail)
            if not self.claude_like:
                self._writeln(f"  done: {kind}  {safe_detail}")
            else:
                role = "success" if presentation.success is not False else "error"
                raw_marker = self._glyph("⎿", "-")
                marker = self._styled(role, raw_marker)
                label = self._fit(
                    label,
                    max(1, self._border_width() - display_width(f"  {raw_marker} ")),
                )
                base_width = display_width(f"  {raw_marker} {label}")
                detail_width = max(0, self._border_width() - base_width - 3)
                detail = self._fit(
                    safe_detail,
                    detail_width,
                )
                separator = self._glyph("·", "|")
                suffix = f" {separator} {detail}" if detail else ""
                self._writeln(f"  {marker} {label}{suffix}")
            self.epoch += 1

    def output_block(self, value: str) -> None:
        with self.lock:
            self.ensure_newline()
            safe_value = sanitize_terminal_content(value).rstrip()
            if safe_value:
                self._writeln(safe_value)
            self.epoch += 1

    def clear_screen(self) -> None:
        with self.lock:
            self.ensure_newline()
            if self.claude_like:
                self._write("\x1b[3J\x1b[2J\x1b[H")
            self.epoch += 1

    def _safe_footer(self, footer: str | Callable[[], str]) -> str:
        width = self._border_width()
        footer_width = max(1, width - 2)
        raw_footer = footer() if callable(footer) else footer
        clean_footer = sanitize_terminal_content(raw_footer).replace("\n", " ")
        return (
            clean_footer
            if display_width(clean_footer) <= footer_width
            else self._fit(clean_footer, footer_width)
        )

    def _prompt_with_toolkit(
        self,
        footer: str | Callable[[], str],
        on_tool_details_toggle: Callable[[], None] | None,
        default: str = "",
        *,
        commands: tuple[tuple[str, str], ...] = SLASH_COMMANDS,
        files: tuple[str, ...] = (),
        right_hint: str | Callable[[], str] = "",
        tasks: Callable[[], list[str]] | None = None,
    ) -> str | None:
        if (
            not self._native_stream
            or not self.claude_like
            or not bool(getattr(sys.stdin, "isatty", lambda: False)())
        ):
            return None
        try:
            if self._claude_ui is None:
                self._claude_ui = ClaudePromptUI()
            footer_callback = (
                lambda: self._safe_footer(footer)
                if not callable(footer)
                else self._safe_footer(footer)
            )
            hint_callback = (
                right_hint if callable(right_hint) else lambda: str(right_hint)
            )
            result = self._claude_ui.run(
                footer=footer_callback,
                right_hint=hint_callback,
                default=default,
                commands=commands,
                files=files,
                tasks=tasks,
            )
            self._action_draft = self._claude_ui.action_draft
            return result
        except (ImportError, OSError):
            return None

    def prompt(
        self,
        footer: str | Callable[[], str],
        *,
        on_tool_details_toggle: Callable[[], None] | None = None,
        default: str = "",
        commands: tuple[tuple[str, str], ...] = SLASH_COMMANDS,
        files: tuple[str, ...] = (),
        right_hint: str | Callable[[], str] = "",
        tasks: Callable[[], list[str]] | None = None,
    ) -> str:
        with self.lock:
            self.ensure_newline()
            if not self.claude_like:
                entered = input("\nYou: ")
                return entered if entered else default
            width = self._border_width()
            safe_footer = self._safe_footer(footer)
            if default:
                toolkit_value = self._prompt_with_toolkit(
                    footer,
                    on_tool_details_toggle,
                    default,
                    commands=commands,
                    files=files,
                    right_hint=right_hint,
                    tasks=tasks,
                )
            else:
                toolkit_value = self._prompt_with_toolkit(
                    footer,
                    on_tool_details_toggle,
                    commands=commands,
                    files=files,
                    right_hint=right_hint,
                    tasks=tasks,
                )
            if toolkit_value is not None:
                if toolkit_value in {
                    REWIND_TOKEN,
                    TRANSCRIPT_TOKEN,
                    MODEL_TOKEN,
                    FAST_TOKEN,
                    PERMISSIONS_TOKEN,
                    EFFORT_TOKEN,
                    EDITOR_TOKEN,
                    PASTE_IMAGE_TOKEN,
                    TASKS_TOKEN,
                    EXIT_TOKEN,
                }:
                    return toolkit_value
                submitted = sanitize_terminal_content(toolkit_value)
                lines = submitted.splitlines() or [""]
                marker = self._styled("brand", ">")
                self._writeln(f"{marker} {lines[0]}")
                for line in lines[1:]:
                    self._writeln(f"  {line}")
                return toolkit_value
            border = self._styled(
                "border",
                self._glyph("─", "-") * width,
            )
            self._writeln()
            self._writeln(border)
            prompt_marker = self._styled("brand", ">") + " "
            try:
                return input(prompt_marker)
            finally:
                self._writeln(border)
                if footer:
                    self._writeln("  " + self._styled("inactive", safe_footer))

    def choice_prompt(self) -> str:
        if not self.claude_like:
            return "You: "
        return self._styled("brand", ">") + " "

    def take_action_draft(self) -> str:
        draft = self._action_draft
        self._action_draft = ""
        return draft

    def take_rewind_draft(self) -> str:
        return self.take_action_draft()

    def select_menu(
        self,
        title: str,
        options: list[tuple[str, str]],
        *,
        subtitle: str = "",
        initial_index: int = 0,
        efforts: list[list[str]] | None = None,
        initial_effort: str | None = None,
        allow_session_only: bool = False,
    ) -> MenuSelection | None:
        if not self._native_stream or not self.claude_like:
            return None
        try:
            if self._claude_ui is None:
                self._claude_ui = ClaudePromptUI()
            return self._claude_ui.select(
                title=title,
                subtitle=subtitle,
                options=options,
                initial_index=initial_index,
                efforts=efforts,
                initial_effort=initial_effort,
                allow_session_only=allow_session_only,
            )
        except (ImportError, OSError):
            return None

    def select_fast_mode(self, *, enabled: bool, model_label: str) -> bool | None:
        if not self._native_stream or not self.claude_like:
            return None
        try:
            if self._claude_ui is None:
                self._claude_ui = ClaudePromptUI()
            return self._claude_ui.fast_mode(enabled=enabled, model_label=model_label)
        except (ImportError, OSError):
            return None

    def show_transcript(self, value: str) -> None:
        if not self._native_stream or not self.claude_like:
            self.output_block(value)
            return
        try:
            if self._claude_ui is None:
                self._claude_ui = ClaudePromptUI()
            self._claude_ui.transcript(value)
        except (ImportError, OSError, RuntimeError, TypeError, ValueError):
            self.output_block(value)

    def transcript_text(self) -> str:
        return "\n".join(self._transcript[-4000:])

    def reset_conversation_view(self, *, clear_input_history: bool) -> None:
        self._transcript = []
        self._assistant_transcript = []
        self._action_draft = ""
        if clear_input_history and self._claude_ui is not None:
            self._claude_ui.reset_history()

    def question(
        self,
        label: str,
        options: list[dict[str, Any]],
        *,
        heading: bool = True,
    ) -> None:
        with self.lock:
            self.ensure_newline()
            safe_label = sanitize_terminal_metadata(label)
            if not self.claude_like:
                if heading:
                    self._writeln("Your choice is required:")
                self._writeln(safe_label)
                for index, option in enumerate(options, start=1):
                    option_label = sanitize_terminal_metadata(
                        str(option.get("label", ""))
                    )
                    description = sanitize_terminal_metadata(
                        str(option.get("description", ""))
                    )
                    separator = self._glyph("—", "-")
                    self._writeln(
                        f"  {index}. {option_label} {separator} {description}"
                    )
            else:
                marker = self._styled("system", "?")
                self._writeln(f"{marker} {safe_label}")
                for index, option in enumerate(options, start=1):
                    option_label = sanitize_terminal_metadata(str(option.get("label", "")))
                    description = sanitize_terminal_metadata(
                        str(option.get("description", ""))
                    )
                    self._writeln(f"  {index}. {option_label}")
                    if description:
                        self._writeln("     " + self._styled("inactive", description))
            self.epoch += 1

    def auto_choice(self, value: str, *, grace_ms: int | None = None) -> None:
        with self.lock:
            if not self.claude_like:
                suffix = f" ({grace_ms}ms)" if grace_ms is not None else ""
                self._writeln(
                    f"  Automatically selected{suffix}: {sanitize_terminal_metadata(value)}"
                )
            else:
                marker = self._styled("system", self._glyph("⎿", "-"))
                separator = self._glyph("·", "|")
                self._writeln(
                    f"  {marker} Default {separator} "
                    f"{sanitize_terminal_metadata(value)}"
                )

    def help(self) -> None:
        with self.lock:
            self.ensure_newline()
            if not self.claude_like:
                self._writeln(
                    "Commands: /model  /fast  /status  /new  /rewind  /exit"
                )
                return
            self._writeln(
                "  ! for shell mode        double tap esc to clear input      ctrl + shift + _ to undo"
            )
            self._writeln(
                "  / for commands          shift + tab to auto-accept edits   alt + v to paste images"
            )
            self._writeln(
                "  @ for file paths        ctrl + o to open transcript        alt + p to switch model"
            )
            self._writeln(
                "  /btw for side question  ctrl + t to toggle tasks           ctrl + s to stash prompt"
            )
            self._writeln(
                "                          shift + enter for newline          ctrl + g to edit in $EDITOR"
            )
            self._writeln(
                "                                                             /keybindings to customize"
            )


@dataclass(slots=True)
class TurnOutcome:
    final_response: str
    usage: Any | None
    compacted: bool = False
    interrupted: bool = False
    error_message: str | None = None
    error_code: str | None = None
    side_effects: bool = False
    rollover_signal: str | None = None
    guardian_checkpointed: bool = False
    guardian_audited: bool = False
    guardian_finished: bool = False
    tool_activity: bool = False
    turn_id: str | None = None
    guardian_finish_kind: str | None = None
    policy_notice_published: bool = False


class _TerminalPolicyBoundary(RuntimeError):
    """Carry a structured platform boundary out of a nested control turn."""

    def __init__(self, outcome: TurnOutcome) -> None:
        super().__init__("structured platform boundary")
        self.outcome = outcome


@dataclass(frozen=True, slots=True)
class SlashCommandOutcome:
    handled: bool
    exit_requested: bool = False
    prefill: str | None = None
    turn_prompt: str | None = None
    turn_handle: Any | None = None


@dataclass(slots=True)
class PendingUserInput:
    params: dict[str, Any]
    completed: threading.Event = field(default_factory=threading.Event)
    response: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class GuardianTarget:
    workspace_root: Path
    project_id: str
    contextctl: Path


@dataclass(frozen=True, slots=True)
class GuardianFinishCandidate:
    old_task_id: str
    target_task_id: str
    handoff_sha256: str


@dataclass(frozen=True, slots=True)
class GuardianRuntimeIdentity:
    task_id: str
    state_sha256: str
    rules_sha256: str
    audit_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class RolloverJournal:
    transaction_id: str
    project_id: str
    workspace_root: str
    project_root: str
    session_cwd: str
    generation: int
    source_task_id: str
    source_state_sha256: str
    source_rules_sha256: str
    source_audit_sha256: str
    phase: str
    target_task_id: str | None = None
    target_state_sha256: str | None = None
    target_rules_sha256: str | None = None
    handoff_sha256: str | None = None
    final_response_sha256: str | None = None
    created_at: str = ""
    updated_at: str = ""

    @property
    def thread_source(self) -> str:
        return ROLLOVER_THREAD_SOURCE_PREFIX + self.transaction_id

    def as_dict(self) -> dict[str, Any]:
        return {
            field_name: getattr(self, field_name)
            for field_name in self.__dataclass_fields__
        } | {"schema_version": ROLLOVER_JOURNAL_SCHEMA_VERSION}


def _valid_task_identity(value: Any) -> bool:
    return bool(
        isinstance(value, str)
        and re.fullmatch(r"[A-Za-z0-9._-]{1,128}", value)
    )


def _require_sha256(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise RuntimeError(f"rollover journal {field_name} is invalid")
    return value


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _next_timestamp(previous: str) -> str:
    prior = dt.datetime.fromisoformat(previous)
    current = dt.datetime.now(dt.timezone.utc)
    if current <= prior:
        current = prior + dt.timedelta(microseconds=1)
    return current.isoformat()


def _canonical_path(value: Path | str) -> str:
    return os.path.normcase(str(Path(value).resolve()))


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    fd, raw_temp = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temp_path = Path(raw_temp)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def validate_rollover_journal(
    raw: Any,
    *,
    expected_project_id: str,
    expected_workspace_root: Path,
    expected_project_root: Path,
) -> RolloverJournal:
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise RuntimeError("rollover journal schema is invalid")
    expected_keys = set(RolloverJournal.__dataclass_fields__) | {"schema_version"}
    if set(raw) != expected_keys:
        raise RuntimeError("rollover journal fields are invalid")
    transaction_id = raw.get("transaction_id")
    try:
        parsed_transaction_id = uuid.UUID(str(transaction_id))
    except (ValueError, AttributeError) as exc:
        raise RuntimeError("rollover journal transaction identity is invalid") from exc
    if str(parsed_transaction_id) != transaction_id:
        raise RuntimeError("rollover journal transaction identity is not canonical")
    if raw.get("project_id") != expected_project_id:
        raise RuntimeError("rollover journal project identity is invalid")
    if raw.get("workspace_root") != _canonical_path(expected_workspace_root):
        raise RuntimeError("rollover journal workspace identity is invalid")
    if raw.get("project_root") != _canonical_path(expected_project_root):
        raise RuntimeError("rollover journal registered project identity is invalid")
    session_cwd = raw.get("session_cwd")
    if not isinstance(session_cwd, str) or session_cwd != _canonical_path(session_cwd):
        raise RuntimeError("rollover journal session cwd identity is invalid")
    registered_root = Path(expected_project_root).resolve()
    resolved_session_cwd = Path(session_cwd).resolve()
    if (
        resolved_session_cwd != registered_root
        and registered_root not in resolved_session_cwd.parents
    ):
        raise RuntimeError("rollover journal session cwd escapes the registered project")
    source_task_id = raw.get("source_task_id")
    target_task_id = raw.get("target_task_id")
    if not _valid_task_identity(source_task_id):
        raise RuntimeError("rollover journal source task identity is invalid")
    phase = raw.get("phase")
    if phase not in ROLLOVER_JOURNAL_PHASES:
        raise RuntimeError("rollover journal phase is invalid")
    phase_index = ROLLOVER_JOURNAL_PHASES.index(phase)
    generation = raw.get("generation")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
        raise RuntimeError("rollover journal generation is invalid")
    if phase_index == 0:
        if target_task_id is not None:
            raise RuntimeError("prepared rollover journal already names a target")
    elif (
        not _valid_task_identity(target_task_id)
        or target_task_id == source_task_id
    ):
        raise RuntimeError("rollover journal target task identity is invalid")
    target_state = raw.get("target_state_sha256")
    target_rules = raw.get("target_rules_sha256")
    handoff_sha = raw.get("handoff_sha256")
    if phase_index >= 1:
        _require_sha256(target_state, "target_state_sha256")
        _require_sha256(target_rules, "target_rules_sha256")
        _require_sha256(handoff_sha, "handoff_sha256")
    elif any(value is not None for value in (target_state, target_rules, handoff_sha)):
        raise RuntimeError("prepared rollover journal contains target evidence")
    final_response_sha = raw.get("final_response_sha256")
    if phase == "completion_ready":
        _require_sha256(final_response_sha, "final_response_sha256")
    elif final_response_sha is not None:
        raise RuntimeError("active rollover journal contains terminal response evidence")
    for field_name in ("created_at", "updated_at"):
        try:
            parsed = dt.datetime.fromisoformat(str(raw.get(field_name)))
        except ValueError as exc:
            raise RuntimeError(f"rollover journal {field_name} is invalid") from exc
        if parsed.tzinfo is None:
            raise RuntimeError(f"rollover journal {field_name} has no timezone")
    return RolloverJournal(
        transaction_id=str(transaction_id),
        project_id=expected_project_id,
        workspace_root=str(raw["workspace_root"]),
        project_root=str(raw["project_root"]),
        session_cwd=str(session_cwd),
        generation=generation,
        source_task_id=str(source_task_id),
        source_state_sha256=_require_sha256(
            raw.get("source_state_sha256"), "source_state_sha256"
        ),
        source_rules_sha256=_require_sha256(
            raw.get("source_rules_sha256"), "source_rules_sha256"
        ),
        source_audit_sha256=_require_sha256(
            raw.get("source_audit_sha256"), "source_audit_sha256"
        ),
        phase=str(phase),
        target_task_id=str(target_task_id) if target_task_id is not None else None,
        target_state_sha256=str(target_state) if target_state is not None else None,
        target_rules_sha256=str(target_rules) if target_rules is not None else None,
        handoff_sha256=str(handoff_sha) if handoff_sha is not None else None,
        final_response_sha256=(
            str(final_response_sha) if final_response_sha is not None else None
        ),
        created_at=str(raw["created_at"]),
        updated_at=str(raw["updated_at"]),
    )


class RolloverJournalLease:
    """Process-scoped single writer lock; the OS releases it after a crash."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._stream = path.open("a+b")
        if os.fstat(self._stream.fileno()).st_size == 0:
            self._stream.seek(0)
            self._stream.write(b"1")
            self._stream.flush()
        self._stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            self._stream.close()
            raise RuntimeError(
                "another codex-continuous controller owns this Guardian project"
            ) from exc

    def close(self) -> None:
        stream = getattr(self, "_stream", None)
        if stream is None or stream.closed:
            return
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()


def trim_text(value: str | None, limit: int = MAX_HANDOFF_FIELD_CHARS) -> str:
    text = (value or "").strip()
    if len(text) <= limit:
        return text
    head = max(1, limit * 2 // 3)
    tail = max(1, limit - head - 32)
    return text[:head] + "\n…[bounded handoff]…\n" + text[-tail:]


def auto_input_grace_ms(requested_ms: int) -> int:
    configured = os.environ.get("CODEX_CONTINUOUS_INPUT_GRACE_MS")
    try:
        grace = int(configured) if configured is not None else DEFAULT_AUTO_INPUT_GRACE_MS
    except ValueError:
        grace = DEFAULT_AUTO_INPUT_GRACE_MS
    return max(0, min(requested_ms, grace))


def console_input_with_timeout(prompt: str, timeout_ms: int) -> str | None:
    if timeout_ms <= 0 or os.name != "nt" or not sys.stdin.isatty():
        return None
    try:
        import msvcrt
    except ImportError:
        return None
    print(prompt, end="", flush=True)
    characters: list[str] = []
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if not msvcrt.kbhit():
            time.sleep(0.05)
            continue
        character = msvcrt.getwch()
        if character in {"\r", "\n"}:
            print(flush=True)
            return "".join(characters).strip()
        if character == "\x03":
            raise KeyboardInterrupt
        if character == "\x14":
            self._toggle_todos()
            return False
        if character == "\b":
            if characters:
                characters.pop()
                print("\b \b", end="", flush=True)
            continue
        if character in {"\x00", "\xe0"}:
            if msvcrt.kbhit():
                msvcrt.getwch()
            continue
        characters.append(character)
        print(character, end="", flush=True)
    print(flush=True)
    return None


def is_generic_content_block(value: str | None) -> bool:
    lowered = (value or "").strip().casefold()
    if any(lowered.startswith(marker) for marker in GENERIC_POLICY_BLOCK_MARKERS):
        return True
    starts_with_heading = any(lowered.startswith(marker) for marker in GENERIC_BLOCK_MARKERS)
    return starts_with_heading and (
        GENERIC_BLOCK_CAUTION in lowered or lowered in GENERIC_BLOCK_MARKERS
    )


def is_cyber_policy_code(value: str | None) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "", (value or "").casefold())
    return normalized == "cyberpolicy"


def is_policy_content_block(value: str | None) -> bool:
    lowered = (value or "").strip().casefold()
    if any(lowered.startswith(marker) for marker in GENERIC_POLICY_BLOCK_MARKERS):
        return True
    return (
        any(lowered.startswith(marker) for marker in GENERIC_BLOCK_MARKERS)
        and GENERIC_BLOCK_CAUTION in lowered
    )


def outcome_has_policy_boundary(outcome: TurnOutcome) -> bool:
    return is_cyber_policy_code(outcome.error_code) or any(
        is_policy_content_block(value)
        for value in (outcome.final_response, outcome.error_message)
        if value
    )


def outcome_has_generic_block(outcome: TurnOutcome) -> bool:
    return is_cyber_policy_code(outcome.error_code) or any(
        is_generic_content_block(value)
        for value in (outcome.final_response, outcome.error_message)
        if value
    )


def assistant_requests_manual_fresh_thread(value: str | None) -> bool:
    """Narrow failsafe for a model that hands controller work back to the user."""
    text = re.sub(r"\s+", " ", (value or "").strip()).casefold()
    if not text:
        return False
    closure = any(
        marker in text
        for marker in (
            "\u820a\u5c0d\u8a71\u5df2\u6b63\u5f0f\u6536\u5c3e",
            "\u820a\u5c0d\u8a71\u5df2\u6536\u5c3e",
            "\u820a\u804a\u5929\u5df2\u7d50\u675f",
            "old conversation is complete",
            "old thread is complete",
            "old chat is complete",
        )
    )
    first_person_boundary = any(
        marker in text
        for marker in (
            "\u6211\u7121\u6cd5\u66ff\u4f60",
            "\u6211\u4e0d\u80fd\u66ff\u4f60",
            "i can't create",
            "i cannot create",
            "i'm unable to create",
        )
    )
    manual_action = any(
        marker in text
        for marker in (
            "\u8acb\u6309\u300c\u65b0\u589e\u5c0d\u8a71\u300d",
            "\u8acb\u6309\u65b0\u589e\u5c0d\u8a71",
            "\u8acb\u958b\u555f\u65b0\u5c0d\u8a71",
            "\u8acb\u8f38\u5165 /new",
            "please click new chat",
            "please start a new chat",
            "please type /new",
        )
    )
    return closure and first_person_boundary and manual_action


def assistant_reports_rollover_status_only(value: str | None) -> bool:
    """Detect a handoff receipt that promises future work but performs none.

    A successful fresh-thread transition is infrastructure, not completion of
    the user's project objective.  Keep this deliberately narrower than a
    generic "unfinished" classifier so ordinary completion summaries are not
    turned into loops.
    """
    text = re.sub(r"\s+", " ", (value or "").strip()).casefold()
    if not text:
        return False
    transition = any(
        marker in text
        for marker in (
            "\u5df2\u6210\u529f\u81ea\u52d5\u7e8c\u63a5",
            "\u5df2\u81ea\u52d5\u7e8c\u63a5",
            "\u6210\u529f\u7e8c\u63a5\u5c08\u6848",
            "\u65b0 task",
            "\u65b0\u5c0d\u8a71",
            "\u820a task",
            "checkpoint sha",
            "fresh task",
            "fresh thread",
            "automatic rollover",
            "automatic handoff",
            "handoff complete",
        )
    )
    promised_work = any(
        marker in text
        for marker in (
            "\u6b63\u5f9e\u4e2d\u65b7\u9ede\u7e7c\u7e8c",
            "\u6b63\u5f9e\u4e2d\u65b7\u9ede\u63a5\u7e8c",
            "\u6b63\u5728\u7e7c\u7e8c",
            "\u63a5\u4e0b\u4f86",
            "\u5148\u5b8c\u6210",
            "\u518d\u8655\u7406",
            "\u5c07\u7e7c\u7e8c",
            "will continue",
            "is continuing",
            "continues from",
            "next action",
            "then handle",
        )
    )
    return transition and promised_work


def _completion_semantic_text(value: str | None) -> str:
    """Normalize prose while masking only bounded completion metadata."""

    text = unicodedata.normalize("NFKC", (value or "").strip())
    text = re.sub(r"\s+", " ", text).casefold()
    if not text:
        return ""
    for pattern, replacement in {
        r"\bhaven['’]t\b": "have not",
        r"\bhasn['’]t\b": "has not",
        r"\bhadn['’]t\b": "had not",
        r"\bisn['’]t\b": "is not",
        r"\baren['’]t\b": "are not",
        r"\bwasn['’]t\b": "was not",
        r"\bweren['’]t\b": "were not",
        r"\bdidn['’]t\b": "did not",
        r"\bdoesn['’]t\b": "does not",
        r"\bdon['’]t\b": "do not",
        r"\bi['’]ll\b": "i will",
        r"\bwe['’]ll\b": "we will",
    }.items():
        text = re.sub(pattern, replacement, text)

    quote = r'(?:"[^"\n]{1,180}"|`[^`\n]{1,180}`|\u300c[^\u300d\n]{1,180}\u300d|“[^”\n]{1,180}”)'
    for metadata in (
        rf"(?:regression (?:coverage|test|case)|classifier (?:coverage|case)|"
        rf"verified the phrase|test named)[^.;!?\u3002\uff1b\n]{{0,80}}{quote}",
        rf"{quote}\s+(?:false[- ]termination|regression|classifier)\s+"
        r"(?:case|test|coverage)[^.;!?\u3002\uff1b\n]{0,80}(?:fixed|passes|covered)",
        rf"(?:\u5df2)?(?:\u4fee\u5fa9|\u4fee\u6b63|\u65b0\u589e|\u52a0\u5165)[^\u3002\uff1b;!?\n]{{0,60}}{quote}"
        r"[^\u3002\uff1b;!?\n]{0,60}(?:\u554f\u984c|\u56de\u6b78\u6e2c\u8a66|\u6e2c\u8a66|\u6848\u4f8b)",
        r"[^\u3002\uff1b;.!?\uff01\uff1f\n]{0,140}"
        r"(?:\u7684\s*(?:\u6b63\u4f8b|\u53cd\u4f8b)|\u56de\u6b78(?:\u5b57\u4e32|\u6e2c\u8a66|\u6848\u4f8b)|\u5206\u985e\u5668\u6848\u4f8b)"
        r"[^\u3002\uff1b;.!?\uff01\uff1f\n]{0,80}(?:\u4ecd|\u4ecd\u7136|\u4f9d\u7136)?(?:\u6b63\u78ba)?(?:\u88ab)?"
        r"(?:\u963b\u64cb|\u6514\u622a|\u8bc6\u522b|\u8b58\u5225|\u8fa8\u8b58|\u5206\u985e|\u901a\u8fc7|\u901a\u904e)",
        r"[^.;!?\u3002\uff1b\uff01\uff1f\n]{0,140}\b"
        r"(?:positive|negative|regression|classifier)\b"
        r"[^.;!?\u3002\uff1b\uff01\uff1f\n]{0,80}\b(?:cases?|examples?|strings?)\b"
        r"[^.;!?\u3002\uff1b\uff01\uff1f\n]{0,80}\b(?:are\s+|remain\s+)?(?:still\s+)?"
        r"(?:correctly\s+)?(?:blocked|rejected|detected|classified|passing)\b",
    ):
        text = re.sub(metadata, "", text)

    # A hook trust contract describes third-party runtime behavior, not a
    # promise to perform project work after this turn.  Keep this narrow so a
    # real future action such as "\u5b8c\u6210\u5f8c\u624d\u6703\u57f7\u884c\u90e8\u7f72" remains contradictory.
    text = re.sub(
        r"(?:an?\s+)?(?:(?:non[- ]managed|\u975e\s+managed)\s+)?hooks?"
        r"[^\u3002\uff1b;.!?\uff01\uff1f\n]{0,100}"
        r"(?:must\s+be\s+trusted\s+before\s+(?:it|they)\s+"
        r"(?:will\s+)?(?:run|execute)s?"
        r"|\u5fc5\u9808(?:\u5148)?(?:\u7d93|\u7ecf|\u7d93\u904e|\u7ecf\u8fc7)?\s*\u4fe1\u4efb(?:\u4e4b)?\u5f8c"
        r"\u624d\s*[\u6703\u4f1a]?\s*(?:\u57f7\u884c|\u6267\u884c))",
        "",
        text,
    )

    # A completed response must disclose validation scope honestly without that
    # disclosure becoming a promise to perform more project work.  Mask only a
    # bounded live/production validation clause with no future obligation in
    # the same clause.  Explicit follow-up language remains fail-closed, and a
    # later clause such as "the audit still needs work" remains visible.
    validation_disclosure = re.compile(
        r"(?:\u672a\u9a57\u8b49|\u672a\u9a8c\u8bc1)\s*(?:\u7684\s*)?(?:live\s*)?(?:\u884c\u70ba|\u884c\u4e3a)"
        r"|(?:live\s*)?(?:\u884c\u70ba|\u884c\u4e3a)[^\u3002\uff1b;.!?\uff01\uff1f\n]{0,24}"
        r"(?:\u5c1a\u672a|\u672a)\s*(?:\u7d93|\u7ecf)?(?:\u9a57\u8b49|\u9a8c\u8bc1)"
        r"|(?:\u672a\u57f7\u884c|\u672a\u6267\u884c|\u672a\u9032\u884c|\u672a\u8fdb\u884c)"
        r"[^\u3002\uff1b;.!?\uff01\uff1f\n]{0,100}"
        r"(?:live|production|app server|device|\u771f\u6a5f|\u5b9e\u673a|\u5be6\u6a5f|\u88dd\u7f6e|\u8bbe\u5907)"
        r"[^\u3002\uff1b;.!?\uff01\uff1f\n]{0,80}"
        r"|\b(?:live|production|device|end[- ]to[- ]end|app server)\b"
        r"[^\u3002\uff1b;.!?\uff01\uff1f\n]{0,80}\b"
        r"(?:(?:was|were|is|are|has|have)\s+)?not\s+"
        r"(?:run|performed|tested|verified|validated|exercised)\b"
        r"|\bnot\s+(?:run|performed|tested|verified|validated|exercised)\s+"
        r"[^\u3002\uff1b;.!?\uff01\uff1f\n]{0,50}\b"
        r"(?:live|production|device|end[- ]to[- ]end|app server)\b"
        r"|\bunverified\s+(?:live|production|device)\b"
    )
    required_followup = re.compile(
        r"(?:\u4ecd|[\u9084\u8fd8])\s*(?:\u9700|\u9700\u8981|\u5fc5\u9808|\u5fc5\u987b|\u5f85)"
        r"|(?:\u9700\u8981|\u5fc5\u9808|\u5fc5\u987b|\u5c1a\u5f85|\u5f85)\s*"
        r"(?:\u57f7\u884c|\u6267\u884c|\u9032\u884c|\u8fdb\u884c|\u9a57\u8b49|\u9a8c\u8bc1|\u6e2c\u8a66|\u6d4b\u8bd5|\u88dc\u505a|\u5b8c\u6210)"
        r"|(?:\u5c07|\u5c06|\u6703|\u4f1a|\u4e0b\u4e00\u6b65|\u4e0b\u4e00\u8f2a|\u4e0b\u4e00\u8f6e|\u7a0d\u5f8c|\u7a0d\u540e|\u5f8c\u7e8c|\u540e\u7eed)"
        r"[^\u3002\uff1b;.!?\uff01\uff1f\n]{0,80}(?:\u57f7\u884c|\u6267\u884c|\u9a57\u8b49|\u9a8c\u8bc1|\u6e2c\u8a66|\u6d4b\u8bd5|\u88dc\u505a|\u5b8c\u6210)"
        r"|(?:\u4ea4\u4ed8|\u767c\u5e03|\u53d1\u5e03|\u90e8\u7f72)\s*\u524d"
        r"|\b(?:must|will|shall|should)\b"
        r"|\b(?:still\s+)?needs?\s+to\b"
        r"|\b(?:is|are|remains?)\s+(?:still\s+)?"
        r"(?:required|needed|pending|unverified|untested)\b"
        r"|\b(?:later|next)\b"
        r"|\bbefore\s+(?:release|deployment|shipping|completion)\b"
    )
    clauses = re.split(r"([\u3002\uff1b;.!?\uff01\uff1f\n]+)", text)
    for index in range(0, len(clauses), 2):
        clause = clauses[index]
        if (
            validation_disclosure.search(clause)
            and not required_followup.search(clause)
        ):
            clauses[index] = ""
    text = "".join(clauses)

    cjk_label = (
        r"(?:\u672a\u5b8c\u6210|\u672a\u8655\u7406|\u672a\u5904\u7406|\u672a\u9a57\u8b49|\u672a\u9a8c\u8bc1|\u672a\u7a3d\u6838|\u672a\u5be9\u8a08|\u672a\u5ba1\u8ba1)"
        r"(?:\s*(?:\u9805\u76ee|\u9879\u76ee|\u5de5\u4f5c|\u4efb\u52d9|\u4efb\u52a1|\u4e8b\u9805|\u4e8b\u9879|\u6e2c\u8a66|\u6d4b\u8bd5|"
        r"(?:live\s*)?(?:\u884c\u70ba|\u884c\u4e3a)))?"
    )
    cjk_none = r"(?:0|\u96f6|\u7121|\u65e0|\u6c92\u6709|\u6ca1\u6709|\u4e0d\u5b58\u5728|none|nil|n/?a)"
    text = re.sub(rf"{cjk_label}\s*(?::|=)?\s*{cjk_none}", "", text)

    cjk_remaining = r"(?:\u5269\u9918|\u5269\u4f59|\u9918\u4e0b|\u4f59\u4e0b)"
    cjk_work = (
        r"(?:\u5de5\u4f5c|\u9805\u76ee|\u9879\u76ee|\u4efb\u52d9|\u4efb\u52a1|\u4e8b\u9805|\u4e8b\u9879|\u9a57\u8b49|\u9a8c\u8bc1|\u7a3d\u6838|\u5be9\u8a08|"
        r"\u5ba1\u8ba1|\u6e2c\u8a66|\u6d4b\u8bd5|\u4fee\u5fa9|\u4fee\u590d)"
    )
    cjk_absence = r"(?:\u6c92\u6709|\u6ca1\u6709|\u7121|\u65e0|\u4e0d\u5b58\u5728)"
    for no_remaining in (
        rf"{cjk_absence}\s*(?:\u4efb\u4f55)?\s*{cjk_remaining}\s*{cjk_work}",
        rf"{cjk_absence}\s*(?:\u4efb\u4f55)?\s*{cjk_work}\s*{cjk_remaining}",
        rf"{cjk_remaining}\s*{cjk_work}\s*(?::|=)?\s*{cjk_none}",
    ):
        text = re.sub(no_remaining, "", text)

    english_work = (
        r"(?:work|tasks?|items?|verification|audit|testing|tests?|fix(?:es)?|"
        r"implementation)"
    )
    english_state = r"(?:remaining|outstanding|unfinished|incomplete)"
    english_none = r"(?:0|zero|none|nil|n/?a|nothing)"
    for zero_counter in (
        rf"\b{english_state}\s+{english_work}\s*(?::|=|is|are)?\s*{english_none}\b",
        rf"\b{english_none}\s+{english_state}\s+{english_work}\b",
        rf"\b{english_work}\s+{english_state}\s*(?::|=|is|are)?\s*{english_none}\b",
        rf"\b(?:0|zero)\s+{english_work}\s+remains?\b",
        rf"\b(?:there\s+(?:is|are)\s+)?no\s+(?:further\s+|additional\s+)?"
        rf"{english_work}\s+remains?\b",
        rf"\bnone\s+of\s+(?:the\s+)?{english_work}\s+remains?\b",
        r"\bnothing\s+remains?\s+to\s+(?:be\s+)?"
        r"(?:completed|done|finished|run|verified|audited|tested|fixed|"
        r"implemented|handled)\b",
    ):
        text = re.sub(zero_counter, "", text)

    # Guardian completion summaries may quote their structured state sections
    # literally.  Mask only an explicitly empty Next Actions status so the
    # future-work detector cannot combine its word "next" with a later report
    # that ``finish`` passed.  Populated or unqualified Next Actions remain
    # visible and therefore fail closed.
    structured_actions = (
        r"(?:[`*_]{1,3})?\b"
        r"(?:open(?:\s*/\s*uncertain)?\s*(?:/|and|&|\u8207|\u548c)\s*)?"
        r"next actions?\b(?:[`*_]{1,3})?(?:\s+sections?)?"
    )
    for empty_actions in (
        rf"(?:\u660e\u78ba)?(?:\u70ba|\u4e3a)?(?:\u7a7a|\u7121|\u65e0)\s*\u7684?\s*{structured_actions}",
        rf"\bempty\s+{structured_actions}",
        rf"{structured_actions}\s*(?:\u5747|\u7686|\u90fd|both)?\s*"
        r"(?::|=|is|are|\u70ba|\u4e3a)?\s*(?:\u5747|\u7686|\u90fd|both)?\s*"
        r"(?:empty|none|nil|n/?a|\u7a7a|\u96f6|\[\s*\])"
        r"(?=$|[\s,.;:!?\uff0c\u3002\uff1b\uff1a\uff01\uff1f])",
    ):
        text = re.sub(empty_actions, "", text)

    completion_verb = (
        r"(?:complete|completed|done|finished|verified|audited|tested|fixed|"
        r"implemented|handled|pass|passes|passed)"
    )
    for settled_remaining in (
        rf"\b(?:all\s+)?(?:the\s+)?{english_state}\s+{english_work}\s+"
        rf"(?:(?:is|are|was|were|has|have|had)\s+(?:all\s+)?(?:been\s+)?)?"
        rf"(?:now\s+)?{completion_verb}\b",
        rf"\b{completion_verb}\s+(?:all\s+)?(?:the\s+)?{english_state}\s+"
        rf"{english_work}\b",
        r"(?:\u6240\u6709|\u5168\u90e8)?\s*(?:\u5269\u9918|\u5269\u4f59|\u9918\u4e0b|\u4f59\u4e0b)\s*"
        r"(?:\u5de5\u4f5c|\u9805\u76ee|\u9879\u76ee|\u6e2c\u8a66|\u6d4b\u8bd5|\u7a3d\u6838|\u5be9\u8a08|\u5ba1\u8ba1)"
        r"[^\u3002\uff1b;!?\n]{0,30}(?:\u5df2)?(?:\u5b8c\u6210|\u901a\u904e|\u901a\u8fc7)",
    ):
        text = re.sub(settled_remaining, "", text)
    return text


def assistant_defers_unfinished_work(value: str | None) -> bool:
    """Detect an explicit promise to do required work after this turn.

    This is intentionally semantic rather than tied to one handoff sentence.
    A Guardian ``finish`` receipt is lifecycle evidence, but prose saying that
    work remains is direct evidence that the project objective did not settle.
    """

    text = _completion_semantic_text(value)
    if not text:
        return False
    chinese_unfinished_state = (
        r"(?:(?:\u5c1a|\u4ecd|[\u9084\u8fd8])\s*\u672a|[\u9084\u8fd8]\s*[\u6c92\u6ca1](?:\u6709)?|\u672a)\s*"
        r"(?:(?:\u5168(?:\u90e8|\u6578)?|\u5b8c\u5168|\u771f\u6b63|\u5be6\u969b|\u6b63\u5f0f|\u6700\u7d42|\u5fb9\u5e95|\u5b8c\u6574)\s*)?"
        r"(?:\u5b8c\u6210|\u8655\u7406|\u5904\u7406|\u9a57\u8b49|\u9a8c\u8bc1|\u7a3d\u6838|\u5be9\u8a08|\u5ba1\u8ba1|\u7d50\u675f|\u7ed3\u675f|\u6536\u5c3e)"
    )
    chinese_pending_action = (
        r"(?:(?:\u4ecd|[\u9084\u8fd8])\s*(?:\u9700|\u9700\u8981|\u5f85)|\u5c1a\s*\u5f85|\u5f85)\s*"
        r"(?:\u5b8c\u6210|\u8655\u7406|\u5904\u7406|\u9a57\u8b49|\u9a8c\u8bc1|\u7a3d\u6838|\u5be9\u8a08|\u5ba1\u8ba1|\u57f7\u884c|\u6267\u884c|"
        r"\u88dc(?:\u505a|\u9f4a)?|\u4fee(?:\u6b63|\u5fa9)?|\u5be6\u4f5c|\u5b9e\u73b0|\u6e2c\u8a66|\u6d4b\u8bd5|\u6536\u5c3e)"
    )
    chinese_pending = rf"(?:{chinese_unfinished_state}|{chinese_pending_action})"
    chinese_negator = (
        r"(?:(?:[\u4e26\u5e76]\s*)?(?:\u6c92\u6709|\u6ca1\u6709|\u7121|\u65e0)"
        r"(?:\s*(?:\u4efb\u4f55|\u4e00\u9805|\u4e00\u9879|\u4e00\u500b|\u4e00\u4e2a))?"
        r"|\u4e0d\u5b58\u5728(?:\s*(?:\u4efb\u4f55|\u4e00\u9805|\u4e00\u9879|\u4e00\u500b|\u4e00\u4e2a))?"
        r"|\u672a\s*(?:\u767c\u73fe|\u53d1\u73b0)(?:\s*(?:\u4efb\u4f55|\u4e00\u9805|\u4e00\u9879|\u4e00\u500b|\u4e00\u4e2a))?)"
    )
    # Ignore only a negated pending-work span.  Returning for the whole
    # response would let a later, explicit pending clause bypass the terminal
    # boundary (for example, "\u6c92\u6709\u5c1a\u672a\u5b8c\u6210\u7684\u6e2c\u8a66\uff1b\u7a3d\u6838\u4ecd\u9700\u5b8c\u6210").
    semantic_text = re.sub(
        rf"{chinese_negator}\s*{chinese_pending}",
        "",
        text,
    )
    # English completion summaries need the same clause-local treatment.  In
    # particular, "no remaining work" and "nothing remains pending" must not
    # turn an otherwise valid completed receipt into an infinite continuation
    # loop.  Keep the patterns bounded to the negated clause so a later positive
    # clause ("no remaining tests; the audit still needs work") is retained.
    english_work_noun = (
        r"(?:work|tasks?|items?|verification|audit|testing|tests?|fix(?:es)?|"
        r"implementation)"
    )
    for negated_pending in (
        rf"\b(?:there\s+(?:is|are)\s+)?no\s+"
        rf"(?:remaining|outstanding|unfinished|incomplete)\s+{english_work_noun}\b",
        rf"\bno\s+(?:further|additional)\s+{english_work_noun}\s+"
        r"(?:is|are)\s+(?:required|needed|pending|unfinished|incomplete)\b",
        rf"\b(?:there\s+(?:is|are)\s+)?no\s+{english_work_noun}\s+"
        r"(?:remains?|is|are)\s+(?:pending|unfinished|incomplete)\b",
        rf"\b(?:there\s+(?:is|are)\s+)?no\s+{english_work_noun}\s+"
        r"(?:that\s+)?remains?\s+(?:pending|unfinished|incomplete)\b",
        r"\bnothing\s+remains?\s+(?:pending|unfinished|incomplete)\b",
        rf"\bnone\s+of\s+the\s+{english_work_noun}\s+"
        r"remains?\s+(?:pending|unfinished|incomplete)\b",
        rf"\b{english_work_noun}\s+(?:is|are)\s+not\s+"
        r"(?:pending|unfinished|incomplete)\b",
        r"\b(?:the\s+)?(?:objective|project)\s+(?:is|was)\s+not\s+"
        r"(?:pending|unfinished|incomplete)\b",
    ):
        semantic_text = re.sub(negated_pending, "", semantic_text)
    # Do the same for a bounded, explicitly negated future-work clause.  This
    # avoids treating "\u4e0d\u6703\u518d\u7e8c\u63a5" as promised work without suppressing a
    # later positive clause after punctuation.
    chinese_future_action = (
        r"(?:\u7e7c\u7e8c|\u7ee7\u7eed|\u7e8c\u63a5|\u63a5\u7e8c|\u63a5\u7eed|\u7e8c\u505a|\u5b8c\u6210|\u8655\u7406|\u5904\u7406|\u9a57\u8b49|\u9a8c\u8bc1|"
        r"\u7a3d\u6838|\u5be9\u8a08|\u5ba1\u8ba1|\u57f7\u884c|\u6267\u884c|\u88dc|\u4fee\u6b63|\u4fee\u5fa9|\u5be6\u4f5c|\u5b9e\u73b0|\u6e2c\u8a66|\u6d4b\u8bd5)"
    )
    semantic_text = re.sub(
        rf"(?:\u4e0d|\u7121\u9700|\u65e0\u9700|\u4e0d\u7528)\s*(?:\u5c07|\u5c06|\u6703|\u4f1a)?"
        rf"[^\u3002\uff1b;.!?\uff01\uff1f\n]{{0,80}}{chinese_future_action}",
        "",
        semantic_text,
    )
    english_future_action = (
        r"(?:continue|resume|finish|complete|run|verify|audit|test|fix|"
        r"implement|handle)\w*"
    )
    for negated_future in (
        rf"\b(?:will|would|should)\s+not\s+(?:need\s+to\s+)?"
        rf"{english_future_action}\b",
        rf"\b(?:will|would)\s+no\s+longer\s+{english_future_action}\b",
        rf"\bno\s+(?:further|additional)\s+{english_work_noun}\s+"
        rf"(?:will|needs?\s+to|must)\s+(?:be\s+)?{english_future_action}\b",
    ):
        semantic_text = re.sub(negated_future, "", semantic_text)
    explicitly_unfinished = bool(
        re.search(
            rf"{chinese_pending}"
            r"|(?:\u5269\u9918|\u5269\u4f59|\u9918\u4e0b|\u4f59\u4e0b).{0,80}"
            r"(?:\u5de5\u4f5c|\u9805\u76ee|\u9879\u76ee|\u9a57\u8b49|\u9a8c\u8bc1|\u7a3d\u6838|\u5be9\u8a08|\u5ba1\u8ba1|\u6e2c\u8a66|\u6d4b\u8bd5|\u4fee\u5fa9|\u4fee\u590d)"
            r"|\b(?:not yet (?:complete|completed|done)|unfinished|incomplete)\b"
            r"|\b(?:i|we)\s+(?:have\s+)?not\s+"
            r"(?:complete|completed|finish|finished|run|verify|verified|audit|audited|"
            r"test|tested|fix|fixed|implement|implemented|handle|handled)\b"
            r"|\b(?:have|has|had)\s+not\s+been\s+"
            r"(?:completed|finished|run|verified|audited|tested|fixed|implemented|handled)\b"
            r"|\b(?:did|do|does)\s+not\s+"
            r"(?:complete|finish|run|verify|audit|test|fix|implement|handle)\b"
            r"|\b(?:is|are|was|were)\s+not\s+"
            r"(?:complete|completed|done|finished|verified|audited|tested|fixed|"
            r"implemented|handled)\b"
            r"|\b(?:further|additional)\s+"
            r"(?:work|verification|audit|testing|tests?|fix(?:es)?|implementation)\s+"
            r"(?:is|are)\s+(?:required|needed|pending)\b"
            r"|\b(?:work|verification|audit|testing|tests?|fix(?:es)?|implementation)\s+"
            r"(?:is|are)\s+(?:still\s+)?(?:required|needed)\b"
            r"|\bremains?\s+to\s+be\s+"
            r"(?:completed|done|finished|run|verified|audited|tested|fixed|implemented|handled)\b"
            r"|\b(?:still (?:need|needs|require|requires|must)|remains? (?:pending|unfinished|incomplete))\b",
            semantic_text,
        )
    )
    explicitly_unfinished = explicitly_unfinished or bool(
        re.search(
            r"\b(?:there\s+(?:is|are)\s+)?(?:still\s+)?(?:more\s+)?work\s+to\s+do\b"
            r"|\b(?:some|additional|further)\s+work\s+remains?\b"
            r"|\b(?:work|tasks?|items?|verification|audit|testing|tests?|fix(?:es)?)\s+remains?\b"
            r"|\bremains?\s+to\s+be\s+"
            r"(?:completed|done|finished|run|verified|audited|tested|fixed|implemented|handled)\b"
            r"|\b(?:i|we)\s+(?:need|require)\s+another\s+turn\s+to\s+"
            r"(?:finish|complete|run|verify|audit|test|fix|implement|handle)\w*\b"
            r"|\b(?:this|it)\s+(?:will\s+)?requires?\s+another\s+turn\b"
            r"|\b(?:i|we)\s+will\s+[^.;!?]{0,80}\bin\s+another\s+turn\b",
            semantic_text,
        )
    )
    promised_required_work = bool(
        re.search(
            r"(?:\u5c07|\u5c06|\u6703|\u4f1a|\u63a5\u4e0b\u4f86|\u63a5\u4e0b\u6765|\u4e0b\u4e00\u6b65|\u4e0b\u4e00\u8f2a|\u4e0b\u4e00\u8f6e|\u7a0d\u5f8c|\u7a0d\u540e|\u5f8c\u7e8c|\u540e\u7eed).{0,80}"
            rf"{chinese_future_action}"
            r"|(?:\u81ea\u52d5|\u81ea\u52a8)(?:\u7e8c\u63a5|\u63a5\u7e8c|\u63a5\u7eed|\u7e7c\u7e8c|\u7ee7\u7eed)"
            r"|\b(?:will|next|later).{0,120}"
            r"(?:continue|resume|finish|complete|run|verify|audit|test|fix|implement|handle)\w*\b"
            r"|\b(?:remaining|outstanding).{0,120}"
            r"(?:work|tasks?|verification|audit|tests?)\b",
            semantic_text,
        )
    )
    return explicitly_unfinished or promised_required_work


def assistant_response_allows_terminal_settlement(value: str | None) -> bool:
    """Return whether assistant prose can safely be exposed as terminal."""

    return bool((value or "").strip()) and not (
        assistant_requests_manual_fresh_thread(value)
        or assistant_reports_rollover_status_only(value)
        or assistant_defers_unfinished_work(value)
    )


def outcome_allows_terminal_settlement(outcome: TurnOutcome) -> bool:
    """Apply the shared prose and lifecycle boundary to a concrete outcome."""

    return (
        (
            not outcome.guardian_finished
            or outcome.guardian_finish_kind == "completed"
        )
        and (
            not outcome.guardian_finished
            or not assistant_requires_user_input(outcome.final_response)
        )
        and assistant_response_allows_terminal_settlement(outcome.final_response)
    )


def assistant_requires_user_input(value: str | None) -> bool:
    if not isinstance(value, str) or value.count(
        CONTINUOUS_USER_INPUT_REQUIRED_MARKER
    ) != 1:
        return False
    _, _, question = value.partition(CONTINUOUS_USER_INPUT_REQUIRED_MARKER)
    question = question.strip()
    if not question or not question.endswith(("?", "\uff1f")):
        return False
    if (
        CONTINUOUS_USER_INPUT_REQUIRED_MARKER in question
        or "[CONTINUOUS_" in question
    ):
        return False
    lowered = question.casefold()
    if any(
        marker in lowered
        for marker in (
            "/new",
            "/clear",
            "continue",
            "\u7e7c\u7e8c",
            "\u63a5\u7e8c",
            "\u7e8c\u505a",
            "\u518d\u8f38\u5165",
            "\u518d\u8aaa",
        )
    ):
        return False
    return bool(re.search(r"[A-Za-z0-9\u0080-\uffff]", question))


def assistant_response_contradicts_guardian_completion(value: str | None) -> bool:
    """A registered completed receipt cannot settle unfinished/blocker prose."""

    raw_text = value or ""
    return (
        CONTINUOUS_USER_INPUT_REQUIRED_MARKER.casefold() in raw_text.casefold()
        or not assistant_response_allows_terminal_settlement(value)
        or assistant_requires_user_input(value)
    )


def assistant_claims_objective_complete(value: str | None) -> bool:
    return CONTINUOUS_OBJECTIVE_COMPLETE_MARKER.casefold() in (
        value or ""
    ).casefold()


def should_retry_generic(outcome: TurnOutcome) -> bool:
    return (
        outcome_has_generic_block(outcome)
        and not outcome.side_effects
        and not outcome_has_policy_boundary(outcome)
    )


def is_recoverable_transport_error(outcome: TurnOutcome) -> bool:
    if outcome.error_code in RECOVERABLE_TRANSPORT_CODES:
        return True
    message = (outcome.error_message or "").casefold()
    return any(
        marker in message
        for marker in (
            "connection failed",
            "connection reset",
            "stream disconnected",
            "transport closed",
        )
    )


def validate_sdk_cli_pair(sdk_version: str, cli_version: str | None) -> None:
    if not cli_version:
        raise RuntimeError("app-server initialize response did not expose a version")
    if sdk_version == cli_version:
        return
    if (sdk_version, cli_version) in TESTED_SDK_CLI_PAIRS:
        return
    raise RuntimeError(
        "untested openai-codex SDK / Codex CLI pair: "
        f"sdk={sdk_version}, cli={cli_version}. Update the continuous client "
        "compatibility pin before using this newly installed CLI."
    )


def turn_error_details(value: Any) -> tuple[str | None, str | None]:
    if not isinstance(value, dict):
        return None, None
    message = value.get("message")
    raw_code = value.get("codexErrorInfo")
    if isinstance(raw_code, dict):
        root_code = raw_code.get("root")
        if isinstance(root_code, str):
            raw_code = root_code
        elif len(raw_code) == 1:
            raw_code = next(iter(raw_code))
    code = str(raw_code) if isinstance(raw_code, str) else None
    return (str(message) if message else None), code


def contextctl_subcommands(command: str | None) -> frozenset[str]:
    """Return subcommands only for a real Python contextctl.py invocation."""
    try:
        raw_tokens = shlex.split(command or "", posix=False)
    except ValueError:
        return frozenset()
    tokens = [token.strip("\"'()") for token in raw_tokens]
    found: set[str] = set()
    python_names = {"python", "python.exe", "python3", "python3.exe", "py", "py.exe"}
    for index, token in enumerate(tokens):
        if Path(token).name.casefold() != "contextctl.py" or index == 0:
            continue
        executable = Path(tokens[index - 1]).name.casefold()
        if executable not in python_names:
            continue
        for candidate in tokens[index + 1 :]:
            normalized = candidate.strip("\"';&|(),").casefold()
            if normalized in CONTEXTCTL_SUBCOMMANDS:
                found.add(normalized)
    return frozenset(found)


def contextctl_command_binds_task(
    command: str | None,
    subcommand: str,
    task_id: str,
) -> bool:
    if subcommand.casefold() not in contextctl_subcommands(command):
        return False
    try:
        tokens = [
            token.strip("\"'()")
            for token in shlex.split(command or "", posix=False)
        ]
    except ValueError:
        return False
    for index, token in enumerate(tokens):
        normalized = token.strip("\"';&|(),")
        if normalized == "--task-id" and index + 1 < len(tokens):
            return tokens[index + 1].strip("\"';&|(),") == task_id
        if normalized.startswith("--task-id="):
            return normalized.partition("=")[2].strip("\"'") == task_id
    return False


def output_has_result_line(output: str | None, prefix: str) -> bool:
    return bool(
        re.search(
            rf"(?im)^\s*{re.escape(prefix)}(?:\s|$)",
            output or "",
        )
    )


def item_may_have_side_effect(item: dict[str, Any]) -> bool:
    item_type = getattr(item.get("type"), "value", item.get("type"))
    if item_type != "commandExecution":
        return item_type in POTENTIAL_SIDE_EFFECT_ITEMS
    actions = item.get("commandActions")
    if not isinstance(actions, list) or not actions:
        return True
    action_types = {
        action.get("type")
        for action in actions
        if isinstance(action, dict)
    }
    return not action_types or not action_types.issubset(READ_ONLY_COMMAND_ACTIONS)


def item_is_concrete_work(item: dict[str, Any]) -> bool:
    """Return whether a completed item can prove target project work began."""

    item_type = getattr(item.get("type"), "value", item.get("type"))
    if item_type not in POTENTIAL_SIDE_EFFECT_ITEMS:
        return False
    status = getattr(item.get("status"), "value", item.get("status"))
    if isinstance(status, str) and status.casefold() != "completed":
        return False
    if item.get("success") is False or item.get("error") is not None:
        return False
    if item_type == "commandExecution" and contextctl_subcommands(
        str(item.get("command", ""))
    ):
        return False
    raw_tool_name = (
        item.get("toolName")
        or item.get("tool_name")
        or item.get("name")
        or item.get("tool")
    )
    if isinstance(raw_tool_name, str):
        normalized_tool_name = re.split(r"[./:]", raw_tool_name.casefold())[-1]
        if normalized_tool_name in LIFECYCLE_ONLY_TOOL_NAMES:
            return False
    return True


def discover_guardian_target(project_root: Path) -> GuardianTarget | None:
    resolved_project = project_root.resolve()
    for candidate_root in (resolved_project, *resolved_project.parents):
        registry_path = candidate_root / ".context" / "registry.json"
        contextctl = (
            candidate_root
            / ".agents"
            / "skills"
            / "context-guardian"
            / "scripts"
            / "contextctl.py"
        )
        if not registry_path.is_file() or not contextctl.is_file():
            continue
        try:
            registry = json.loads(registry_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return None
        matches: list[tuple[int, str, Path]] = []
        for project in registry.get("projects", []):
            if not isinstance(project, dict):
                continue
            project_id = project.get("id")
            relative = project.get("path")
            if not isinstance(project_id, str) or not isinstance(relative, str):
                continue
            registered_root = (candidate_root / relative).resolve()
            if resolved_project == registered_root or registered_root in resolved_project.parents:
                matches.append((len(registered_root.parts), project_id, registered_root))
        if matches:
            _, project_id, registered_root = max(matches)
            if resolved_project != registered_root:
                for possible_git_root in (resolved_project, *resolved_project.parents):
                    if possible_git_root == registered_root:
                        break
                    if (possible_git_root / ".git").exists():
                        return None
            return GuardianTarget(candidate_root, project_id, contextctl)
    return None


def validated_guardian_bundle(
    project_root: Path,
    task_id: str,
    *,
    reject_task_id: str | None = None,
) -> str | None:
    target = discover_guardian_target(project_root)
    if target is None:
        return None
    common = [
        sys.executable,
        str(target.contextctl),
        "--root",
        str(target.workspace_root),
    ]
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    preflight_command = [
        *common,
        "preflight",
        "--project",
        target.project_id,
        "--task-id",
        task_id,
        "--resume",
    ]
    if reject_task_id:
        preflight_command.extend(["--replaces-task", reject_task_id])
    preflight = subprocess.run(
        preflight_command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        env=environment,
    )
    if preflight.returncode != 0:
        raise RuntimeError(
            "context-guardian resume preflight failed: "
            + trim_text(preflight.stderr or preflight.stdout, 800)
        )
    bounded_bundle = trim_text(preflight.stdout, MAX_GUARDIAN_BUNDLE_CHARS)
    if output_has_result_line(bounded_bundle, "CONTEXT_ROLLOVER_REQUIRED"):
        raise RuntimeError(
            "the new thread resume preflight inherited a rollover sentinel"
        )
    if reject_task_id:
        bounded_bundle = bounded_bundle.replace(
            reject_task_id,
            "[previous task id omitted]",
        )
    if f"- Task ID: `{task_id}`" not in bounded_bundle:
        raise RuntimeError(
            "context-guardian resume preflight did not bind the bundle to the new task"
        )
    return bounded_bundle


def finish_guardian_session(
    project_root: Path,
    task_id: str,
    *,
    replaced_by: str | None = None,
) -> bool:
    target = discover_guardian_target(project_root)
    if target is None:
        return False
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    try:
        command = [
                sys.executable,
                str(target.contextctl),
                "--root",
                str(target.workspace_root),
                "finish",
                "--project",
                target.project_id,
                "--task-id",
                task_id,
            ]
        if replaced_by is not None:
            command.extend(["--replaced-by", replaced_by])
        finish = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(
            "context-guardian could not run finish for the old thread session: "
            + trim_text(str(exc), 800)
        ) from exc
    if finish.returncode != 0:
        raise RuntimeError(
            "context-guardian could not finish the old thread session: "
            + trim_text(finish.stderr or finish.stdout, 800)
        )
    found = guardian_receipt(project_root, task_id)
    if found is None:
        raise RuntimeError(
            "context-guardian finish returned success without an exact lifecycle receipt"
        )
    _receipt_target, receipt = found
    expected_kind = "retired" if replaced_by is not None else "completed"
    if receipt.get("kind") != expected_kind:
        raise RuntimeError(
            "context-guardian finish receipt has the wrong lifecycle kind"
        )
    expected_replacement = replaced_by if replaced_by is not None else None
    if receipt.get("replacement_task_id") != expected_replacement:
        raise RuntimeError(
            "context-guardian finish receipt has the wrong replacement identity"
        )
    return True


def prepare_guardian_source_for_replacement(
    project_root: Path,
    task_id: str,
) -> bool:
    """Rebase and task-audit an interrupted source before a fresh replacement."""
    target = discover_guardian_target(project_root)
    if target is None:
        return False
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    common = [
        sys.executable,
        str(target.contextctl),
        "--root",
        str(target.workspace_root),
    ]
    for operation in (
        [
            "refresh",
            "--project",
            target.project_id,
            "--task-id",
            task_id,
        ],
        [
            "audit",
            "--project",
            target.project_id,
            "--task-id",
            task_id,
        ],
    ):
        try:
            completed = subprocess.run(
                [*common, *operation],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                env=environment,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(
                "context-guardian could not prepare the interrupted source "
                "for replacement: " + trim_text(str(exc), 800)
            ) from exc
        if completed.returncode != 0:
            raise RuntimeError(
                "context-guardian rejected interrupted-source replacement: "
                + trim_text(completed.stderr or completed.stdout, 800)
            )
    return True


def guardian_runtime_location(
    project_root: Path,
    task_id: str,
) -> tuple[GuardianTarget, Path] | None:
    target = discover_guardian_target(project_root)
    if target is None:
        return None
    registry_path = target.workspace_root / ".context" / "registry.json"
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "context-guardian registry is unreadable: " + trim_text(str(exc), 500)
        ) from exc
    projects = [
        item
        for item in registry.get("projects", [])
        if isinstance(item, dict) and item.get("id") == target.project_id
    ]
    if len(projects) != 1:
        raise RuntimeError("context-guardian target project is not unique")
    project = projects[0]
    project_base = (target.workspace_root / str(project.get("path", "."))).resolve()
    state_path = project_base / str(project.get("state", ".context/state.json"))
    session_path = state_path.parent / "runtime" / f"{task_id}.json"
    return target, session_path


def guardian_registered_project_root(target: GuardianTarget) -> Path:
    """Resolve the one registry-owned root for a discovered Guardian target."""
    registry_path = target.workspace_root / ".context" / "registry.json"
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "context-guardian registry is unreadable: " + trim_text(str(exc), 500)
        ) from exc
    projects = [
        item
        for item in registry.get("projects", [])
        if isinstance(item, dict) and item.get("id") == target.project_id
    ]
    if len(projects) != 1 or not isinstance(projects[0].get("path"), str):
        raise RuntimeError("context-guardian target project is not unique")
    return (target.workspace_root / projects[0]["path"]).resolve()


def read_guardian_runtime_identity(
    project_root: Path,
    task_id: str,
    *,
    require_audit: bool,
    expected_source: GuardianRuntimeIdentity | None = None,
) -> GuardianRuntimeIdentity:
    location = guardian_runtime_location(project_root, task_id)
    if location is None:
        raise RuntimeError("rollover journal requires a registered Guardian project")
    target, session_path = location
    try:
        runtime = json.loads(session_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "rollover journal Guardian runtime is unreadable: "
            + trim_text(str(exc), 500)
        ) from exc
    if runtime.get("project_id") != target.project_id or runtime.get("task_id") != task_id:
        raise RuntimeError("rollover journal Guardian runtime identity is invalid")
    state_sha = runtime.get("audited_state_sha256") if require_audit else None
    rules_sha = (
        runtime.get("audit_rules_fingerprint_sha256") if require_audit else None
    )
    audit_sha = runtime.get("audit_fingerprint_sha256") if require_audit else None
    if state_sha is None:
        state_sha = runtime.get("started_state_sha256")
    if rules_sha is None:
        rules_sha = runtime.get("started_rules_fingerprint_sha256")
    identity = GuardianRuntimeIdentity(
        task_id=task_id,
        state_sha256=_require_sha256(state_sha, "runtime state_sha256"),
        rules_sha256=_require_sha256(rules_sha, "runtime rules_sha256"),
        audit_sha256=(
            _require_sha256(audit_sha, "runtime audit_sha256")
            if audit_sha is not None
            else None
        ),
    )
    if require_audit and identity.audit_sha256 is None:
        raise RuntimeError("rollover journal source runtime has no task audit")
    if expected_source is not None:
        if runtime.get("source_task_id") != expected_source.task_id:
            raise RuntimeError("rollover journal target source task is invalid")
        if runtime.get("source_audited_state_sha256") != expected_source.state_sha256:
            raise RuntimeError("rollover journal target source state is invalid")
        if (
            runtime.get("source_audit_rules_fingerprint_sha256")
            != expected_source.rules_sha256
        ):
            raise RuntimeError("rollover journal target source rules are invalid")
        if runtime.get("source_audit_fingerprint_sha256") != expected_source.audit_sha256:
            raise RuntimeError("rollover journal target source audit is invalid")
        if identity.state_sha256 != expected_source.state_sha256:
            raise RuntimeError("rollover journal target start state is invalid")
        if identity.rules_sha256 != expected_source.rules_sha256:
            raise RuntimeError("rollover journal target start rules are invalid")
    return identity


def guardian_receipt(
    project_root: Path,
    task_id: str,
) -> tuple[GuardianTarget, dict[str, Any]] | None:
    location = guardian_runtime_location(project_root, task_id)
    if location is None:
        return None
    target, session_path = location
    receipt_path = session_path.parent / "receipts" / f"{task_id}.json"
    if not receipt_path.is_file():
        return None
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "context-guardian lifecycle receipt is unreadable: "
            + trim_text(str(exc), 500)
        ) from exc
    validate_guardian_lifecycle_receipt(receipt, target.project_id, task_id)
    return target, receipt


def reopen_completed_guardian_session(
    project_root: Path,
    task_id: str,
    receipt: dict[str, Any],
) -> str:
    """Revoke contradictory completion authority before another model turn."""

    found = guardian_receipt(project_root, task_id)
    if found is None or found[1] != receipt or receipt.get("kind") != "completed":
        raise RuntimeError("contradictory Guardian completion receipt changed")
    source_task_id = receipt.get("source_task_id")
    if source_task_id is not None and not _valid_task_identity(source_task_id):
        raise RuntimeError("contradictory Guardian completion source is invalid")
    bundle = validated_guardian_bundle(
        project_root,
        task_id,
        reject_task_id=str(source_task_id) if source_task_id is not None else None,
    )
    if bundle is None:
        raise RuntimeError("contradictory Guardian completion is unregistered")
    location = guardian_runtime_location(project_root, task_id)
    if location is None:
        raise RuntimeError("reopened Guardian runtime location is unavailable")
    target, session_path = location
    receipt_path = session_path.parent / "receipts" / f"{task_id}.json"
    try:
        runtime = json.loads(session_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "contradictory Guardian completion did not recreate its runtime"
        ) from exc
    if receipt_path.exists():
        raise RuntimeError("contradictory Guardian completion receipt was not revoked")
    expected = {
        "project_id": target.project_id,
        "task_id": task_id,
        # contextctl preserves the original task-start lineage when reopening a
        # completed receipt; final state and audit rules may have changed.
        "started_state_sha256": receipt.get("started_state_sha256"),
        "started_rules_fingerprint_sha256": receipt.get(
            "started_rules_fingerprint_sha256"
        ),
        "source_task_id": source_task_id,
        "source_audited_state_sha256": receipt.get(
            "source_audited_state_sha256"
        ),
        "source_audit_rules_fingerprint_sha256": receipt.get(
            "source_audit_rules_fingerprint_sha256"
        ),
        "source_audit_fingerprint_sha256": receipt.get(
            "source_audit_fingerprint_sha256"
        ),
    }
    if any(runtime.get(name) != value for name, value in expected.items()):
        raise RuntimeError("reopened Guardian runtime lost its exact completion lineage")
    if any(
        name in runtime
        for name in (
            "audited_state_sha256",
            "audit_rules_fingerprint_sha256",
            "audit_fingerprint_sha256",
            "work_evidence",
        )
    ):
        raise RuntimeError("reopened Guardian runtime retained stale completion evidence")
    return bundle


def rollover_journal_location(project_root: Path) -> tuple[GuardianTarget, Path] | None:
    target = discover_guardian_target(project_root)
    if target is None:
        return None
    # Scope by the registry-owned identity, never by the caller's cwd.  A
    # controller restarted from ``registered-root/src`` must find and lock the
    # same active worker as one started from ``registered-root``.
    registered_root = guardian_registered_project_root(target)
    identity = target.project_id + "\0" + _canonical_path(registered_root)
    project_scope = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return (
        target,
        target.workspace_root
        / ".context"
        / "runtime"
        / "continuous-rollover"
        / project_scope
        / "active.json",
    )


def _journal_lock_path(project_root: Path) -> Path | None:
    location = rollover_journal_location(project_root)
    if location is None:
        return None
    _target, journal_path = location
    return journal_path.with_suffix(".lock")


def _require_matching_journal_lease(
    project_root: Path,
    lease: RolloverJournalLease,
) -> None:
    expected = _journal_lock_path(project_root)
    if expected is None or _canonical_path(lease.path) != _canonical_path(expected):
        raise RuntimeError("rollover journal lock identity is invalid")


def _read_rollover_journal_unlocked(project_root: Path) -> RolloverJournal | None:
    location = rollover_journal_location(project_root)
    if location is None:
        return None
    target, journal_path = location
    if not journal_path.is_file():
        return None
    try:
        if journal_path.stat().st_size > 64 * 1024:
            raise RuntimeError("rollover journal exceeds the bounded size limit")
        raw = json.loads(journal_path.read_text(encoding="utf-8-sig"))
    except RuntimeError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "rollover journal is unreadable: " + trim_text(str(exc), 500)
        ) from exc
    return validate_rollover_journal(
        raw,
        expected_project_id=target.project_id,
        expected_workspace_root=target.workspace_root,
        expected_project_root=guardian_registered_project_root(target),
    )


def read_rollover_journal(
    project_root: Path,
    *,
    lease: RolloverJournalLease | None = None,
) -> RolloverJournal | None:
    owned = lease is None
    if lease is None:
        lock_path = _journal_lock_path(project_root)
        if lock_path is None:
            return None
        lease = RolloverJournalLease(lock_path)
    else:
        _require_matching_journal_lease(project_root, lease)
    try:
        return _read_rollover_journal_unlocked(project_root)
    finally:
        if owned:
            lease.close()


def write_rollover_journal(
    project_root: Path,
    journal: RolloverJournal,
    *,
    lease: RolloverJournalLease | None = None,
) -> None:
    owned = lease is None
    if lease is None:
        lock_path = _journal_lock_path(project_root)
        if lock_path is None:
            raise RuntimeError("rollover journal requires a registered Guardian project")
        lease = RolloverJournalLease(lock_path)
    else:
        _require_matching_journal_lease(project_root, lease)
    try:
        _write_rollover_journal_unlocked(project_root, journal)
    finally:
        if owned:
            lease.close()


def _write_rollover_journal_unlocked(
    project_root: Path,
    journal: RolloverJournal,
) -> None:
    location = rollover_journal_location(project_root)
    if location is None:
        raise RuntimeError("rollover journal requires a registered Guardian project")
    target, journal_path = location
    validated = validate_rollover_journal(
        journal.as_dict(),
        expected_project_id=target.project_id,
        expected_workspace_root=target.workspace_root,
        expected_project_root=guardian_registered_project_root(target),
    )
    current = _read_rollover_journal_unlocked(project_root)
    if current is not None:
        if (
            current.transaction_id != validated.transaction_id
            or current.source_task_id != validated.source_task_id
        ):
            raise RuntimeError("another rollover journal is already active")
        old_index = ROLLOVER_JOURNAL_PHASES.index(current.phase)
        new_index = ROLLOVER_JOURNAL_PHASES.index(validated.phase)
        if current == validated:
            return
        if new_index < old_index or new_index > old_index + 1:
            raise RuntimeError("rollover journal phase transition is invalid")
        immutable_source = (
            "transaction_id",
            "project_id",
            "workspace_root",
            "project_root",
            "session_cwd",
            "generation",
            "source_task_id",
            "source_state_sha256",
            "source_rules_sha256",
            "source_audit_sha256",
            "created_at",
        )
        if any(
            getattr(current, name) != getattr(validated, name)
            for name in immutable_source
        ):
            raise RuntimeError("rollover journal immutable source identity changed")
        if old_index >= 1 and any(
            getattr(current, name) != getattr(validated, name)
            for name in (
                "target_task_id",
                "target_state_sha256",
                "target_rules_sha256",
                "handoff_sha256",
            )
        ):
            raise RuntimeError("rollover journal immutable target identity changed")
        if dt.datetime.fromisoformat(validated.updated_at) <= dt.datetime.fromisoformat(
            current.updated_at
        ):
            raise RuntimeError("rollover journal updated_at did not advance")
        if new_index == old_index and current != validated:
            raise RuntimeError("rollover journal phase cannot be rewritten")
    _atomic_write_json(journal_path, validated.as_dict())


def replace_rollover_journal_chain(
    project_root: Path,
    previous: RolloverJournal,
    successor: RolloverJournal,
    *,
    lease: RolloverJournalLease,
) -> None:
    """Atomically make the active target the source of its next rollover."""
    _require_matching_journal_lease(project_root, lease)
    location = rollover_journal_location(project_root)
    if location is None:
        raise RuntimeError("rollover journal requires a registered Guardian project")
    target, journal_path = location
    current = _read_rollover_journal_unlocked(project_root)
    if current != previous:
        raise RuntimeError("rollover journal chain changed before replacement")
    validated = validate_rollover_journal(
        successor.as_dict(),
        expected_project_id=target.project_id,
        expected_workspace_root=target.workspace_root,
        expected_project_root=guardian_registered_project_root(target),
    )
    if (
        previous.phase != "source_retired"
        or validated.phase != "prepared"
        or previous.target_task_id != validated.source_task_id
        or validated.generation != previous.generation + 1
        or previous.project_id != validated.project_id
        or previous.workspace_root != validated.workspace_root
        or previous.project_root != validated.project_root
        or previous.transaction_id == validated.transaction_id
    ):
        raise RuntimeError("rollover journal chain replacement is invalid")
    _atomic_write_json(journal_path, validated.as_dict())


def remove_rollover_journal(
    project_root: Path,
    transaction_id: str,
    *,
    lease: RolloverJournalLease | None = None,
) -> None:
    owned = lease is None
    if lease is None:
        lock_path = _journal_lock_path(project_root)
        if lock_path is None:
            return
        lease = RolloverJournalLease(lock_path)
    else:
        _require_matching_journal_lease(project_root, lease)
    try:
        _remove_rollover_journal_unlocked(project_root, transaction_id)
    finally:
        if owned:
            lease.close()


def _remove_rollover_journal_unlocked(
    project_root: Path,
    transaction_id: str,
) -> None:
    current = _read_rollover_journal_unlocked(project_root)
    if current is None:
        return
    if current.transaction_id != transaction_id:
        raise RuntimeError("rollover journal cleanup identity is invalid")
    if current.phase != "completion_ready":
        raise RuntimeError("rollover journal cannot be removed before final publication")
    location = rollover_journal_location(project_root)
    if location is None:
        raise RuntimeError("rollover journal location disappeared")
    _target, journal_path = location
    journal_path.unlink(missing_ok=True)


def validate_guardian_work_evidence(value: Any, task_id: str) -> None:
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise RuntimeError("context-guardian work evidence schema is invalid")
    if value.get("task_id") != task_id:
        raise RuntimeError("context-guardian work evidence task is invalid")
    baseline = value.get("baseline_offset")
    observed = value.get("observed_offset")
    if (
        not isinstance(baseline, int)
        or isinstance(baseline, bool)
        or baseline < 0
        or not isinstance(observed, int)
        or isinstance(observed, bool)
        or observed <= baseline
    ):
        raise RuntimeError("context-guardian work evidence offsets are invalid")
    if not isinstance(value.get("rollout_file_id"), str) or not value["rollout_file_id"]:
        raise RuntimeError("context-guardian work evidence rollout identity is invalid")
    call_type = value.get("call_type")
    output_type = value.get("output_type")
    if (
        not isinstance(call_type, str)
        or GUARDIAN_WORK_PROTOCOLS.get(call_type) != output_type
    ):
        raise RuntimeError(
            "context-guardian work evidence call/output protocol is invalid"
        )
    for field_name in ("call_id_sha256", "call_record_sha256", "output_record_sha256"):
        if re.fullmatch(r"[0-9a-f]{64}", str(value.get(field_name, ""))) is None:
            raise RuntimeError(
                f"context-guardian work evidence {field_name} is invalid"
            )
    try:
        observed_at = dt.datetime.fromisoformat(str(value.get("observed_at")))
    except ValueError as exc:
        raise RuntimeError("context-guardian work evidence timestamp is invalid") from exc
    if observed_at.tzinfo is None:
        raise RuntimeError("context-guardian work evidence timestamp has no timezone")


def validate_guardian_lifecycle_receipt(
    receipt: Any,
    project_id: str,
    task_id: str,
) -> None:
    if (
        not isinstance(receipt, dict)
        or receipt.get("schema_version")
        != GUARDIAN_LIFECYCLE_RECEIPT_SCHEMA_VERSION
    ):
        raise RuntimeError("context-guardian lifecycle receipt schema is invalid")
    if receipt.get("project_id") != project_id or receipt.get("task_id") != task_id:
        raise RuntimeError("context-guardian lifecycle receipt identity is invalid")
    kind = receipt.get("kind")
    if kind not in {"completed", "retired"}:
        raise RuntimeError("context-guardian lifecycle receipt kind is invalid")
    for field_name in (
        "state_sha256",
        "started_state_sha256",
        "started_rules_fingerprint_sha256",
        "audited_state_sha256",
        "audit_rules_fingerprint_sha256",
        "audit_fingerprint_sha256",
    ):
        if re.fullmatch(r"[0-9a-f]{64}", str(receipt.get(field_name, ""))) is None:
            raise RuntimeError(
                f"context-guardian lifecycle receipt {field_name} is invalid"
            )
    if receipt.get("audited_state_sha256") != receipt.get("state_sha256"):
        raise RuntimeError(
            "context-guardian lifecycle receipt audit state does not match state"
        )
    source_task_id = receipt.get("source_task_id")
    source_fields = (
        receipt.get("source_audited_state_sha256"),
        receipt.get("source_audit_rules_fingerprint_sha256"),
        receipt.get("source_audit_fingerprint_sha256"),
    )
    if source_task_id is None:
        if any(value is not None for value in source_fields):
            raise RuntimeError(
                "context-guardian lifecycle receipt source lineage is incomplete"
            )
    else:
        if (
            not _valid_task_identity(source_task_id)
            or source_task_id == task_id
        ):
            raise RuntimeError(
                "context-guardian lifecycle receipt source task is invalid"
            )
        for field_name, value in zip(
            (
                "source_audited_state_sha256",
                "source_audit_rules_fingerprint_sha256",
                "source_audit_fingerprint_sha256",
            ),
            source_fields,
        ):
            if re.fullmatch(r"[0-9a-f]{64}", str(value or "")) is None:
                raise RuntimeError(
                    f"context-guardian lifecycle receipt {field_name} is invalid"
                )
        if receipt["started_state_sha256"] != source_fields[0]:
            raise RuntimeError(
                "context-guardian lifecycle receipt start state does not match source"
            )
        if receipt["started_rules_fingerprint_sha256"] != source_fields[1]:
            raise RuntimeError(
                "context-guardian lifecycle receipt start rules do not match source"
            )
        validate_guardian_work_evidence(receipt.get("work_evidence"), task_id)
    replacement_task_id = receipt.get("replacement_task_id")
    if kind == "completed" and replacement_task_id is not None:
        raise RuntimeError(
            "context-guardian completed receipt names a replacement task"
        )
    if kind == "retired" and (
        not _valid_task_identity(replacement_task_id)
        or replacement_task_id == task_id
    ):
        raise RuntimeError(
            "context-guardian retired receipt replacement is invalid"
        )
    if kind == "retired":
        validate_guardian_work_evidence(
            receipt.get("replacement_work_evidence"), str(replacement_task_id)
        )
    elif receipt.get("replacement_work_evidence") is not None:
        raise RuntimeError(
            "context-guardian completed receipt contains replacement work evidence"
        )
    recorded_at = receipt.get("recorded_at")
    try:
        parsed_at = dt.datetime.fromisoformat(str(recorded_at))
    except ValueError as exc:
        raise RuntimeError(
            "context-guardian lifecycle receipt recorded_at is invalid"
        ) from exc
    if parsed_at.tzinfo is None:
        raise RuntimeError(
            "context-guardian lifecycle receipt recorded_at has no timezone"
        )


def guardian_runtime_active(project_root: Path, task_id: str) -> bool | None:
    """Return exact target lifecycle state; None means this project is unguarded.

    Assistant prose and `turn/completed` are intentionally ignored.  Runtime
    absence succeeds only with an exact completed/retired lifecycle receipt.
    """
    location = guardian_runtime_location(project_root, task_id)
    if location is None:
        return None
    target, session_path = location
    if not session_path.is_file():
        receipt_path = session_path.parent / "receipts" / f"{task_id}.json"
        if not receipt_path.is_file():
            # Missing runtime without an exact lifecycle receipt is not proof
            # of completion.  Fail closed so the next automatic turn can
            # recreate/resume the guarded session instead of waiting for input.
            return True
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                "context-guardian lifecycle receipt is unreadable: "
                + trim_text(str(exc), 500)
            ) from exc
        validate_guardian_lifecycle_receipt(receipt, target.project_id, task_id)
        return False
    try:
        runtime = json.loads(session_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "context-guardian target runtime is unreadable: "
            + trim_text(str(exc), 500)
        ) from exc
    if runtime.get("project_id") != target.project_id:
        raise RuntimeError("context-guardian target runtime project mismatch")
    if runtime.get("task_id") != task_id:
        raise RuntimeError("context-guardian target runtime task mismatch")
    return True


def prepare_guardian_user_objective(
    project_root: Path,
    task_id: str,
) -> str | None:
    """Create the exact task runtime before a normal user turn, if needed."""
    location = guardian_runtime_location(project_root, task_id)
    if location is None:
        return None
    target, session_path = location
    if session_path.is_file():
        return None
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    result = subprocess.run(
        [
            sys.executable,
            str(target.contextctl),
            "--root",
            str(target.workspace_root),
            "preflight",
            "--project",
            target.project_id,
            "--task-id",
            task_id,
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        env=environment,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "context-guardian user-turn preflight failed: "
            + trim_text(result.stderr or result.stdout, 800)
        )
    bundle = trim_text(result.stdout, MAX_GUARDIAN_BUNDLE_CHARS)
    if output_has_result_line(bundle, "CONTEXT_PREFLIGHT_ALREADY_ACTIVE"):
        if guardian_runtime_active(project_root, task_id) is True:
            return None
        raise RuntimeError(
            "context-guardian user-turn preflight reported an active task "
            "without a valid exact runtime"
        )
    if output_has_result_line(bundle, "CONTEXT_ROLLOVER_REQUIRED"):
        raise RuntimeError(
            "context-guardian user-turn preflight unexpectedly requires rollover"
        )
    if not bundle.startswith("# BOUNDED CONTEXT BUNDLE"):
        raise RuntimeError("context-guardian user-turn preflight returned no bundle")
    if f"- Task ID: `{task_id}`" not in bundle:
        raise RuntimeError("context-guardian user-turn bundle has the wrong task id")
    return bundle


def usage_input_and_window(usage: Any | None) -> tuple[int | None, int | None]:
    if usage is None:
        return None, None
    if isinstance(usage, dict):
        last = usage.get("last")
        if isinstance(last, dict):
            input_tokens = last.get("inputTokens", last.get("input_tokens"))
            if not isinstance(input_tokens, int):
                input_tokens = last.get("totalTokens", last.get("total_tokens"))
        else:
            input_tokens = None
        window = usage.get("modelContextWindow", usage.get("model_context_window"))
    else:
        last = getattr(usage, "last", None)
        input_tokens = getattr(last, "input_tokens", None)
        if not isinstance(input_tokens, int):
            input_tokens = getattr(last, "total_tokens", None)
        window = getattr(usage, "model_context_window", None)
    if not isinstance(input_tokens, int) or not isinstance(window, int) or window <= 0:
        return None, None
    return input_tokens, window


def rollover_reason(
    outcome: TurnOutcome,
    previous_input_tokens: int | None,
    threshold: float,
) -> str | None:
    if outcome.rollover_signal:
        return outcome.rollover_signal
    if outcome.compacted:
        return "compaction event"
    if (
        outcome.guardian_checkpointed
        and outcome.guardian_audited
        and assistant_requests_manual_fresh_thread(outcome.final_response)
    ):
        return "assistant requested manual fresh thread"
    current, window = usage_input_and_window(outcome.usage)
    if current is None or window is None:
        return None
    ratio = current / window
    if ratio >= threshold:
        return f"context {ratio:.0%}"
    if (
        previous_input_tokens is not None
        and previous_input_tokens >= int(window * 0.45)
        and current <= int(previous_input_tokens * 0.70)
    ):
        return "post-compaction token drop"
    return None


def active_state_snapshot(project_root: Path) -> tuple[str, str, str]:
    state_path = project_root / "ACTIVE_STATE.md"
    if not state_path.is_file():
        return str(state_path), "missing", "(ACTIVE_STATE.md is missing)"
    raw = state_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()[:16]
    content = raw.decode("utf-8", errors="replace")
    return str(state_path), digest, trim_text(content, 3600)


def build_handoff(
    project_root: Path,
    latest_user: str,
    latest_assistant: str,
    reason: str,
    *,
    resume_bundle: str | None = None,
) -> str:
    del latest_assistant
    state_path, state_hash, _ = active_state_snapshot(project_root)
    carried_user_context = (
        "(Omitted because the validated context-guardian bundle below is authoritative.)"
        if resume_bundle
        else trim_text(latest_user)
    )
    validated = resume_bundle or (
        "No controller-validated context-guardian bundle is available. Only the state "
        "pointer above is carried forward; inspect direct evidence before acting."
    )
    preflight_instruction = (
        "The controller already ran the required context-guardian preflight with "
        "`--resume` for this new thread. Use the validated bundle below and do not "
        "repeat preflight before acting."
        if resume_bundle
        else
        "Before acting, run the required context-guardian preflight with `--resume` "
        "for this new thread, then inspect the working tree and direct evidence."
    )
    return f"""\
[AUTOMATIC FRESH-THREAD HANDOFF]

The local controller replaced an aging thread because of: {reason}.
This is a continuation, not a new user objective.

1. The prior user message below is completed context only. Do not execute it
   again; the continuation directive or new user message appended after this
   handoff is authoritative.
2. {preflight_instruction}
3. The bounded state view is `{state_path}` (sha256 prefix `{state_hash}`).
4. The previous turn has already ended. Do not replay its mutations or trust
   old assistant prose; continue only work proven unfinished by state and evidence.
5. Never ask the user to type `/new`; this controller owns future rollovers.
6. Creating this thread and acknowledging this handoff are not project
   completion. Execute the first concrete unresolved next action now.

Latest user objective:
{carried_user_context}

Controller-validated resume bundle (or pointer-only fallback):
{validated}
"""


def find_codex_binary(explicit: str | None = None) -> Path:
    candidates: list[Path] = []
    configured = explicit or os.environ.get("CODEX_CONTINUOUS_BIN")
    if configured:
        candidates.append(Path(configured).expanduser())

    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(
            Path(appdata)
            / "npm"
            / "node_modules"
            / "@openai"
            / "codex"
            / "node_modules"
            / "@openai"
            / "codex-win32-x64"
            / "vendor"
            / "x86_64-pc-windows-msvc"
            / "bin"
            / "codex.exe"
        )

    npm_root: str | None = None
    try:
        npm_command = shutil.which("npm.cmd") or shutil.which("npm") or "npm.cmd"
        result = subprocess.run(
            [npm_command, "root", "-g"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
        )
        npm_root = result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    if npm_root:
        candidates.append(
            Path(npm_root)
            / "@openai"
            / "codex"
            / "node_modules"
            / "@openai"
            / "codex-win32-x64"
            / "vendor"
            / "x86_64-pc-windows-msvc"
            / "bin"
            / "codex.exe"
        )

    direct = shutil.which("codex.exe")
    if direct:
        candidates.append(Path(direct))

    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        key = os.path.normcase(str(resolved))
        if key in seen:
            continue
        seen.add(key)
        if not resolved.is_file():
            continue
        try:
            probe = subprocess.run(
                [str(resolved), "--version"],
                check=False,
                capture_output=True,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0:
            return resolved
    raise RuntimeError(
        "No installed codex.exe was found. Run `npm install -g @openai/codex@latest` first."
    )


def codex_version(codex_bin: Path) -> str:
    result = subprocess.run(
        [str(codex_bin), "--version"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )
    return result.stdout.strip()


def metadata_server_version(metadata: Any) -> str | None:
    server_info = getattr(metadata, "serverInfo", None)
    server_version = getattr(server_info, "version", None)
    source = server_version or getattr(metadata, "userAgent", None)
    match = re.search(r"(\d+\.\d+\.\d+)", str(source or ""))
    return match.group(1) if match else None


class ContinuousCodex:
    def __init__(
        self,
        project_root: Path,
        *,
        codex_bin: Path,
        rollover_ratio: float,
        model: str | None,
        verbose: bool = False,
        renderer: TerminalRenderer | None = None,
    ) -> None:
        try:
            from openai_codex import (
                CodexConfig,
                MentionInput,
                LocalImageInput,
                SkillInput,
                TextInput,
                Thread,
                TurnHandle,
            )
            from openai_codex.client import CodexClient
            from openai_codex.generated.v2_all import (
                ThreadDeleteResponse,
                ThreadUnsubscribeResponse,
            )
        except ImportError as exc:
            raise RuntimeError(
                "The openai-codex SDK is missing. Start with bootstrap.ps1 to install it automatically."
            ) from exc

        self.sdk_version = importlib.metadata.version("openai-codex")

        runtime_env: dict[str, str] = {}
        bundled_path = codex_bin.parent.parent / "codex-path"
        if bundled_path.is_dir():
            runtime_env["PATH"] = str(bundled_path) + os.pathsep + os.environ.get("PATH", "")
        config = CodexConfig(
            codex_bin=str(codex_bin),
            cwd=str(project_root),
            env=runtime_env or None,
            client_name="codex_continuous",
            client_title="Codex Continuous CLI",
        )
        self._CodexClient = CodexClient
        self._client_config = config
        self._codex_bin = codex_bin
        self.client = CodexClient(
            config=config,
            approval_handler=self._handle_server_request,
        )
        self.client.start()
        self.metadata = self.client.initialize()
        self.server_version = metadata_server_version(self.metadata)
        expected_match = re.search(r"(\d+\.\d+\.\d+)", codex_version(codex_bin))
        if expected_match and not self.server_version:
            self.client.close()
            raise RuntimeError("app-server initialize response did not expose a version")
        if expected_match and expected_match.group(1) != self.server_version:
            self.client.close()
            raise RuntimeError(
                "app-server version mismatch: "
                f"expected {expected_match.group(1)}, got {self.server_version}"
            )
        try:
            validate_sdk_cli_pair(self.sdk_version, self.server_version)
        except RuntimeError:
            self.client.close()
            raise
        self._Thread = Thread
        self._TurnHandle = TurnHandle
        self._TextInput = TextInput
        self._SkillInput = SkillInput
        self._MentionInput = MentionInput
        self._LocalImageInput = LocalImageInput
        self._ThreadDeleteResponse = ThreadDeleteResponse
        self._ThreadUnsubscribeResponse = ThreadUnsubscribeResponse
        self.project_root = project_root
        self.rollover_ratio = rollover_ratio
        self.model = model
        self.reasoning_effort: str | None = None
        self.service_tier: str | None = None
        self.personality: str | None = None
        self.active_permission_profile: str | None = None
        self.verbose = verbose
        self._suppress_turn_output = False
        self._automatic_objective_active = False
        self.thread = None
        self.latest_user = ""
        self.latest_assistant = ""
        self.pending_handoff: str | None = None
        self._pending_guardian_finish_candidate: GuardianFinishCandidate | None = None
        self._pending_guardian_finishes: list[GuardianFinishCandidate] = []
        self._active_rollover_journal: RolloverJournal | None = None
        self._rollover_journal_lease: RolloverJournalLease | None = None
        self._rollover_target_creation_uncertain = False
        self._prefill_prompt = ""
        self._model_catalog: list[Any] | None = None
        self._fast_tier_id: str | None = None
        self._pending_turn_inputs: list[Any] = []
        self._rewind_sources: list[str] = []
        self._logical_prompts: dict[tuple[str, str], str] = {}
        self._file_checkpoints: dict[tuple[str, str], list[FileCheckpoint]] = {}
        self._prompt_commands: tuple[tuple[str, str], ...] = SLASH_COMMANDS
        self._prompt_files: tuple[str, ...] = ()
        self._slash_skills: dict[str, dict[str, Any]] = {}
        self._pending_shell_context: list[str] = []
        self._typed_ahead = ""
        self._pending_temp_images: list[Path] = []
        self._todo_plan: list[dict[str, Any]] = []
        self._todos_visible = False
        self.previous_input_tokens: int | None = None
        self.previous_context_window: int | None = None
        self.rollovers = 0
        self._output_lock = renderer.lock if renderer is not None else threading.RLock()
        self.renderer = renderer or TerminalRenderer(lock=self._output_lock)
        self._render_epoch = self.renderer.epoch
        self._line_open = self.renderer.line_open
        self._compacted_threads: set[str] = set()
        self._manual_compaction_thread: str | None = None
        self._manual_compaction_done = threading.Event()
        self._stop_events = threading.Event()
        self._pending_inputs: queue.Queue[PendingUserInput] = queue.Queue()
        self._event_thread = threading.Thread(target=self._drain_global_events, daemon=True)
        self._event_thread.start()

    def _get_renderer(self) -> TerminalRenderer:
        renderer = getattr(self, "renderer", None)
        if renderer is None:
            lock = getattr(self, "_output_lock", None) or threading.RLock()
            renderer = TerminalRenderer(stream=sys.stdout, lock=lock)
            renderer.line_open = bool(getattr(self, "_line_open", False))
            renderer.epoch = int(getattr(self, "_render_epoch", 0))
            self.renderer = renderer
            self._output_lock = lock
        return renderer

    def _discover_prompt_files(self) -> tuple[str, ...]:
        commands = (
            ["rg", "--files", "--hidden", "-g", "!.git/**", "-g", "!.workspace/**"],
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        )
        for command in commands:
            try:
                result = subprocess.run(
                    command,
                    cwd=self.project_root,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=4,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            if result.returncode not in {0, 1}:
                continue
            values = sorted(
                {
                    line.strip().replace("\\", "/")
                    for line in result.stdout.splitlines()
                    if line.strip()
                },
                key=str.casefold,
            )
            if values:
                return tuple(values[:5000])
        return ()

    def _refresh_prompt_catalog(self) -> None:
        commands = list(SLASH_COMMANDS)
        self._slash_skills = {}
        try:
            response = self._raw_request(
                "skills/list",
                {"cwds": [str(self.project_root)], "forceReload": False},
            )
            entries = response.get("data", []) if isinstance(response, dict) else []
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                for skill in entry.get("skills", []) or []:
                    if not isinstance(skill, dict) or not skill.get("enabled", True):
                        continue
                    name = str(skill.get("name", "")).strip()
                    command = "/" + name
                    if not name or not re.fullmatch(r"[A-Za-z0-9:_-]+", name):
                        continue
                    if command.casefold() in SLASH_COMMAND_NAMES:
                        continue
                    self._slash_skills[command.casefold()] = skill
                    commands.append(
                        (command, trim_text(str(skill.get("description", "Skill")), 90))
                    )
        except BaseException:
            self._slash_skills = {}
        self._prompt_commands = tuple(commands)
        self._prompt_files = self._discover_prompt_files()

    def _sync_render_state(self) -> None:
        renderer = self._get_renderer()
        self._line_open = renderer.line_open
        self._render_epoch = renderer.epoch

    def _poll_turn_keyboard(self) -> bool:
        if os.name != "nt" or not bool(getattr(sys.stdin, "isatty", lambda: False)()):
            return False
        try:
            import msvcrt
        except ImportError:
            return False
        if not msvcrt.kbhit():
            return False
        character = msvcrt.getwch()
        if character == "\x1b":
            return True
        if character == "\x03":
            raise KeyboardInterrupt
        if character in {"\x00", "\xe0"}:
            if msvcrt.kbhit():
                msvcrt.getwch()
            return False
        if character == "\b":
            self._typed_ahead = self._typed_ahead[:-1]
        elif ord(character) < 32 or character == "\x7f":
            return False
        elif character not in {"\r", "\n"}:
            self._typed_ahead += character
        return False

    def _restore_typed_ahead(self) -> None:
        value = getattr(self, "_typed_ahead", "")
        if value:
            current = getattr(self, "_prefill_prompt", "")
            self._prefill_prompt = current + value
            self._typed_ahead = ""

    def close(self) -> None:
        self._stop_events.set()
        self._cancel_pending_inputs()
        try:
            self.client.close()
        finally:
            lease = getattr(self, "_rollover_journal_lease", None)
            if lease is not None:
                lease.close()
                self._rollover_journal_lease = None
        for path in getattr(self, "_pending_temp_images", []):
            path.unlink(missing_ok=True)
        self._pending_temp_images = []

    def _restart_app_server(
        self,
        *,
        resume_thread_id: str | None = None,
        reconnect_only: bool = False,
    ) -> None:
        old_thread_id = getattr(getattr(self, "thread", None), "id", None)
        self._stop_events.set()
        self._cancel_pending_inputs()
        try:
            self.client.close()
        finally:
            if self._event_thread.is_alive():
                self._event_thread.join(timeout=0.5)

        self._stop_events = threading.Event()
        self._pending_inputs = queue.Queue()
        self._compacted_threads = set()
        self.client = self._CodexClient(
            config=self._client_config,
            approval_handler=self._handle_server_request,
        )
        self.client.start()
        self.metadata = self.client.initialize()
        self.server_version = metadata_server_version(self.metadata)
        expected_match = re.search(
            r"(\d+\.\d+\.\d+)",
            codex_version(self._codex_bin),
        )
        if expected_match and expected_match.group(1) != self.server_version:
            self.client.close()
            raise RuntimeError(
                "replacement app-server version mismatch: "
                f"expected {expected_match.group(1)}, got {self.server_version}"
            )
        try:
            validate_sdk_cli_pair(self.sdk_version, self.server_version)
        except RuntimeError:
            self.client.close()
            raise
        self.thread = None
        if isinstance(old_thread_id, str):
            self._discard_thread_runtime_state(old_thread_id)
        if resume_thread_id is None and not reconnect_only:
            self.start_cleared_thread()
            self.rollovers += 1
        elif resume_thread_id is None:
            self.thread = None
            self.previous_input_tokens = None
            self.previous_context_window = None
        else:
            resume_params: dict[str, Any] = {
                "cwd": str(self.project_root),
                "developerInstructions": CONTINUOUS_DEVELOPER_INSTRUCTIONS,
            }
            permission_profile = getattr(self, "active_permission_profile", None)
            if permission_profile:
                resume_params["config"] = {
                    "default_permissions": permission_profile
                }
            resumed = self.client.thread_resume(resume_thread_id, resume_params)
            resumed_thread_id = getattr(
                getattr(resumed, "thread", None),
                "id",
                None,
            )
            if resumed_thread_id != resume_thread_id:
                self.client.close()
                raise RuntimeError(
                    "replacement app-server did not resume the exact guarded target"
                )
            self.thread = self._Thread(self.client, resume_thread_id)
            self._adopt_runtime_settings(resumed)
            self.previous_input_tokens = None
            self.previous_context_window = None
        self._event_thread = threading.Thread(
            target=self._drain_global_events,
            daemon=True,
        )
        self._event_thread.start()

    def recover_transport_failure(self, detail: str) -> TurnOutcome:
        previous_suppression = bool(
            getattr(self, "_suppress_turn_output", False)
        )
        self._suppress_turn_output = True
        try:
            if isinstance(getattr(self, "project_root", None), Path):
                lease = self._ensure_rollover_journal_lease()
                active_journal = (
                    read_rollover_journal(self.project_root, lease=lease)
                    if lease is not None
                    else None
                )
                if active_journal is not None:
                    self._restart_app_server(
                        resume_thread_id=active_journal.target_task_id
                        if active_journal.phase != "prepared"
                        else None,
                        reconnect_only=active_journal.phase == "prepared",
                    )
                    reconciliation = self._reconcile_rollover_journal_with_retry()
                    if reconciliation == "complete":
                        reconciled = getattr(
                            self,
                            "_reconciled_terminal_outcome",
                            None,
                        )
                        if (
                            not isinstance(reconciled, TurnOutcome)
                            or not reconciled.guardian_finished
                            or reconciled.guardian_finish_kind != "completed"
                        ):
                            raise RuntimeError(
                                "rollover completion lost its exact completed receipt"
                            )
                        return reconciled
                    if reconciliation == "awaiting_user":
                        blocker = getattr(
                            self,
                            "_reconciled_user_blocker_outcome",
                            None,
                        )
                        if not isinstance(blocker, TurnOutcome):
                            raise RuntimeError(
                                "rollover transport recovery lost its exact question"
                            )
                        return blocker
                    if reconciliation != "active":
                        raise RuntimeError(
                            "rollover transport recovery lost its durable transaction"
                        )
                    if self.pending_handoff:
                        dispatched_handoff = self.pending_handoff
                        recovery_prompt = (
                            dispatched_handoff
                            + "\n\n"
                            + RECOVERED_ROLLOVER_DISPATCH_PROMPT
                        )
                        outcome = self._run_controller_turn_silently(
                            recovery_prompt
                        )
                        self._complete_handoff_dispatch(
                            outcome,
                            dispatched_handoff=dispatched_handoff,
                        )
                        return outcome
                    return self._run_controller_turn_silently(
                        RECOVERED_ROLLOVER_PROMPT
                    )
            unresolved = self._unresolved_guardian_target_candidate()
            if unresolved is not None:
                # The guarded source is still waiting for proof that this exact
                # target began concrete work.  A dead App Server transport does
                # not authorize replacing that target or forgetting the hash-
                # bound candidate: reconnect, resume it, and repair in place.
                self._restart_app_server(
                    resume_thread_id=unresolved.target_task_id,
                )
                return self._continue_unresolved_guardian_target(
                    detail,
                )

            old_task_id = getattr(getattr(self, "thread", None), "id", None)
            guardian_source_ready = False
            if isinstance(old_task_id, str):
                guardian_source_ready = prepare_guardian_source_for_replacement(
                    self.project_root,
                    old_task_id,
                )
                self._remember_rewind_source(old_task_id)

            journal: RolloverJournal | None = None
            guardian_target = discover_guardian_target(self.project_root)
            if (
                guardian_source_ready
                and isinstance(old_task_id, str)
                and isinstance(guardian_target, GuardianTarget)
            ):
                journal = self._prepare_rollover_journal(old_task_id)
                self._next_rollover_thread_source = journal.thread_source

            # The dead transport is never reused.  Restarting creates one clear
            # target; its first turn receives a task-bound Guardian bundle.
            self._pending_guardian_finish_candidate = None
            try:
                self._restart_app_server()
            finally:
                self._next_rollover_thread_source = None
            new_task_id = getattr(getattr(self, "thread", None), "id", None)
            if not isinstance(new_task_id, str):
                raise RuntimeError("transport recovery did not create a fresh task")

            resume_bundle: str | None = None
            if guardian_source_ready:
                resume_bundle = validated_guardian_bundle(
                    self.project_root,
                    new_task_id,
                    reject_task_id=old_task_id,
                )
                if resume_bundle is None:
                    raise RuntimeError(
                        "transport recovery target has no Guardian resume bundle"
                    )

            handoff = build_handoff(
                self.project_root,
                self.latest_user,
                self.latest_assistant,
                "app-server transport recovery",
                resume_bundle=resume_bundle,
            )
            if isinstance(old_task_id, str):
                handoff = handoff.replace(
                    old_task_id,
                    "[previous task id omitted]",
                )
            handoff = handoff.replace(
                "CONTEXT_ROLLOVER_REQUIRED",
                "[previous rollover sentinel omitted]",
            )
            self.pending_handoff = handoff
            if journal is not None:
                journal = self._record_rollover_target(
                    journal,
                    new_task_id,
                    handoff,
                )
            if guardian_source_ready and isinstance(old_task_id, str):
                self._pending_guardian_finish_candidate = GuardianFinishCandidate(
                    old_task_id=old_task_id,
                    target_task_id=new_task_id,
                    handoff_sha256=hashlib.sha256(
                        handoff.encode("utf-8")
                    ).hexdigest(),
                )

            recovery = handoff + """

[AUTOMATIC TRANSPORT RECOVERY — authoritative]

The previous turn may have partially executed tools before its transport ended.
Do not replay the original request or repeat mutations. Inspect bounded state and
direct working-tree evidence, then continue only work proven unfinished. For a
registered context-guardian project, the controller has already created and
validated this fresh task's exact runtime. If the objective is already complete,
make no changes; validate, checkpoint a completed state, run task-bound audit,
and plain `finish`. Do not output transport, handoff, checkpoint, or task status.
"""
            outcome = self._run_controller_turn_silently(recovery)
            self._complete_handoff_dispatch(
                outcome,
                dispatched_handoff=handoff,
            )
            return outcome
        finally:
            self._suppress_turn_output = previous_suppression

    def _write_status(
        self,
        message: str,
        *,
        verbose_only: bool = False,
        level: RenderLevel = RenderLevel.INFO,
    ) -> None:
        if getattr(self, "_suppress_turn_output", False) or getattr(
            self, "_automatic_objective_active", False
        ):
            return
        if verbose_only and not getattr(self, "verbose", False):
            return
        renderer = self._get_renderer()
        with self._output_lock:
            renderer.status(message, level=level)
            self._sync_render_state()

    def _handle_server_request(self, method: str, params: dict[str, Any] | None) -> dict[str, Any]:
        params = params or {}
        if method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        }:
            if getattr(self, "active_permission_profile", None) == ":read-only":
                return {"decision": "decline"}
            return {"decision": "accept"}
        if method == "item/tool/requestUserInput":
            pending = PendingUserInput(params)
            self._pending_inputs.put(pending)
            while not self._stop_events.is_set():
                if pending.completed.wait(0.1):
                    return pending.response or {"answers": {}}
            return {"answers": {}}
        if method == "item/permissions/requestApproval":
            requested = params.get("permissions")
            if getattr(self, "active_permission_profile", None) == ":read-only":
                return {"permissions": {}, "scope": "turn"}
            return {
                "permissions": requested if isinstance(requested, dict) else {},
                "scope": "turn",
            }
        if method == "mcpServer/elicitation/request":
            return {"action": "decline", "content": None}
        return {}

    def _service_pending_input(self, timeout: float = 0.1) -> None:
        try:
            pending = self._pending_inputs.get(timeout=timeout)
        except queue.Empty:
            return
        answers: dict[str, dict[str, list[str]]] = {}
        auto_resolve_ms = pending.params.get("autoResolutionMs")
        try:
            renderer = self._get_renderer()
            with self._output_lock:
                for question_index, question in enumerate(
                    pending.params.get("questions", [])
                ):
                    qid = str(question.get("id", "answer"))
                    label = str(question.get("question", qid))
                    options = question.get("options") or []
                    if isinstance(auto_resolve_ms, int) and options:
                        renderer.question(
                            label,
                            options if isinstance(options, list) else [],
                            heading=question_index == 0,
                        )
                        grace_ms = auto_input_grace_ms(auto_resolve_ms)
                        override = console_input_with_timeout(
                            renderer.choice_prompt(),
                            grace_ms,
                        )
                        if override:
                            value = override
                        else:
                            value = str(options[0].get("label", ""))
                            renderer.auto_choice(
                                value,
                                grace_ms=grace_ms if self.verbose else None,
                            )
                    elif options and renderer._native_stream and renderer.claude_like:
                        selection = renderer.select_menu(
                            "Select an option",
                            [
                                (
                                    str(option.get("label", "")),
                                    str(option.get("description", "")),
                                )
                                for option in options
                                if isinstance(option, dict)
                            ],
                            subtitle=label,
                        )
                        value = (
                            str(options[selection.index].get("label", ""))
                            if selection is not None
                            else ""
                        )
                    elif question.get("isSecret"):
                        renderer.question(label, [], heading=question_index == 0)
                        value = getpass.getpass(renderer.choice_prompt()).strip()
                    else:
                        renderer.question(
                            label,
                            options if isinstance(options, list) else [],
                            heading=question_index == 0,
                        )
                        value = input(renderer.choice_prompt()).strip()
                    if options and value.isdigit() and 1 <= int(value) <= len(options):
                        value = str(options[int(value) - 1].get("label", ""))
                    answers[qid] = {"answers": [value]}
                self._sync_render_state()
            pending.response = {"answers": answers}
        except (EOFError, KeyboardInterrupt):
            pending.response = {"answers": {}}
            raise
        finally:
            pending.completed.set()

    def _cancel_pending_inputs(self) -> None:
        while True:
            try:
                pending = self._pending_inputs.get_nowait()
            except queue.Empty:
                return
            pending.response = {"answers": {}}
            pending.completed.set()

    def _drain_global_events(self) -> None:
        while not self._stop_events.is_set():
            try:
                event = self.client.next_notification()
            except BaseException:
                return
            method = getattr(event, "method", "")
            payload = getattr(event, "payload", None)
            data = self._payload_dict(payload)
            if method == "thread/compacted":
                thread_id = getattr(payload, "thread_id", None) or data.get("threadId")
                if isinstance(thread_id, str):
                    self._compacted_threads.add(thread_id)
                    if thread_id == getattr(self, "_manual_compaction_thread", None):
                        self.previous_input_tokens = None
                        self.previous_context_window = None
                        self._manual_compaction_done.set()
            elif method == "turn/plan/updated":
                plan = data.get("plan", [])
                if isinstance(plan, list):
                    self._todo_plan = [item for item in plan if isinstance(item, dict)]
            elif method in {"warning", "error", "model/rerouted"}:
                message = getattr(payload, "message", None) or data.get("message")
                if message:
                    level = (
                        RenderLevel.ERROR
                        if method == "error"
                        else RenderLevel.WARNING
                        if method == "warning"
                        else RenderLevel.INFO
                    )
                    self._write_status(str(message), level=level)
    @staticmethod
    def _setting_value(value: Any) -> str | None:
        if value is None:
            return None
        raw = getattr(value, "value", value)
        return str(raw)

    def _thread_start_params(
        self,
        *,
        session_start_source: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "cwd": str(self.project_root),
            "developerInstructions": CONTINUOUS_DEVELOPER_INSTRUCTIONS,
            "serviceName": "codex_continuous",
        }
        model = getattr(self, "model", None)
        effort = getattr(self, "reasoning_effort", None)
        service_tier = getattr(self, "service_tier", None)
        personality = getattr(self, "personality", None)
        if model:
            params["model"] = model
        config_overrides: dict[str, Any] = {}
        if effort:
            config_overrides["model_reasoning_effort"] = effort
        permission_profile = getattr(self, "active_permission_profile", None)
        if permission_profile:
            config_overrides["default_permissions"] = permission_profile
        if config_overrides:
            params["config"] = config_overrides
        if service_tier:
            params["serviceTier"] = service_tier
        if personality:
            params["personality"] = personality
        if session_start_source:
            params["sessionStartSource"] = session_start_source
        thread_source = getattr(self, "_next_rollover_thread_source", None)
        if isinstance(thread_source, str) and thread_source:
            params["threadSource"] = thread_source
        return params

    def _adopt_runtime_settings(self, response: Any) -> None:
        resolved_model = self._setting_value(getattr(response, "model", None))
        resolved_effort = self._setting_value(
            getattr(response, "reasoning_effort", None)
        )
        resolved_tier = self._setting_value(getattr(response, "service_tier", None))
        if resolved_model:
            self.model = resolved_model
        if resolved_effort:
            self.reasoning_effort = resolved_effort
        if resolved_tier:
            self.service_tier = resolved_tier

    def _unsubscribe_thread(self, thread_id: str | None) -> None:
        if not isinstance(thread_id, str):
            return
        try:
            self.client.request(
                "thread/unsubscribe",
                {"threadId": thread_id},
                response_model=self._ThreadUnsubscribeResponse,
            )
        except BaseException as exc:
            self._write_status(
                "Failed to unsubscribe the previous thread: " + trim_text(str(exc), 300),
                verbose_only=True,
                level=RenderLevel.DETAIL,
            )

    def start_fresh_thread(self, *, session_start_source: str | None = None) -> None:
        previous_thread = getattr(self, "thread", None)
        previous_thread_id = getattr(previous_thread, "id", None)
        params = self._thread_start_params(session_start_source=session_start_source)
        started = self.client.thread_start(params)
        started_thread_id = getattr(getattr(started, "thread", None), "id", None)
        if (
            not isinstance(started_thread_id, str)
            or not started_thread_id.strip()
        ):
            raise RuntimeError("thread/start did not return a target thread id")
        if (
            isinstance(previous_thread_id, str)
            and started_thread_id == previous_thread_id
        ):
            raise RuntimeError("thread/start did not create a fresh thread")
        self.thread = self._Thread(self.client, started_thread_id)
        self._adopt_runtime_settings(started)
        if isinstance(previous_thread_id, str) and previous_thread_id != self.thread.id:
            self._unsubscribe_thread(previous_thread_id)
        self.previous_input_tokens = None
        self.previous_context_window = None
        if isinstance(previous_thread_id, str):
            self._discard_thread_runtime_state(previous_thread_id)

    def start_cleared_thread(self) -> None:
        """Apply the same visible and App Server semantics as terminal `/clear`."""
        lock = getattr(self, "_output_lock", None) or threading.RLock()
        self._output_lock = lock
        with lock:
            renderer = self._get_renderer()
            reset_view = getattr(renderer, "reset_conversation_view", None)
            if callable(reset_view):
                reset_view(clear_input_history=False)
            renderer.clear_screen()
            self._sync_render_state()
        self.start_fresh_thread(session_start_source="clear")

    def _discard_thread_runtime_state(self, thread_id: str) -> None:
        compacted_threads = getattr(self, "_compacted_threads", None)
        if compacted_threads is not None:
            compacted_threads.discard(thread_id)
        if getattr(self, "_manual_compaction_thread", None) == thread_id:
            self._manual_compaction_thread = None
            manual_done = getattr(self, "_manual_compaction_done", None)
            if manual_done is not None:
                manual_done.set()

    def _remember_rewind_source(self, thread_id: str) -> None:
        sources = getattr(self, "_rewind_sources", None)
        if sources is None:
            sources = []
            self._rewind_sources = sources
        if not sources or sources[-1] != thread_id:
            sources.append(thread_id)

    @staticmethod
    def _payload_dict(payload: Any) -> dict[str, Any]:
        if isinstance(payload, dict):
            return payload
        raw_params = getattr(payload, "params", None)
        if isinstance(raw_params, dict):
            return raw_params
        dump = getattr(payload, "model_dump", None)
        if dump is None:
            return {}
        value = dump(by_alias=True, exclude_none=True, mode="json")
        return value if isinstance(value, dict) else {}

    def _show_item_started(self, payload: Any) -> str | None:
        item = self._payload_dict(payload).get("item", {})
        if not isinstance(item, dict):
            return None
        item_type = item.get("type")
        if not isinstance(item_type, str):
            return None
        # Clean mode intentionally hides tool lifecycle rows.  Controller-owned
        # checkpoint/handoff/progress turns additionally force suppression even
        # if the user enabled verbose output for their own turns.
        if getattr(self, "_suppress_turn_output", False) or not getattr(
            self, "verbose", False
        ):
            return item_type
        detail = ""
        label = "Working"
        if item_type == "commandExecution":
            detail = sanitize_terminal_metadata(
                trim_text(str(item.get("command", "")), 180)
            )
            label = "Running"
        elif item_type == "webSearch":
            detail = sanitize_terminal_metadata(
                trim_text(str(item.get("query", "")), 180)
            )
            label = "Searching"
        elif item_type == "mcpToolCall":
            server = sanitize_terminal_metadata(str(item.get("server", "")))
            tool = sanitize_terminal_metadata(str(item.get("tool", "")))
            detail = " / ".join(part for part in (server, tool) if part)
            label = "Calling"
        elif item_type == "collabAgentToolCall":
            detail = sanitize_terminal_metadata(str(item.get("tool", "subagent")))
            label = "Delegating"
        elif item_type == "dynamicToolCall":
            detail = sanitize_terminal_metadata(str(item.get("tool", "dynamic tool")))
            label = "Using"
        elif item_type == "fileChange":
            changes = item.get("changes")
            paths = [
                sanitize_terminal_metadata(str(change.get("path", "")))
                for change in changes or []
                if isinstance(change, dict) and change.get("path")
            ]
            detail = paths[0] if len(paths) == 1 else f"{len(paths)} files" if paths else "file"
            label = "Editing"
        elif item_type == "imageGeneration":
            detail = "image"
            label = "Generating"
        elif item_type == "imageView":
            detail = sanitize_terminal_metadata(str(item.get("path", "image")))
            label = "Viewing"
        elif item_type in {"subAgentActivity", "subagentActivity"}:
            detail = sanitize_terminal_metadata(str(item.get("description", "agent")))
            label = "Delegating"
        elif item_type == "sleep":
            detail = sanitize_terminal_metadata(str(item.get("reason", "")))
            label = "Waiting"
        elif item_type == "contextCompaction":
            detail = "context"
            label = "Compacting"
        else:
            return item_type
        renderer = self._get_renderer()
        with self._output_lock:
            renderer.tool_started(
                ToolPresentation(kind=item_type, label=label, detail=detail)
            )
            self._sync_render_state()
        return item_type

    def _show_item_completed(self, item: dict[str, Any]) -> None:
        if getattr(self, "_suppress_turn_output", False):
            return
        item_type = item.get("type")
        detail = ""
        success: bool | None = None
        if item_type == "commandExecution":
            detail = f"exit={item.get('exitCode', 'unknown')}"
            success = item.get("exitCode") == 0
        elif item_type == "webSearch":
            detail = "completed"
            success = True
        elif item_type in {
            "fileChange",
            "mcpToolCall",
            "dynamicToolCall",
            "collabAgentToolCall",
            "imageGeneration",
            "imageView",
            "subAgentActivity",
            "subagentActivity",
            "sleep",
        }:
            detail = f"status={item.get('status', 'completed')}"
            success = str(item.get("status", "completed")).casefold() not in {
                "failed",
                "declined",
                "error",
            }
        elif item_type == "contextCompaction":
            detail = "completed"
            success = True
        else:
            return
        if not getattr(self, "verbose", False) and success is not False:
            return
        renderer = self._get_renderer()
        with self._output_lock:
            renderer.tool_completed(
                ToolPresentation(
                    kind=str(item_type),
                    label="Done" if success is not False else "Failed",
                    detail=detail,
                    success=success,
                )
            )
            self._sync_render_state()

    def run_turn(
        self,
        prompt: str,
        *,
        stream_text: bool = True,
        handle: Any | None = None,
    ) -> TurnOutcome:
        if self.thread is None:
            raise RuntimeError("thread is not initialized")
        run_thread_id = self.thread.id
        renderer = self._get_renderer()
        turn_settings = {
            key: value
            for key, value in {
                "model": getattr(self, "model", None),
                "effort": getattr(self, "reasoning_effort", None),
                "service_tier": getattr(self, "service_tier", None),
                "personality": getattr(self, "personality", None),
            }.items()
            if value is not None
        }
        if handle is None:
            pending_inputs = list(getattr(self, "_pending_turn_inputs", []) or [])
            pending_images = list(getattr(self, "_pending_temp_images", []) or [])
            turn_input: Any = prompt
            if pending_inputs:
                turn_input = pending_inputs + [self._TextInput(prompt)]
            try:
                handle = self.thread.turn(turn_input, **turn_settings)
            finally:
                for image_path in pending_images:
                    image_path.unlink(missing_ok=True)
                if pending_images:
                    self._pending_temp_images = [
                        path
                        for path in getattr(self, "_pending_temp_images", [])
                        if path not in pending_images
                    ]
                    pending_image_names = {str(path) for path in pending_images}
                    self._pending_turn_inputs = [
                        item
                        for item in getattr(self, "_pending_turn_inputs", [])
                        if str(getattr(item, "path", "")) not in pending_image_names
                    ]
            if pending_inputs:
                self._pending_turn_inputs = []
        messages: dict[str, str] = {}
        message_order: list[str] = []
        final_response = ""
        usage = None
        printed_item: str | None = None
        printed_epoch = -1
        response_started = False
        result: dict[str, Any] = {}
        compacted = False
        completion_observed = False
        side_effects = False
        side_effect_items: set[str] = set()
        rollover_signal: str | None = None
        guardian_checkpointed = False
        guardian_audited = False
        guardian_finished = False
        guardian_finish_kind: str | None = None
        tool_activity = False
        command_output_buffers: dict[str, str] = {}
        pending_file_snapshots: dict[str, dict[Path, FileSnapshot]] = {}
        completed_file_checkpoints: list[FileCheckpoint] = []
        turn_status: str | None = None
        turn_error: str | None = None
        turn_error_code: str | None = None
        turn_id: str | None = None

        def observe_completed_item(data: dict[str, Any], *, show_status: bool) -> None:
            nonlocal compacted, final_response, guardian_audited, guardian_finished
            nonlocal guardian_finish_kind
            nonlocal guardian_checkpointed, rollover_signal, side_effects, tool_activity
            item_type = data.get("type")
            if item_is_concrete_work(data):
                tool_activity = True
            if item_type == "fileChange":
                item_id = str(data.get("id") or "")
                before = pending_file_snapshots.pop(item_id, None)
                if (
                    before is not None
                    and str(data.get("status", "")).casefold() == "completed"
                ):
                    checkpoint = self._capture_file_item_after(before)
                    if checkpoint is not None:
                        completed_file_checkpoints.append(checkpoint)
            if item_type in POTENTIAL_SIDE_EFFECT_ITEMS:
                item_key = str(data.get("id") or f"{item_type}:unknown")
                declined = str(data.get("status", "")).casefold() == "declined"
                if declined or not item_may_have_side_effect(data):
                    side_effect_items.discard(item_key)
                else:
                    side_effect_items.add(item_key)
                side_effects = bool(side_effect_items)
            if item_type == "contextCompaction":
                compacted = True
            if item_type == "commandExecution":
                output = data.get("aggregatedOutput")
                item_id = str(data.get("id") or "")
                if not isinstance(output, str) or not output:
                    output = command_output_buffers.get(item_id, "")
                command = str(data.get("command", ""))
                renderer.record_transcript(
                    "[commandExecution]\n"
                    + command
                    + f"\nexit={data.get('exitCode', 'unknown')}\n"
                    + trim_text(str(output or ""), MAX_COMMAND_OUTPUT_BUFFER_CHARS)
                )
                guardian_commands = contextctl_subcommands(command)
                if guardian_commands and output_has_result_line(
                    str(output or ""),
                    "CONTEXT_ROLLOVER_REQUIRED",
                ):
                    rollover_signal = "context-guardian rollover signal"
                normalized_output = str(output or "").casefold()
                exit_code = data.get("exitCode")
                if (
                    "checkpoint" in guardian_commands
                    and exit_code == 0
                    and output_has_result_line(normalized_output, "checkpointed")
                ):
                    guardian_checkpointed = True
                if (
                    "audit" in guardian_commands
                    and exit_code == 0
                    and "context audit:" in normalized_output
                    and " projects pass" in normalized_output
                    and contextctl_command_binds_task(
                        command,
                        "audit",
                        run_thread_id,
                    )
                    and re.search(
                        rf"(?im)^task audit recorded:\s*\S+\s+task="
                        rf"{re.escape(run_thread_id)}\s*$",
                        str(output or ""),
                    )
                ):
                    guardian_audited = True
                if (
                    "finish" in guardian_commands
                    and exit_code == 0
                    and contextctl_command_binds_task(
                        command,
                        "finish",
                        run_thread_id,
                    )
                    and output_has_result_line(
                        normalized_output,
                        "context session finished:",
                    )
                    and re.search(
                        rf"(?im)^context session finished:\s*\S+\s+task="
                        rf"{re.escape(run_thread_id)}(?:\s|$)",
                        str(output or ""),
                    )
                ):
                    guardian_finished = True
                    finish_kind = re.search(
                        rf"(?im)^context session finished:\s*\S+\s+task="
                        rf"{re.escape(run_thread_id)}[^\r\n]*\bkind="
                        r"(completed|retired)\b",
                        str(output or ""),
                    )
                    if finish_kind is not None:
                        guardian_finish_kind = finish_kind.group(1).casefold()
            if item_type == "agentMessage":
                text = str(data.get("text", ""))
                phase = str(data.get("phase", ""))
                if phase == "final_answer" or not phase:
                    final_response = text
            elif show_status:
                if item_type != "commandExecution":
                    renderer.record_transcript(
                        f"[{item_type}] "
                        + trim_text(
                            json.dumps(data, ensure_ascii=False, default=str),
                            6000,
                        )
                    )
                self._show_item_completed(data)

        def consume() -> None:
            nonlocal compacted, completion_observed, final_response, printed_epoch
            nonlocal printed_item, response_started, side_effects
            nonlocal turn_error, turn_error_code, turn_id, turn_status, usage
            nonlocal tool_activity
            try:
                for event in handle.stream():
                    method = event.method
                    payload = event.payload
                    if method == "item/agentMessage/delta":
                        data = self._payload_dict(payload)
                        raw_item_id = getattr(payload, "item_id", None) or data.get("itemId")
                        raw_delta = getattr(payload, "delta", None)
                        if raw_delta is None:
                            raw_delta = data.get("delta", "")
                        item_id = str(raw_item_id or "agent")
                        delta = str(raw_delta)
                        if item_id not in messages:
                            messages[item_id] = ""
                            message_order.append(item_id)
                        messages[item_id] += delta
                        if (
                            stream_text
                            and delta
                            and not getattr(self, "_suppress_turn_output", False)
                        ):
                            with self._output_lock:
                                current_epoch = renderer.epoch
                                needs_prefix = (
                                    not response_started or printed_epoch != current_epoch
                                )
                                if needs_prefix:
                                    response_started = True
                                    printed_epoch = current_epoch
                                continuation = (
                                    not needs_prefix
                                    and printed_item is not None
                                    and printed_item != item_id
                                )
                                printed_item = item_id
                                renderer.assistant_delta(
                                    delta,
                                    prefix=needs_prefix,
                                    continuation=continuation,
                                )
                                self._sync_render_state()
                    elif method == "item/started":
                        item_type = self._show_item_started(payload)
                        if item_type not in {None, "agentMessage", "reasoning"}:
                            tool_activity = True
                        started_item = self._payload_dict(payload).get("item", {})
                        if (
                            ENABLE_LEGACY_FILE_REWIND
                            and
                            isinstance(started_item, dict)
                            and started_item.get("type") == "fileChange"
                        ):
                            item_id = str(started_item.get("id") or "")
                            before = self._capture_file_item_before(started_item)
                            if item_id and before is not None:
                                pending_file_snapshots[item_id] = before
                        if isinstance(started_item, dict) and item_may_have_side_effect(started_item):
                            item_key = str(
                                started_item.get("id")
                                or f"{started_item.get('type')}:unknown"
                            )
                            side_effect_items.add(item_key)
                            side_effects = True
                        if item_type == "contextCompaction":
                            compacted = True
                    elif method == "item/completed":
                        data = self._payload_dict(payload).get("item", {})
                        if isinstance(data, dict):
                            observe_completed_item(data, show_status=True)
                    elif method == "item/commandExecution/outputDelta":
                        data = self._payload_dict(payload)
                        item_id = str(data.get("itemId") or data.get("item_id") or "")
                        delta = str(data.get("delta") or "")
                        if item_id and delta:
                            combined = command_output_buffers.get(item_id, "") + delta
                            command_output_buffers[item_id] = combined[
                                -MAX_COMMAND_OUTPUT_BUFFER_CHARS:
                            ]
                    elif method == "turn/plan/updated":
                        data = self._payload_dict(payload)
                        plan = data.get("plan", [])
                        if isinstance(plan, list):
                            self._todo_plan = [
                                item for item in plan if isinstance(item, dict)
                            ]
                    elif method == "thread/tokenUsage/updated":
                        usage = getattr(payload, "token_usage", None)
                        if usage is None:
                            data = self._payload_dict(payload)
                            usage = data.get("tokenUsage", data.get("token_usage"))
                    elif method == "thread/compacted":
                        compacted = True
                    elif method == "error":
                        data = self._payload_dict(payload)
                        message, error_code = turn_error_details(data.get("error"))
                        if not data.get("willRetry", False):
                            turn_error = message or turn_error
                            turn_error_code = error_code or turn_error_code
                    elif method == "turn/completed":
                        completion_observed = True
                        data = self._payload_dict(payload)
                        turn = data.get("turn", {})
                        if isinstance(turn, dict):
                            raw_turn_id = turn.get("id")
                            if raw_turn_id is not None:
                                turn_id = str(raw_turn_id)
                            raw_status = turn.get("status")
                            if raw_status is not None:
                                turn_status = str(raw_status)
                            items = turn.get("items", [])
                            if isinstance(items, list):
                                for item in items:
                                    if isinstance(item, dict):
                                        observe_completed_item(item, show_status=False)
                            message, error_code = turn_error_details(turn.get("error"))
                            turn_error = message or turn_error
                            turn_error_code = error_code or turn_error_code
                        # Break on the method name even if a newer server adds fields
                        # that this pinned SDK cannot parse. Never wait forever for an
                        # exact payload class after completion has been observed.
                        break
            except BaseException as exc:
                result["error"] = exc
            finally:
                if stream_text and printed_item is not None:
                    with self._output_lock:
                        renderer.end_assistant()
                        self._sync_render_state()

        worker = threading.Thread(target=consume, daemon=True)
        worker.start()
        try:
            while worker.is_alive():
                if self._poll_turn_keyboard():
                    raise KeyboardInterrupt
                self._service_pending_input(0.1)
        except (KeyboardInterrupt, EOFError):
            self._cancel_pending_inputs()
            interrupt_result: dict[str, Any] = {}

            def request_interrupt() -> None:
                try:
                    handle.interrupt()
                except BaseException as exc:
                    interrupt_result["error"] = exc

            interrupt_worker = threading.Thread(target=request_interrupt, daemon=True)
            interrupt_worker.start()
            deadline = time.monotonic() + 10.0
            try:
                while (
                    worker.is_alive()
                    and time.monotonic() < deadline
                ):
                    self._cancel_pending_inputs()
                    worker.join(timeout=0.1)
            except (KeyboardInterrupt, EOFError) as exc:
                self._cancel_pending_inputs()
                self.client.close()
                raise RuntimeError("A second interrupt was received; the stalled app server was closed.") from exc
            if worker.is_alive():
                self.client.close()
                detail = interrupt_result.get("error")
                suffix = f" ({trim_text(str(detail), 200)})" if detail else ""
                raise RuntimeError(
                    "Turn interruption timed out; the app server was closed to prevent the CLI from hanging." + suffix
                )
            worker.join()
            if turn_status == "completed":
                self._write_status(
                    "The turn completed before the interrupt arrived; the completed result was kept.",
                    level=RenderLevel.SUCCESS,
                )
            else:
                self._write_status(
                    "The current turn was interrupted; the thread was preserved.",
                    level=RenderLevel.WARNING,
                )
                self._restore_typed_ahead()
                if turn_id and completed_file_checkpoints:
                    self._file_checkpoints[(run_thread_id, turn_id)] = list(
                        completed_file_checkpoints
                    )
                compacted = compacted or self.thread.id in self._compacted_threads
                self._compacted_threads.discard(self.thread.id)
                return TurnOutcome(
                    "",
                    usage,
                    compacted=compacted,
                    interrupted=True,
                    side_effects=side_effects,
                    rollover_signal=rollover_signal,
                    turn_id=turn_id,
                )
        worker.join()
        self._restore_typed_ahead()
        error = result.get("error")
        if error is not None:
            raise error

        if not final_response and message_order:
            final_response = messages[message_order[-1]]
        compacted = compacted or self.thread.id in self._compacted_threads
        self._compacted_threads.discard(self.thread.id)
        interrupted = turn_status == "interrupted"
        if completion_observed and turn_status is None and not turn_error:
            turn_error = "app-server returned an unparseable turn/completed payload"
        if turn_status == "failed" and not turn_error:
            turn_error = "Codex turn failed without an error message"
        if turn_id and completed_file_checkpoints:
            self._file_checkpoints[(run_thread_id, turn_id)] = list(
                completed_file_checkpoints
            )
        # Command text and command stdout are transcript diagnostics only.
        # Lifecycle authority comes from the exact task-bound durable receipt;
        # otherwise a compound command or fabricated output could terminate an
        # unfinished objective.
        guardian_finished = False
        guardian_finish_kind = None
        current_project_root = getattr(self, "project_root", None)
        if isinstance(current_project_root, Path):
            try:
                finished_receipt = guardian_receipt(
                    current_project_root,
                    run_thread_id,
                )
            except (OSError, RuntimeError, ValueError) as exc:
                receipt_error = (
                    "context-guardian lifecycle receipt validation failed: "
                    + trim_text(str(exc), 500)
                )
                turn_error = (
                    f"{turn_error}; {receipt_error}" if turn_error else receipt_error
                )
                if turn_error_code is None:
                    turn_error_code = "contextGuardianReceiptInvalid"
            else:
                if finished_receipt is not None:
                    _finished_target, receipt = finished_receipt
                    guardian_finished = True
                    guardian_finish_kind = str(receipt["kind"])
        return TurnOutcome(
            final_response,
            usage,
            compacted=compacted,
            interrupted=interrupted,
            error_message=turn_error,
            error_code=turn_error_code,
            side_effects=side_effects,
            rollover_signal=rollover_signal,
            guardian_checkpointed=guardian_checkpointed,
            guardian_audited=guardian_audited,
            guardian_finished=guardian_finished,
            guardian_finish_kind=guardian_finish_kind,
            tool_activity=tool_activity,
            turn_id=turn_id,
            policy_notice_published=(
                response_started
                and any(is_policy_content_block(value) for value in messages.values())
            ),
        )

    def _ensure_rollover_journal_lease(self) -> RolloverJournalLease | None:
        location = rollover_journal_location(self.project_root)
        if location is None:
            return None
        _target, journal_path = location
        expected = journal_path.with_suffix(".lock")
        current = getattr(self, "_rollover_journal_lease", None)
        if current is not None:
            _require_matching_journal_lease(self.project_root, current)
            return current
        current = RolloverJournalLease(expected)
        self._rollover_journal_lease = current
        return current

    def _prepare_rollover_journal(self, source_task_id: str) -> RolloverJournal:
        lease = self._ensure_rollover_journal_lease()
        if lease is None:
            raise RuntimeError("guarded rollover has no journal location")
        previous = read_rollover_journal(self.project_root, lease=lease)
        if previous is not None and (
            previous.phase != "source_retired"
            or previous.target_task_id != source_task_id
        ):
            raise RuntimeError("an unresolved rollover journal already exists")
        if previous is not None and guardian_receipt(
            self.project_root, source_task_id
        ) is not None:
            raise RuntimeError("a completed active target cannot start another rollover")
        source = read_guardian_runtime_identity(
            self.project_root,
            source_task_id,
            require_audit=True,
        )
        now = _utc_now()
        target = discover_guardian_target(self.project_root)
        if target is None:
            raise RuntimeError("guarded rollover target disappeared")
        journal = RolloverJournal(
            transaction_id=str(uuid.uuid4()),
            project_id=target.project_id,
            workspace_root=_canonical_path(target.workspace_root),
            project_root=_canonical_path(guardian_registered_project_root(target)),
            session_cwd=_canonical_path(self.project_root),
            generation=1 if previous is None else previous.generation + 1,
            source_task_id=source.task_id,
            source_state_sha256=source.state_sha256,
            source_rules_sha256=source.rules_sha256,
            source_audit_sha256=str(source.audit_sha256),
            phase="prepared",
            created_at=now,
            updated_at=now,
        )
        if previous is None:
            write_rollover_journal(self.project_root, journal, lease=lease)
        else:
            replace_rollover_journal_chain(
                self.project_root,
                previous,
                journal,
                lease=lease,
            )
        self._active_rollover_journal = journal
        return journal

    @staticmethod
    def _source_identity_from_journal(
        journal: RolloverJournal,
    ) -> GuardianRuntimeIdentity:
        return GuardianRuntimeIdentity(
            task_id=journal.source_task_id,
            state_sha256=journal.source_state_sha256,
            rules_sha256=journal.source_rules_sha256,
            audit_sha256=journal.source_audit_sha256,
        )

    def _record_rollover_target(
        self,
        journal: RolloverJournal,
        target_task_id: str,
        handoff: str,
    ) -> RolloverJournal:
        target = read_guardian_runtime_identity(
            self.project_root,
            target_task_id,
            require_audit=False,
            expected_source=self._source_identity_from_journal(journal),
        )
        advanced = replace(
            journal,
            phase="target_created",
            target_task_id=target_task_id,
            target_state_sha256=target.state_sha256,
            target_rules_sha256=target.rules_sha256,
            handoff_sha256=hashlib.sha256(handoff.encode("utf-8")).hexdigest(),
            updated_at=_next_timestamp(journal.updated_at),
        )
        lease = self._ensure_rollover_journal_lease()
        if lease is None:
            raise RuntimeError("guarded rollover journal lock disappeared")
        write_rollover_journal(self.project_root, advanced, lease=lease)
        self._active_rollover_journal = advanced
        return advanced

    def _advance_active_rollover_journal(self, phase: str) -> RolloverJournal | None:
        journal = getattr(self, "_active_rollover_journal", None)
        if journal is None:
            return None
        if phase == journal.phase:
            return journal
        advanced = replace(
            journal,
            phase=phase,
            updated_at=_next_timestamp(journal.updated_at),
        )
        lease = self._ensure_rollover_journal_lease()
        if lease is None:
            raise RuntimeError("guarded rollover journal lock disappeared")
        write_rollover_journal(self.project_root, advanced, lease=lease)
        self._active_rollover_journal = advanced
        return advanced

    def _begin_concrete_rollover_dispatch(self, dispatched_handoff: str) -> None:
        journal = getattr(self, "_active_rollover_journal", None)
        if journal is None:
            raise RuntimeError("rollover dispatch has no durable journal")
        candidate = getattr(self, "_pending_guardian_finish_candidate", None)
        current_task_id = getattr(getattr(self, "thread", None), "id", None)
        if (
            candidate is None
            or candidate.target_task_id != current_task_id
            or candidate.old_task_id != journal.source_task_id
            or candidate.target_task_id != journal.target_task_id
            or candidate.handoff_sha256
            != hashlib.sha256(dispatched_handoff.encode("utf-8")).hexdigest()
        ):
            raise RuntimeError("rollover dispatch lost its exact journal binding")
        if journal.phase == "target_created":
            self._advance_active_rollover_journal("dispatch_started")
        elif journal.phase != "dispatch_started":
            raise RuntimeError("rollover dispatch began from an invalid phase")

    def _record_concrete_rollover_dispatch(self) -> RolloverJournal | None:
        journal = getattr(self, "_active_rollover_journal", None)
        if journal is not None and journal.phase == "dispatch_started":
            return self._advance_active_rollover_journal("concrete_started")
        return journal

    def _validate_rollover_source_retirement(
        self,
        journal: RolloverJournal,
    ) -> None:
        found = guardian_receipt(self.project_root, journal.source_task_id)
        if found is None:
            raise RuntimeError("rollover source retirement has no lifecycle receipt")
        _target, receipt = found
        if (
            receipt.get("kind") != "retired"
            or receipt.get("replacement_task_id") != journal.target_task_id
            or receipt.get("state_sha256") != journal.source_state_sha256
            or receipt.get("started_state_sha256") is None
            or receipt.get("started_rules_fingerprint_sha256") is None
        ):
            raise RuntimeError("rollover source retirement receipt is invalid")

    def _finish_active_rollover_journal(self) -> None:
        journal = getattr(self, "_active_rollover_journal", None)
        if journal is None:
            return
        self._validate_rollover_source_retirement(journal)
        journal = self._advance_active_rollover_journal("source_retired")
        if journal is None:
            raise RuntimeError("rollover journal disappeared during source retirement")
        # Keep this exact target as the durable active worker.  It is cleared
        # only after the target's own completed receipt and true final response
        # are both validated and the response has been published.

    @staticmethod
    def _thread_source_value(thread: Any) -> str | None:
        raw = getattr(thread, "thread_source", None)
        if raw is None and isinstance(thread, dict):
            raw = thread.get("threadSource") or thread.get("thread_source")
        raw = getattr(raw, "value", raw)
        return str(raw) if isinstance(raw, str) else None

    @staticmethod
    def _thread_cwd_value(thread: Any) -> str | None:
        raw = getattr(thread, "cwd", None)
        if raw is None and isinstance(thread, dict):
            raw = thread.get("cwd")
        raw = getattr(raw, "root", raw)
        return str(raw) if isinstance(raw, (str, Path)) else None

    @staticmethod
    def _persisted_rollover_target_ids(journal: RolloverJournal) -> set[str]:
        """Find zero-turn targets that App Server thread/list can omit.

        App Server 0.147 persists thread/start immediately, but a real local
        smoke shows that thread/list may omit a zero-turn task even after a
        fresh server process scans active and archived pages.  The first JSONL
        record is immutable session metadata and carries the exact task id,
        custom threadSource, and cwd, so it is a deterministic local recovery
        index for this otherwise ambiguous commit/response crash window.
        """

        configured_home = os.environ.get("CODEX_HOME")
        codex_home = (
            Path(configured_home).expanduser()
            if configured_home
            else Path.home() / ".codex"
        )
        matches: set[str] = set()
        for sessions_root in (
            codex_home / "sessions",
            codex_home / "archived_sessions",
        ):
            if not sessions_root.is_dir():
                continue
            for rollout_path in sessions_root.rglob("*.jsonl"):
                if rollout_path.is_symlink() or not rollout_path.is_file():
                    continue
                try:
                    with rollout_path.open("r", encoding="utf-8-sig") as stream:
                        first_line = stream.readline(MAX_SESSION_META_BYTES + 1)
                    if len(first_line) > MAX_SESSION_META_BYTES:
                        continue
                    record = json.loads(first_line)
                except (OSError, UnicodeError, json.JSONDecodeError):
                    continue
                if not isinstance(record, dict) or record.get("type") != "session_meta":
                    continue
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    continue
                task_id = payload.get("id") or payload.get("session_id")
                cwd = payload.get("cwd")
                if (
                    _valid_task_identity(task_id)
                    and payload.get("thread_source") == journal.thread_source
                    and isinstance(cwd, str)
                    and _canonical_path(cwd) == journal.session_cwd
                ):
                    matches.add(str(task_id))
        return matches

    def _list_exact_rollover_target(self, journal: RolloverJournal) -> str | None:
        matches: set[str] = set()
        for archived in (False, True):
            cursor: str | None = None
            seen_cursors: set[str] = set()
            while True:
                params: dict[str, Any] = {
                    "cwd": journal.session_cwd,
                    "limit": 100,
                    "sortKey": "created_at",
                    "sortDirection": "desc",
                    "sourceKinds": ["appServer", "vscode"],
                    "archived": archived,
                    # Keep rollout scanning enabled.  Zero-turn App Server
                    # targets can be absent from the state DB immediately
                    # after thread/start even though their JSONL is durable.
                    "useStateDbOnly": False,
                }
                if cursor:
                    params["cursor"] = cursor
                response = self.client.thread_list(params)
                for item in list(getattr(response, "data", []) or []):
                    item_id = getattr(item, "id", None)
                    item_cwd = self._thread_cwd_value(item)
                    if (
                        _valid_task_identity(item_id)
                        and self._thread_source_value(item) == journal.thread_source
                        and item_cwd is not None
                        and _canonical_path(item_cwd) == journal.session_cwd
                    ):
                        matches.add(str(item_id))
                cursor = getattr(response, "next_cursor", None)
                if not cursor:
                    break
                cursor = str(cursor)
                if cursor in seen_cursors:
                    raise RuntimeError(
                        "rollover target search received a repeated thread/list cursor"
                    )
                seen_cursors.add(cursor)
        matches.update(self._persisted_rollover_target_ids(journal))
        if len(matches) > 1:
            raise RuntimeError(
                "prepared rollover journal identifies multiple target tasks"
            )
        return next(iter(matches)) if matches else None

    def _read_exact_rollover_target(self, journal: RolloverJournal) -> Any:
        if not _valid_task_identity(journal.target_task_id):
            raise RuntimeError("rollover journal has no exact target task")
        response = self.client.thread_read(journal.target_task_id, include_turns=True)
        thread = getattr(response, "thread", None)
        if (
            getattr(thread, "id", None) != journal.target_task_id
            or self._thread_source_value(thread) != journal.thread_source
            or self._thread_cwd_value(thread) is None
            or _canonical_path(str(self._thread_cwd_value(thread)))
            != journal.session_cwd
        ):
            raise RuntimeError("rollover journal target thread identity is invalid")
        return thread

    @classmethod
    def _thread_has_concrete_work(cls, thread: Any) -> bool:
        for turn in list(getattr(thread, "turns", []) or []):
            turn_data = cls._payload_dict(turn)
            turn_status = getattr(turn_data.get("status"), "value", turn_data.get("status"))
            items_view = getattr(
                turn_data.get("itemsView", turn_data.get("items_view")),
                "value",
                turn_data.get("itemsView", turn_data.get("items_view")),
            )
            if (
                not isinstance(turn_status, str)
                or turn_status.casefold() != "completed"
                or not isinstance(items_view, str)
                or items_view.casefold() != "full"
                or turn_data.get("error") is not None
            ):
                continue
            concrete_tool = False
            for item in turn_data.get("items", []) or []:
                data = cls._payload_dict(item)
                if item_is_concrete_work(data):
                    concrete_tool = True
            if concrete_tool:
                _user_text, assistant_text = cls._turn_texts(turn)
                if assistant_response_allows_terminal_settlement(assistant_text):
                    return True
        return False

    @classmethod
    def _thread_in_progress_turn_id(cls, thread: Any) -> str | None:
        for turn in reversed(list(getattr(thread, "turns", []) or [])):
            data = cls._payload_dict(turn)
            status = getattr(data.get("status"), "value", data.get("status"))
            turn_id = data.get("id") or getattr(turn, "id", None)
            if status == "inProgress" and _valid_task_identity(turn_id):
                return str(turn_id)
        return None

    @classmethod
    def _thread_latest_completed_response(cls, thread: Any) -> str:
        for turn in reversed(list(getattr(thread, "turns", []) or [])):
            turn_data = cls._payload_dict(turn)
            status = getattr(turn_data.get("status"), "value", turn_data.get("status"))
            if not isinstance(status, str) or status.casefold() != "completed":
                continue
            final_text: str | None = None
            legacy_text: str | None = None
            for raw_item in turn_data.get("items", []) or []:
                item = cls._payload_dict(raw_item)
                item_type = getattr(item.get("type"), "value", item.get("type"))
                if item_type != "agentMessage":
                    continue
                value = item.get("text")
                if not isinstance(value, str):
                    continue
                phase = getattr(item.get("phase"), "value", item.get("phase"))
                if phase == "final_answer":
                    final_text = value
                elif phase in {None, ""}:
                    legacy_text = value
            return (final_text if final_text is not None else legacy_text or "").strip()
        return ""

    @classmethod
    def _thread_final_response(cls, thread: Any) -> str:
        assistant_text = cls._thread_latest_completed_response(thread)
        if not assistant_text:
            raise RuntimeError("completed rollover target has no completed final turn")
        if not assistant_response_allows_terminal_settlement(assistant_text):
            raise RuntimeError(
                "completed rollover target latest turn has no publishable final response"
            )
        return assistant_text

    def _resume_exact_rollover_target(self, task_id: str) -> None:
        params: dict[str, Any] = {
            "cwd": str(self.project_root),
            "developerInstructions": CONTINUOUS_DEVELOPER_INSTRUCTIONS,
        }
        permission_profile = getattr(self, "active_permission_profile", None)
        if permission_profile:
            params["config"] = {"default_permissions": permission_profile}
        resumed = self.client.thread_resume(task_id, params)
        if getattr(getattr(resumed, "thread", None), "id", None) != task_id:
            raise RuntimeError("rollover reconciliation did not resume the exact target")
        self.thread = self._Thread(self.client, task_id)
        self._adopt_runtime_settings(resumed)
        self.previous_input_tokens = None
        self.previous_context_window = None

    def _settle_recovered_in_progress_turn(
        self,
        journal: RolloverJournal,
        thread: Any,
    ) -> Any:
        """Stop an orphaned accepted turn before dispatching recovery work."""

        turn_id = self._thread_in_progress_turn_id(thread)
        if turn_id is None:
            return thread
        self._resume_exact_rollover_target(str(journal.target_task_id))
        try:
            self.client.turn_interrupt(str(journal.target_task_id), turn_id)
        except (OSError, RuntimeError, subprocess.SubprocessError):
            refreshed = self._read_exact_rollover_target(journal)
            if self._thread_in_progress_turn_id(refreshed) is None:
                return refreshed
            raise
        for attempt in range(50):
            refreshed = self._read_exact_rollover_target(journal)
            if self._thread_in_progress_turn_id(refreshed) is None:
                return refreshed
            time.sleep(min(0.05 * (attempt + 1), 0.25))
        raise RuntimeError(
            "recovered rollover target still owns an in-progress turn after interrupt"
        )

    def _validated_target_completion(
        self,
        journal: RolloverJournal,
    ) -> bool:
        if not journal.target_task_id:
            return False
        found = guardian_receipt(self.project_root, journal.target_task_id)
        if found is None:
            return False
        _target, receipt = found
        if receipt.get("kind") != "completed":
            raise RuntimeError("rollover target has a non-terminal replacement receipt")
        expected = {
            "source_task_id": journal.source_task_id,
            "source_audited_state_sha256": journal.source_state_sha256,
            "source_audit_rules_fingerprint_sha256": journal.source_rules_sha256,
            "source_audit_fingerprint_sha256": journal.source_audit_sha256,
            "started_state_sha256": journal.target_state_sha256,
            "started_rules_fingerprint_sha256": journal.target_rules_sha256,
        }
        if any(receipt.get(name) != value for name, value in expected.items()):
            raise RuntimeError("rollover target completion lineage is invalid")
        return True

    def _mark_active_target_completion(self, final_response: str) -> None:
        journal = getattr(self, "_active_rollover_journal", None)
        if journal is None:
            return
        final_sha = hashlib.sha256(final_response.encode("utf-8")).hexdigest()
        if not self._validated_target_completion(journal):
            return
        if journal.phase == "completion_ready":
            if journal.final_response_sha256 != final_sha:
                raise RuntimeError("rollover terminal response changed after commitment")
            return
        if journal.phase != "source_retired":
            raise RuntimeError("rollover target completed before source retirement")
        advanced = replace(
            journal,
            phase="completion_ready",
            final_response_sha256=final_sha,
            updated_at=_next_timestamp(journal.updated_at),
        )
        lease = self._ensure_rollover_journal_lease()
        if lease is None:
            raise RuntimeError("guarded rollover journal lock disappeared")
        write_rollover_journal(self.project_root, advanced, lease=lease)
        self._active_rollover_journal = advanced

    def _clear_published_target_completion(self) -> None:
        journal = getattr(self, "_active_rollover_journal", None)
        if journal is None or journal.phase != "completion_ready":
            return
        lease = self._ensure_rollover_journal_lease()
        if lease is None:
            raise RuntimeError("guarded rollover journal lock disappeared")
        remove_rollover_journal(
            self.project_root,
            journal.transaction_id,
            lease=lease,
        )
        self._active_rollover_journal = None

    def _reconcile_rollover_journal(self) -> str:
        if not isinstance(getattr(self, "project_root", None), Path):
            return "none"
        lease = self._ensure_rollover_journal_lease()
        if lease is None:
            return "none"
        journal = read_rollover_journal(self.project_root, lease=lease)
        if journal is None:
            return "none"
        self._active_rollover_journal = journal
        session_cwd = Path(journal.session_cwd)
        if not session_cwd.is_dir():
            raise RuntimeError("rollover journal session cwd no longer exists")
        self.project_root = session_cwd
        source = self._source_identity_from_journal(journal)
        if journal.phase == "prepared":
            target_task_id: str | None = None
            # `thread/start` can commit server-side just before its response
            # transport fails.  Give thread/list a bounded visibility window
            # before concluding that the prepared transaction has no target;
            # otherwise eventual consistency could manufacture a duplicate.
            for visibility_attempt in range(3):
                target_task_id = self._list_exact_rollover_target(journal)
                if target_task_id is not None:
                    break
                if visibility_attempt < 2:
                    time.sleep(0.05 * (visibility_attempt + 1))
            if target_task_id is None:
                self._next_rollover_thread_source = journal.thread_source
                try:
                    self.start_fresh_thread(session_start_source="clear")
                finally:
                    self._next_rollover_thread_source = None
                target_task_id = getattr(getattr(self, "thread", None), "id", None)
                if not _valid_task_identity(target_task_id):
                    raise RuntimeError(
                        "prepared rollover transaction did not create an exact target"
                    )
            else:
                self._resume_exact_rollover_target(target_task_id)
            bundle = validated_guardian_bundle(
                self.project_root,
                target_task_id,
                reject_task_id=journal.source_task_id,
            )
            if bundle is None:
                raise RuntimeError("recovered rollover target has no Guardian bundle")
            journal = self._record_rollover_target(
                journal,
                target_task_id,
                RECOVERED_ROLLOVER_PROMPT,
            )

        completed_receipt = self._validated_target_completion(journal)
        target_thread = self._read_exact_rollover_target(journal)
        if journal.phase == "dispatch_started":
            target_thread = self._settle_recovered_in_progress_turn(
                journal,
                target_thread,
            )
        latest_response = self._thread_latest_completed_response(target_thread)
        if completed_receipt and assistant_response_contradicts_guardian_completion(
            latest_response
        ):
            if journal.phase == "completion_ready":
                raise RuntimeError(
                    "committed rollover completion contradicts its terminal response"
                )
            found = guardian_receipt(
                self.project_root,
                str(journal.target_task_id),
            )
            if found is None:
                raise RuntimeError("contradictory rollover completion receipt disappeared")
            self._pending_reopened_guardian_bundle = reopen_completed_guardian_session(
                self.project_root,
                str(journal.target_task_id),
                found[1],
            )
            completed_receipt = False
        if not completed_receipt:
            read_guardian_runtime_identity(
                self.project_root,
                str(journal.target_task_id),
                require_audit=False,
                expected_source=source,
            )
        completed = completed_receipt and assistant_response_allows_terminal_settlement(
            latest_response
        )

        final_response: str | None = None
        if completed:
            final_response = self._thread_final_response(target_thread)
            if journal.phase == "completion_ready":
                final_sha = hashlib.sha256(final_response.encode("utf-8")).hexdigest()
                if journal.final_response_sha256 != final_sha:
                    raise RuntimeError("rollover terminal response no longer matches journal")

        concrete = self._thread_has_concrete_work(target_thread)
        if completed or concrete:
            if journal.phase == "target_created":
                journal = (
                    self._advance_active_rollover_journal("dispatch_started") or journal
                )
            if journal.phase == "dispatch_started":
                journal = (
                    self._advance_active_rollover_journal("concrete_started") or journal
                )

        if journal.phase == "concrete_started":
            if guardian_receipt(self.project_root, journal.source_task_id) is None:
                if not finish_guardian_session(
                    self.project_root,
                    journal.source_task_id,
                    replaced_by=str(journal.target_task_id),
                ):
                    raise RuntimeError("rollover source retirement was not acknowledged")
            self._finish_active_rollover_journal()
            journal = self._active_rollover_journal or journal
        elif journal.phase == "source_retired":
            self._finish_active_rollover_journal()
        elif journal.phase == "completion_ready":
            self._validate_rollover_source_retirement(journal)

        if completed:
            assert final_response is not None
            self._mark_active_target_completion(final_response)
            self._reconciled_terminal_outcome = TurnOutcome(
                final_response,
                None,
                guardian_finished=True,
                guardian_finish_kind="completed",
            )
            return "complete"

        self._resume_exact_rollover_target(str(journal.target_task_id))
        if journal.phase in {"source_retired", "concrete_started"}:
            self.pending_handoff = None
            self._pending_guardian_finish_candidate = None
            return "active"

        if concrete:
            raise RuntimeError("concrete rollover dispatch did not reach source retirement")

        recovery_handoff = RECOVERED_ROLLOVER_PROMPT
        self.pending_handoff = recovery_handoff
        self._pending_guardian_finish_candidate = GuardianFinishCandidate(
            old_task_id=journal.source_task_id,
            target_task_id=str(journal.target_task_id),
            handoff_sha256=hashlib.sha256(
                recovery_handoff.encode("utf-8")
            ).hexdigest(),
        )
        return "active"

    def _reconcile_rollover_journal_with_retry(
        self,
        *,
        attempts: int = 3,
    ) -> str:
        """Recover one durable rollover locally instead of returning to the user.

        App Server transport loss and short-lived contextctl/file errors can
        happen at every transaction edge.  The journal is the authority, so a
        failed reconciliation reconnects to that exact transaction and retries
        in-process.  Persistent identity/schema failures still fail closed
        after the bounded retry window; they never authorize a fresh target.
        """

        if attempts < 1:
            raise ValueError("rollover reconciliation attempts must be positive")
        last_error: BaseException | None = None
        for attempt in range(attempts):
            try:
                if bool(
                    getattr(self, "_rollover_target_creation_uncertain", False)
                ):
                    # thread/start can commit server-side and lose only its
                    # response.  Reconnect before discovery so recovery never
                    # trusts the possibly stale creator process.
                    self._restart_app_server(reconnect_only=True)
                    self._rollover_target_creation_uncertain = False
                return self._reconcile_rollover_journal()
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                last_error = exc
                if attempt + 1 >= attempts:
                    break

                journal: RolloverJournal | None = None
                try:
                    lease = self._ensure_rollover_journal_lease()
                    journal = (
                        read_rollover_journal(self.project_root, lease=lease)
                        if lease is not None
                        else None
                    )
                except (OSError, RuntimeError, subprocess.SubprocessError) as read_exc:
                    last_error = read_exc

                if journal is not None:
                    previous_suppression = bool(
                        getattr(self, "_suppress_turn_output", False)
                    )
                    self._suppress_turn_output = True
                    try:
                        self._restart_app_server(
                            resume_thread_id=journal.target_task_id
                            if journal.phase != "prepared"
                            else None,
                            reconnect_only=journal.phase == "prepared",
                        )
                    except (OSError, RuntimeError, subprocess.SubprocessError) as restart_exc:
                        last_error = restart_exc
                    finally:
                        self._suppress_turn_output = previous_suppression
                time.sleep(0.05 * (attempt + 1))

        raise RuntimeError(
            "durable rollover reconciliation did not recover locally: "
            + trim_text(str(last_error), 500)
        ) from last_error

    def _continue_reconciled_rollover(self, reconciliation: str) -> TurnOutcome | None:
        """Resume the exact durable worker without exposing recovery chatter."""

        if reconciliation == "complete":
            outcome = getattr(self, "_reconciled_terminal_outcome", None)
            if not isinstance(outcome, TurnOutcome):
                raise RuntimeError("rollover completion lost its terminal response")
            self._publish_automatic_terminal_response(outcome)
            return None
        if reconciliation == "awaiting_user":
            outcome = getattr(self, "_reconciled_user_blocker_outcome", None)
            if not isinstance(outcome, TurnOutcome):
                raise RuntimeError("rollover user blocker lost its exact question")
            self._publish_automatic_terminal_response(outcome)
            return None
        if reconciliation != "active":
            raise RuntimeError("durable rollover transaction disappeared during recovery")

        dispatched_handoff = self.pending_handoff
        if dispatched_handoff:
            prompt = (
                dispatched_handoff
                + "\n\n"
                + RECOVERED_ROLLOVER_DISPATCH_PROMPT
            )
            outcome = self._run_controller_turn_silently(prompt)
            self._complete_handoff_dispatch(
                outcome,
                dispatched_handoff=dispatched_handoff,
            )
            return outcome
        return self._run_controller_turn_silently(RECOVERED_ROLLOVER_PROMPT)

    def _settle_finished_rollover(
        self,
        outcome: TurnOutcome,
        *,
        sentinel_has_priority: bool = True,
        include_user_context: bool = True,
    ) -> bool:
        if not outcome.guardian_finished:
            return False
        if not outcome_allows_terminal_settlement(outcome):
            # A lifecycle receipt cannot override direct evidence that required
            # project work remains (or that this task was merely retired).
            return False
        if self._current_completed_guardian_receipt() is None:
            return False
        if sentinel_has_priority and outcome.rollover_signal:
            return False
        del include_user_context
        return True

    def _run_controller_turn_silently(
        self,
        prompt: str,
        *,
        handle: Any | None = None,
    ) -> TurnOutcome:
        """Run rollover machinery without surfacing receipts or tool chatter."""
        reopened_bundle = getattr(self, "_pending_reopened_guardian_bundle", None)
        if isinstance(reopened_bundle, str) and reopened_bundle:
            prompt = (
                reopened_bundle
                + "\n\n[REOPENED GUARDIAN RUNTIME — authoritative current state]\n"
                + prompt
            )
            self._pending_reopened_guardian_bundle = None
        previous_verbose = bool(getattr(self, "verbose", False))
        previous_suppression = bool(
            getattr(self, "_suppress_turn_output", False)
        )
        candidate = getattr(self, "_pending_guardian_finish_candidate", None)
        if candidate is not None:
            dispatched_handoff = getattr(self, "pending_handoff", None)
            if (
                not isinstance(dispatched_handoff, str)
                or not dispatched_handoff
                or dispatched_handoff not in prompt
            ):
                raise RuntimeError(
                    "guarded target work prompt omitted its exact bound handoff"
                )
            # Durable before the turn RPC: a crash can reconcile this exact
            # target, but can never authorize a second target.
            self._begin_concrete_rollover_dispatch(dispatched_handoff)
        self.verbose = False
        self._suppress_turn_output = True
        try:
            if handle is not None:
                return self.run_turn(
                    prompt,
                    stream_text=False,
                    handle=handle,
                )
            return self.run_turn(prompt, stream_text=False)
        finally:
            self.verbose = previous_verbose
            self._suppress_turn_output = previous_suppression

    def _publish_automatic_terminal_response(self, outcome: TurnOutcome) -> None:
        committed_value = (outcome.final_response or "").strip()
        if not outcome_allows_terminal_settlement(outcome):
            return
        if (
            outcome.guardian_finished
            and self._current_completed_guardian_receipt() is None
        ):
            return
        value = committed_value
        for marker in (
            CONTINUOUS_OBJECTIVE_COMPLETE_MARKER,
            CONTINUOUS_USER_INPUT_REQUIRED_MARKER,
        ):
            value = re.sub(re.escape(marker), "", value, flags=re.IGNORECASE)
        value = value.strip()
        if not value:
            if (
                committed_value
                and CONTINUOUS_OBJECTIVE_COMPLETE_MARKER.casefold()
                in committed_value.casefold()
            ):
                # A marker-only response has no user-facing prose, but a valid
                # completed target still needs its SHA-bound journal finalized.
                self._mark_active_target_completion(committed_value)
                self._clear_published_target_completion()
            return
        # Commit the exact persisted final, not the marker-stripped rendering,
        # so restart verification computes the same hash.
        self._mark_active_target_completion(committed_value)
        lock = getattr(self, "_output_lock", None) or threading.RLock()
        self._output_lock = lock
        with lock:
            renderer = self._get_renderer()
            renderer.assistant_delta(value, prefix=True)
            renderer.end_assistant()
            self._sync_render_state()
        # Publishing is the last irreversible edge.  If the process crashes
        # before this cleanup, `completion_ready` intentionally republishes the
        # same SHA-bound final at least once on restart rather than losing it.
        self._clear_published_target_completion()

    def _publish_policy_boundary_notice(self, outcome: TurnOutcome) -> None:
        """Publish the platform-provided boundary at most once, without recovery."""
        if outcome.policy_notice_published:
            return
        candidates = (outcome.error_message, outcome.final_response)
        notice = next(
            (
                value.strip()
                for value in candidates
                if isinstance(value, str)
                and value.strip()
                and is_policy_content_block(value)
            ),
            None,
        )
        if notice is None:
            notice = next(
                (
                    value.strip()
                    for value in candidates
                    if isinstance(value, str) and value.strip()
                ),
                None,
            )
        if notice is None:
            return
        lock = getattr(self, "_output_lock", None) or threading.RLock()
        self._output_lock = lock
        with lock:
            renderer = self._get_renderer()
            renderer.assistant_delta(notice, prefix=True)
            renderer.end_assistant()
            self._sync_render_state()
        outcome.policy_notice_published = True

    def _settle_policy_boundary(self, outcome: TurnOutcome) -> bool:
        if not outcome_has_policy_boundary(outcome):
            return False
        self._adopt_outcome(outcome)
        self._publish_policy_boundary_notice(outcome)
        return True

    def _checkpoint_for_rollover(self, reason: str) -> bool:
        if self.thread is None or discover_guardian_target(self.project_root) is None:
            return True
        checkpoint_prompt = f"""\
[CONTROLLER CHECKPOINT-ONLY TURN]

A fresh-thread rollover is now required because of: {reason}.
Do not continue product implementation in this control turn. Use the project's
required context-guardian workflow to checkpoint the current objective, proven
facts, unresolved items, next actions and validation with CAS, then run its
task-bound context audit using `--task-id {self.thread.id}`; a project-only audit
is insufficient. Keep state bounded and exclude secrets/raw logs. Do not ask the
user to type /new; the local controller will create the new thread after this
turn completes. Do not emit a user-facing handoff receipt.
"""
        outcome = self._run_controller_turn_silently(checkpoint_prompt)
        if outcome.interrupted:
            return False
        if outcome_has_policy_boundary(outcome):
            raise _TerminalPolicyBoundary(outcome)
        if outcome.error_message or outcome_has_generic_block(outcome):
            return False
        if not (outcome.guardian_checkpointed and outcome.guardian_audited):
            return False
        return True

    def _restore_source_after_failed_target(
        self,
        source_thread: Any,
        failed_target_id: str,
    ) -> None:
        """Return to the exact source and remove a target that never preflighted."""
        source_id = getattr(source_thread, "id", None)
        if not isinstance(source_id, str) or not source_id:
            raise RuntimeError("rollover source task identity is unavailable")
        restored = source_thread
        resume = getattr(getattr(self, "client", None), "thread_resume", None)
        if callable(resume):
            resume_params: dict[str, Any] = {
                "cwd": str(self.project_root),
                "developerInstructions": CONTINUOUS_DEVELOPER_INSTRUCTIONS,
            }
            permission_profile = getattr(self, "active_permission_profile", None)
            if permission_profile:
                resume_params["config"] = {
                    "default_permissions": permission_profile
                }
            resumed = resume(source_id, resume_params)
            resumed_id = getattr(getattr(resumed, "thread", None), "id", None)
            if resumed_id != source_id:
                raise RuntimeError("failed to restore the exact rollover source task")
            thread_factory = getattr(self, "_Thread", None)
            if callable(thread_factory):
                restored = thread_factory(self.client, source_id)
            else:
                # Lightweight tests and alternate SDK shims may expose only a
                # resume response.  Preserve the known source handle when no
                # wrapper factory is available; production always has one.
                restored = source_thread
            self._adopt_runtime_settings(resumed)
        self.thread = restored
        self.pending_handoff = None
        self._pending_guardian_finish_candidate = None

        cleanup_error: BaseException | None = None
        client = getattr(self, "client", None)
        delete_supported = callable(getattr(client, "request", None)) and hasattr(
            self, "_ThreadDeleteResponse"
        )
        archive = getattr(client, "thread_archive", None)
        if delete_supported:
            try:
                self._delete_thread(failed_target_id)
            except BaseException as exc:
                cleanup_error = exc
                if callable(archive):
                    try:
                        archive(failed_target_id)
                        cleanup_error = None
                    except BaseException as archive_exc:
                        cleanup_error = archive_exc
        elif callable(archive):
            try:
                archive(failed_target_id)
            except BaseException as exc:
                cleanup_error = exc

        try:
            self._unsubscribe_thread(failed_target_id)
        except BaseException as exc:
            cleanup_error = cleanup_error or exc
        try:
            self._discard_thread_runtime_state(failed_target_id)
        except BaseException as exc:
            cleanup_error = cleanup_error or exc
        try:
            location = guardian_runtime_location(
                self.project_root,
                failed_target_id,
            )
            if location is not None:
                _target, failed_runtime = location
                if failed_runtime.is_file():
                    failed_runtime.unlink()
        except OSError as exc:
            cleanup_error = cleanup_error or exc
        except (AttributeError, RuntimeError, TypeError, ValueError):
            # A nonstandard/mocked Guardian target may not expose a registry
            # location.  Remote target cleanup and exact source restoration
            # still stand; real registered targets always resolve this path.
            pass
        if cleanup_error is not None:
            raise RuntimeError(
                "failed rollover target could not be removed: "
                + trim_text(str(cleanup_error), 400)
            ) from cleanup_error

    def prepare_rollover(
        self,
        reason: str,
        *,
        checkpoint: bool = True,
        include_user_context: bool = True,
    ) -> None:
        if self.thread is None:
            raise RuntimeError("thread is not initialized")
        # A newly prepared handoff supersedes any older handoff that never
        # dispatched successfully.  Its OLD session must remain active.
        existing_candidate = getattr(
            self,
            "_pending_guardian_finish_candidate",
            None,
        )
        if existing_candidate is not None:
            raise RuntimeError(
                "an earlier guarded handoff is unresolved; repair its exact "
                "target before creating another task"
            )
        old_thread = self.thread
        old_thread_id = old_thread.id
        guardian_target = discover_guardian_target(self.project_root)
        guardian_checkpoint = (
            checkpoint and guardian_target is not None
        )
        checkpoint_ok = self._checkpoint_for_rollover(reason) if checkpoint else True
        if guardian_checkpoint and not checkpoint_ok:
            raise RuntimeError(
                "context-guardian checkpoint/audit failed; source thread remains active"
            )
        # Once a guarded source has passed checkpoint/audit, its exact runtime
        # is the recoverable transaction record.  A target preflight failure
        # leaves that source runtime intact and the controller repairs/retries
        # this single target instead of claiming the old transport is usable.
        journal: RolloverJournal | None = None
        if guardian_checkpoint and isinstance(guardian_target, GuardianTarget):
            journal = self._prepare_rollover_journal(old_thread_id)
            self._next_rollover_thread_source = journal.thread_source
            self._rollover_target_creation_uncertain = True
        try:
            self.start_cleared_thread()
            self._rollover_target_creation_uncertain = False
        finally:
            self._next_rollover_thread_source = None
        new_thread_id = self.thread.id
        self._remember_rewind_source(old_thread_id)
        resume_bundle: str | None = None
        guardian_preflight_ok = False
        if guardian_checkpoint:
            try:
                resume_bundle = validated_guardian_bundle(
                    self.project_root,
                    new_thread_id,
                    reject_task_id=old_thread_id,
                )
                guardian_preflight_ok = resume_bundle is not None
            except RuntimeError as exc:
                if journal is not None:
                    # The target identity is already durable even though its
                    # Guardian preflight did not finish.  Deleting it would
                    # strand a prepared transaction and invite a duplicate on
                    # retry, so leave the exact target and journal intact for
                    # same-process/startup reconciliation.
                    self.thread = old_thread
                    self.pending_handoff = None
                    raise RuntimeError(
                        "context-guardian target preflight failed; exact target "
                        "is retained for journal reconciliation"
                    ) from exc
                self._restore_source_after_failed_target(
                    old_thread,
                    new_thread_id,
                )
                raise RuntimeError(
                    "context-guardian target preflight failed; failed target was "
                    "removed and the exact source task restored"
                ) from exc
            if not guardian_preflight_ok:
                if journal is not None:
                    self.thread = old_thread
                    self.pending_handoff = None
                    raise RuntimeError(
                        "context-guardian target preflight returned no bounded bundle; "
                        "exact target is retained for journal reconciliation"
                    )
                self._restore_source_after_failed_target(
                    old_thread,
                    new_thread_id,
                )
                raise RuntimeError(
                    "context-guardian target preflight returned no bounded bundle; "
                    "failed target was removed and the exact source task restored"
                )
        self.pending_handoff = build_handoff(
            self.project_root,
            self.latest_user
            if include_user_context
            else "(Original user context omitted by the controller.)",
            self.latest_assistant,
            reason,
            resume_bundle=resume_bundle,
        )
        self.pending_handoff = self.pending_handoff.replace(
            old_thread_id,
            "[previous task id omitted]",
        ).replace(
            "CONTEXT_ROLLOVER_REQUIRED",
            "[previous rollover sentinel omitted]",
        )
        if journal is not None:
            journal = self._record_rollover_target(
                journal,
                new_thread_id,
                self.pending_handoff,
            )
        if checkpoint_ok and guardian_preflight_ok:
            self._pending_guardian_finish_candidate = GuardianFinishCandidate(
                old_task_id=old_thread_id,
                target_task_id=new_thread_id,
                handoff_sha256=hashlib.sha256(
                    self.pending_handoff.encode("utf-8")
                ).hexdigest(),
            )
        self.rollovers += 1

    @staticmethod
    def _handoff_dispatch_succeeded(outcome: TurnOutcome) -> bool:
        return (
            not outcome.interrupted
            and not outcome.error_message
            and not outcome_has_generic_block(outcome)
            and outcome_allows_terminal_settlement(outcome)
            # A prose acknowledgement is not evidence that the target actually
            # resumed.  At least one concrete tool event (read-only is enough)
            # must have occurred in the target work turn.
            and bool(outcome.tool_activity or outcome.side_effects)
        )

    def _finish_guardian_sessions_after_dispatch(self, outcome: TurnOutcome) -> None:
        # Entries reach this queue only through a hash-bound, concrete target
        # work turn.  Once queued, that evidence remains valid across transient
        # finish failures; later retries must not depend on another model turn.
        del outcome
        pending = getattr(self, "_pending_guardian_finishes", None)
        if not pending:
            return
        for candidate in list(pending):
            journal = getattr(self, "_active_rollover_journal", None)
            if (
                journal is None
                or journal.phase != "concrete_started"
                or journal.source_task_id != candidate.old_task_id
                or journal.target_task_id != candidate.target_task_id
                or journal.handoff_sha256 != candidate.handoff_sha256
            ):
                raise RuntimeError(
                    "queued source retirement lacks exact concrete-started journal evidence"
                )
            try:
                finished = finish_guardian_session(
                    self.project_root,
                    candidate.old_task_id,
                    replaced_by=candidate.target_task_id,
                )
            except RuntimeError:
                continue
            if finished:
                try:
                    self._finish_active_rollover_journal()
                except RuntimeError:
                    # Keep the candidate queued until both contextctl finish and
                    # its exact durable receipt/journal transition are visible.
                    continue
                pending.remove(candidate)

    def _wait_for_pending_guardian_finishes(self, outcome: TurnOutcome) -> None:
        """Retire every proven source before publishing terminal completion.

        Retirement is local controller bookkeeping.  A transient contextctl
        failure must neither consume another model turn nor send the user back
        to a prompt, so terminal settlement waits and retries in place.
        """
        attempts = 0
        while getattr(self, "_pending_guardian_finishes", []):
            self._finish_guardian_sessions_after_dispatch(outcome)
            if not getattr(self, "_pending_guardian_finishes", []):
                return
            attempts += 1
            time.sleep(min(0.05 * (2 ** min(attempts - 1, 4)), 0.5))

    def _pending_guardian_finish_for_current_task(self) -> bool:
        return bool(getattr(self, "_pending_guardian_finishes", []))

    def _complete_handoff_dispatch(
        self,
        outcome: TurnOutcome,
        *,
        dispatched_handoff: str,
    ) -> None:
        if not self._handoff_dispatch_succeeded(outcome):
            return
        candidate = getattr(self, "_pending_guardian_finish_candidate", None)
        current_task_id = getattr(getattr(self, "thread", None), "id", None)
        if (
            candidate is not None
            and candidate.target_task_id == current_task_id
            and candidate.handoff_sha256
            == hashlib.sha256(dispatched_handoff.encode("utf-8")).hexdigest()
        ):
            journal = getattr(self, "_active_rollover_journal", None)
            if journal is None:
                raise RuntimeError(
                    "concrete rollover dispatch has no durable journal"
                )
            if (
                journal.source_task_id != candidate.old_task_id
                or journal.target_task_id != candidate.target_task_id
            ):
                raise RuntimeError(
                    "rollover journal does not match concrete dispatch candidate"
                )
            journal = self._record_concrete_rollover_dispatch()
            if journal is None or journal.phase != "concrete_started":
                raise RuntimeError(
                    "rollover journal did not record concrete target work"
                )
            pending = getattr(self, "_pending_guardian_finishes", None)
            if pending is None:
                pending = []
                self._pending_guardian_finishes = pending
            if candidate not in pending:
                pending.append(candidate)
            self._pending_guardian_finish_candidate = None
        self.pending_handoff = None
        self._finish_guardian_sessions_after_dispatch(outcome)

    def continue_after_rollover(self, reason: str) -> TurnOutcome:
        try:
            self.prepare_rollover(reason, checkpoint=True)
        except _TerminalPolicyBoundary as terminal:
            self._settle_policy_boundary(terminal.outcome)
            return terminal.outcome
        if not self.pending_handoff:
            raise RuntimeError("fresh-thread handoff was not prepared")
        dispatched_handoff = self.pending_handoff
        continuation = dispatched_handoff + """

[AUTOMATIC CONTINUATION — authoritative current instruction]

No new user input is required. Inspect the validated bounded state and direct
working-tree evidence first, then execute the first concrete unresolved next
action now. This is a work turn, not a handoff/status-report turn. Do not merely
say that the new task is continuing, list task IDs, or report rollover success.
Continue only the remaining work and never repeat mutations already proven
complete. A successful checkpoint, fresh thread, or source-session finish is
not completion of the project objective. For a registered context-guardian
project, only genuine objective completion followed by required validation, a
completed-state checkpoint, task-bound context audit, and plain `finish` is
terminal. If user-only information is genuinely required, invoke the user-input
tool in this turn. Do not substitute a plaintext blocker marker for a
registered Guardian project.
        """
        outcome = self._run_controller_turn_silently(continuation)
        self._complete_handoff_dispatch(
            outcome,
            dispatched_handoff=dispatched_handoff,
        )
        return outcome

    def _unguarded_objective_completion_proven(self, outcome: TurnOutcome) -> bool:
        if not assistant_claims_objective_complete(outcome.final_response):
            return False
        if not outcome_allows_terminal_settlement(outcome):
            return False
        project_root = getattr(self, "project_root", None)
        if not isinstance(project_root, Path):
            return True
        try:
            return discover_guardian_target(project_root) is None
        except (OSError, RuntimeError, ValueError):
            return False

    def _current_guardian_runtime_active(self) -> bool | None:
        project_root = getattr(self, "project_root", None)
        task_id = getattr(getattr(self, "thread", None), "id", None)
        if not isinstance(project_root, Path) or not isinstance(task_id, str):
            return None
        return guardian_runtime_active(project_root, task_id)

    def _current_completed_guardian_receipt(self) -> dict[str, Any] | None:
        """Return only exact completion authority for the current task.

        A transcript line, a missing runtime, or a syntactically plausible
        outcome is insufficient.  When a durable rollover transaction exists,
        its source/audit lineage must also bind this exact target receipt.
        """

        project_root = getattr(self, "project_root", None)
        task_id = getattr(getattr(self, "thread", None), "id", None)
        if not isinstance(project_root, Path) or not isinstance(task_id, str):
            return None
        found = guardian_receipt(project_root, task_id)
        if found is None:
            return None
        _target, receipt = found
        if (
            receipt.get("kind") != "completed"
            or receipt.get("replacement_task_id") is not None
            or guardian_runtime_active(project_root, task_id) is not False
        ):
            return None

        journal = getattr(self, "_active_rollover_journal", None)
        if journal is None:
            lease = getattr(self, "_rollover_journal_lease", None)
            journal = read_rollover_journal(project_root, lease=lease)
            if journal is not None:
                self._active_rollover_journal = journal
        if journal is not None:
            if journal.target_task_id != task_id:
                raise RuntimeError(
                    "completed Guardian receipt does not match the active rollover target"
                )
            if not self._validated_target_completion(journal):
                return None
        return receipt

    def _prepare_normal_guardian_user_turn(
        self,
        turn_prompt: str,
    ) -> tuple[str, bool]:
        """Preflight a normal objective with silent transient-error recovery."""
        current_task_id = getattr(getattr(self, "thread", None), "id", None)
        current_project_root = getattr(self, "project_root", None)
        if not isinstance(current_task_id, str) or not isinstance(
            current_project_root, Path
        ):
            return turn_prompt, self._current_guardian_runtime_active() is True

        retained_bundle: str | None = None
        last_error: BaseException | None = None
        for attempt in range(3):
            try:
                user_bundle = prepare_guardian_user_objective(
                    current_project_root,
                    current_task_id,
                )
                if user_bundle is not None:
                    retained_bundle = user_bundle
                runtime_active = self._current_guardian_runtime_active()
                if runtime_active is False:
                    raise RuntimeError(
                        "normal user objective is bound to a completed Guardian task"
                    )
                prepared_prompt = turn_prompt
                if retained_bundle is not None:
                    prepared_prompt = (
                        retained_bundle
                        + "\n\n[CURRENT USER MESSAGE — authoritative objective]\n"
                        + turn_prompt
                    )
                return prepared_prompt, runtime_active is True
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(0.05 * (attempt + 1))

        raise RuntimeError(
            "context-guardian normal-turn preflight/runtime recovery failed: "
            + trim_text(str(last_error), 500)
        ) from last_error

    def _unresolved_guardian_target_candidate(
        self,
    ) -> GuardianFinishCandidate | None:
        """Return a hash-bound candidate only for the exact current target.

        A mismatched or partially lost candidate is a fail-closed controller
        error.  Treating it as absent would either leak the old source runtime
        or allow a second target to overwrite the only retirement lineage.
        """
        candidate = getattr(self, "_pending_guardian_finish_candidate", None)
        if candidate is None:
            return None
        current_task_id = getattr(getattr(self, "thread", None), "id", None)
        if candidate.target_task_id != current_task_id:
            raise RuntimeError(
                "unresolved Guardian handoff is not bound to the current target"
            )
        handoff = getattr(self, "pending_handoff", None)
        if (
            not isinstance(handoff, str)
            or not handoff
            or candidate.handoff_sha256
            != hashlib.sha256(handoff.encode("utf-8")).hexdigest()
        ):
            raise RuntimeError(
                "unresolved Guardian handoff lost its exact dispatch binding"
            )
        return candidate

    def _continue_unresolved_guardian_target(
        self,
        reason: str,
    ) -> TurnOutcome:
        """Repair the first target work turn without creating another target."""
        candidate = self._unresolved_guardian_target_candidate()
        if candidate is None:
            raise RuntimeError("there is no unresolved guarded target to repair")
        del candidate
        dispatched_handoff = self.pending_handoff
        recovery_instruction = (
            "The previous target turn did not provide usable dispatch evidence. "
            "Do not replay mutations that direct working-tree evidence shows already "
            "occurred."
        )
        prompt = str(dispatched_handoff) + "\n\n" + f"""\
[CONTROLLER GUARDED TARGET REPAIR — NO NEW USER INPUT]

Stay in this exact current task. Do not create, fork, clear, or hand off to
another task. The source retirement candidate remains hash-bound to this target
until a concrete work turn succeeds. Recovery reason: {trim_text(reason, 300)}

{recovery_instruction}
Inspect the bounded state and direct working-tree evidence, then perform the
first safe concrete unfinished action now. A rollover or status receipt is not
progress. If the underlying objective is complete, validate it and perform the
registered Guardian completion transaction; otherwise continue real work.
"""
        outcome = self._run_controller_turn_silently(prompt)
        self._complete_handoff_dispatch(
            outcome,
            dispatched_handoff=dispatched_handoff,
        )
        return outcome

    def continue_active_objective(
        self,
        previous_outcome: TurnOutcome,
        *,
        sequence: int,
    ) -> TurnOutcome:
        status_only = assistant_reports_rollover_status_only(
            previous_outcome.final_response
        )
        deferred_work = assistant_defers_unfinished_work(
            previous_outcome.final_response
        )
        correction = (
            "The previous response was only a rollover/handoff status report. "
            "It did not complete the project objective. "
            if status_only
            else
            "The previous response explicitly left required work unfinished. "
            if deferred_work
            else "The previous turn did not provide terminal completion evidence. "
        )
        reopened_guardian = (
            "It also closed a Guardian session prematurely; run the required "
            "preflight with `--resume` for this current task again before "
            "continuing. "
            if (status_only or deferred_work) and previous_outcome.guardian_finished
            else ""
        )
        prompt = f"""\
[AUTOMATIC OBJECTIVE PROGRESS TURN {sequence} — NO NEW USER INPUT]

{correction}{reopened_guardian}A checkpoint, fresh thread, handoff receipt,
task ID, or statement that work *will* continue is controller bookkeeping, not
project progress. Do not return another rollover summary and do not ask the user
to say "continue".

Inspect the bounded state and direct working-tree evidence already available in
this thread. Execute the first unresolved `next_action` now using tools, then
continue as far as safely possible in this turn. Do not replay proven completed
mutations. For a registered context-guardian project, when—and only when—the
underlying objective is genuinely complete, run the required validation,
CAS-checkpoint `open` and `next_actions` empty, then run task-bound context
audit and plain `finish`; their successful command evidence is the terminal
signal. For an unregistered project, append
`[CONTINUOUS_OBJECTIVE_COMPLETE]` only after validation proves completion.

If progress truly depends on information or a decision only the user can
provide, invoke the user-input tool in this turn. Do not substitute a plaintext
blocker marker for a registered Guardian project; only an unregistered project
may use `[CONTINUOUS_USER_INPUT_REQUIRED]` when the tool is unavailable.
"""
        dispatched_handoff = self.pending_handoff
        if dispatched_handoff:
            prompt = dispatched_handoff + "\n\n" + prompt
        outcome = self._run_controller_turn_silently(prompt)
        if dispatched_handoff:
            self._complete_handoff_dispatch(
                outcome,
                dispatched_handoff=dispatched_handoff,
            )
        self._finish_guardian_sessions_after_dispatch(outcome)
        return outcome

    def continue_nonreplayable(
        self,
        reason: str,
    ) -> TurnOutcome:
        if self._unresolved_guardian_target_candidate() is not None:
            return self._continue_unresolved_guardian_target(
                reason,
            )

        guardian_active = self._current_guardian_runtime_active() is True
        try:
            self.prepare_rollover(
                reason,
                checkpoint=guardian_active,
                include_user_context=True,
            )
        except _TerminalPolicyBoundary as terminal:
            self._settle_policy_boundary(terminal.outcome)
            return terminal.outcome
        if not self.pending_handoff:
            raise RuntimeError("non-replayable handoff was not prepared")
        dispatched_handoff = self.pending_handoff
        replay_instruction = (
            "The previous turn may already have tool effects. Do not replay the "
            "original request or repeat any mutation."
        )
        continuation = dispatched_handoff + f"""

[AUTOMATIC UNAFFECTED-WORK CONTINUATION — authoritative]

{replay_instruction}
Inspect bounded state and direct working-tree evidence. Continue autonomously
only with concrete, clearly unaffected permitted engineering that remains
unfinished. If nothing unaffected remains, make no changes and report the exact
limitation briefly. This is one continuation assessment, not a retry loop.
        """
        outcome = self._run_controller_turn_silently(continuation)
        self._complete_handoff_dispatch(
            outcome,
            dispatched_handoff=dispatched_handoff,
        )
        return outcome

    def recover_generic_block(self) -> TurnOutcome:
        if self._unresolved_guardian_target_candidate() is not None:
            return self._continue_unresolved_guardian_target(
                "generic display failure on the first guarded target work turn",
            )

        guardian_active = self._current_guardian_runtime_active() is True
        if guardian_active:
            try:
                self.prepare_rollover(
                    "generic display failure before mutating tool activity",
                    checkpoint=True,
                )
            except _TerminalPolicyBoundary as terminal:
                self._settle_policy_boundary(terminal.outcome)
                return terminal.outcome
            if not self.pending_handoff:
                raise RuntimeError("guarded display-failure handoff was not prepared")
            dispatched_handoff = self.pending_handoff
            recovery = dispatched_handoff + f"""\
[GENERIC DISPLAY-FAILURE RECOVERY]

The previous fresh turn ended before any potentially mutating tool activity and
did not expose a usable response. The exact user objective is already in the
validated handoff above. Continue it once without replaying proven mutations.
"""
            outcome = self._run_controller_turn_silently(recovery)
            self._complete_handoff_dispatch(
                outcome,
                dispatched_handoff=dispatched_handoff,
            )
        else:
            state_path, state_hash, _ = active_state_snapshot(self.project_root)
            recovery = f"""\
[GENERIC DISPLAY-FAILURE RECOVERY]

The previous fresh turn ended before any potentially mutating tool activity and
did not expose a usable response. Retry the exact user message below once.
Evaluate concrete actions individually; if an
exact action is unavailable, continue every unaffected permitted part.

Project root: {self.project_root}
Bounded state pointer: {state_path} (sha256 prefix {state_hash})

[ORIGINAL USER MESSAGE — authoritative; appears exactly once]
{self.latest_user}
"""
            self.pending_handoff = None
            self._remember_rewind_source(self.thread.id)
            self.start_cleared_thread()
            self.rollovers += 1
            outcome = self._run_controller_turn_silently(recovery)
        self.latest_assistant = outcome.final_response or outcome.error_message or ""
        current, window = usage_input_and_window(outcome.usage)
        self.previous_input_tokens = current
        self.previous_context_window = window
        return outcome

    def _raw_request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        request = getattr(self.client, "_request_raw", None)
        if not callable(request):
            raise RuntimeError(f"app-server RPC unavailable: {method}")
        return request(method, params)

    def _load_model_catalog(self, *, refresh: bool = True) -> list[Any]:
        cached = getattr(self, "_model_catalog", None)
        if cached is not None and not refresh:
            return cached
        response = self.client.model_list(False)
        catalog = list(getattr(response, "data", []) or [])
        if not catalog:
            raise RuntimeError("model catalog is empty")
        self._model_catalog = catalog
        current = self._current_catalog_model(catalog)
        self._fast_tier_id = self._fast_tier(current) if current is not None else None
        return catalog

    @staticmethod
    def _model_efforts(model: Any) -> list[str]:
        result: list[str] = []
        for option in getattr(model, "supported_reasoning_efforts", []) or []:
            effort = ContinuousCodex._setting_value(
                getattr(option, "reasoning_effort", None)
            )
            if effort and effort not in result:
                result.append(effort)
        return result

    @staticmethod
    def _fast_tier(model: Any) -> str | None:
        for tier in getattr(model, "service_tiers", []) or []:
            tier_id = ContinuousCodex._setting_value(getattr(tier, "id", None))
            name = ContinuousCodex._setting_value(getattr(tier, "name", None))
            if tier_id and (tier_id == "priority" or (name or "").casefold() == "fast"):
                return tier_id
        return None

    def _current_catalog_model(self, catalog: list[Any]) -> Any | None:
        current = (getattr(self, "model", None) or "").casefold()
        for model in catalog:
            candidate = self._setting_value(getattr(model, "model", None)) or ""
            if candidate.casefold() == current:
                return model
        if current:
            return None
        return next((model for model in catalog if getattr(model, "is_default", False)), None)

    def _find_catalog_model(self, catalog: list[Any], value: str) -> Any | None:
        folded = value.casefold()
        exact: list[Any] = []
        partial: list[Any] = []
        for model in catalog:
            names = {
                self._setting_value(getattr(model, "model", None)) or "",
                self._setting_value(getattr(model, "id", None)) or "",
                self._setting_value(getattr(model, "display_name", None)) or "",
            }
            if folded in {name.casefold() for name in names if name}:
                exact.append(model)
            elif any(folded in name.casefold() for name in names if name):
                partial.append(model)
        matches = exact or partial
        return matches[0] if len(matches) == 1 else None

    def _choose(self, label: str, options: list[tuple[str, str]]) -> str | None:
        if not options:
            return None
        renderer = self._get_renderer()
        if renderer._native_stream and renderer.claude_like:
            selection = renderer.select_menu(
                label,
                options,
                subtitle="Use ↑/↓ to select",
            )
            return options[selection.index][0] if selection is not None else None
        with self._output_lock:
            renderer.question(
                label,
                [
                    {"label": option_label, "description": description}
                    for option_label, description in options
                ],
            )
            value = input(renderer.choice_prompt()).strip()
            self._sync_render_state()
        if not value:
            return None
        if value.isdigit() and 1 <= int(value) <= len(options):
            return options[int(value) - 1][0]
        for option_label, _description in options:
            if value.casefold() == option_label.casefold():
                return option_label
        return None

    def _confirm(self, title: str, description: str) -> bool:
        renderer = self._get_renderer()
        if renderer._native_stream and renderer.claude_like:
            selection = renderer.select_menu(
                title,
                [("Yes", description), ("No", "Cancel")],
                subtitle="This action requires confirmation",
                initial_index=1,
            )
            return selection is not None and selection.index == 0
        return input(f"{title} [y/N] ").strip().casefold() in {"y", "yes"}

    def _persist_config(self, edits: list[tuple[str, Any]]) -> None:
        self._raw_request(
            "config/batchWrite",
            {
                "edits": [
                    {
                        "keyPath": key,
                        "value": value,
                        "mergeStrategy": "replace",
                    }
                    for key, value in edits
                ],
                "reloadUserConfig": True,
            },
        )

    def _update_settings_and_persist(
        self,
        settings: dict[str, Any],
        edits: list[tuple[str, Any]],
        rollback: dict[str, Any],
    ) -> None:
        self._raw_request("thread/settings/update", settings)
        try:
            self._persist_config(edits)
        except BaseException as persistence_error:
            try:
                self._raw_request("thread/settings/update", rollback)
            except BaseException as rollback_error:
                raise RuntimeError(
                    "Saving settings failed, and restoring runtime settings also failed: "
                    + trim_text(str(rollback_error), 300)
                ) from persistence_error
            raise

    def _apply_model(self, model: Any, effort: str, *, persist: bool = True) -> None:
        model_name = self._setting_value(getattr(model, "model", None))
        if not model_name:
            raise RuntimeError("selected model has no request name")
        supported = self._model_efforts(model)
        if effort not in supported:
            raise RuntimeError(
                f"{model_name} does not support {effort}; available values: {', '.join(supported)}"
            )
        tier = getattr(self, "service_tier", None) or "default"
        old_fast_tier = getattr(self, "_fast_tier_id", None)
        new_fast_tier = self._fast_tier(model)
        edits: list[tuple[str, Any]] = [
            ("model", model_name),
            ("model_reasoning_effort", effort),
        ]
        settings: dict[str, Any] = {
            "threadId": self.thread.id,
            "model": model_name,
            "effort": effort,
        }
        rollback: dict[str, Any] = {
            "threadId": self.thread.id,
            "model": getattr(self, "model", None),
            "effort": getattr(self, "reasoning_effort", None),
        }
        if tier != "default" and (old_fast_tier is None or tier == old_fast_tier):
            rollback["serviceTier"] = tier
            if new_fast_tier is None:
                tier = "default"
                edits.append(("service_tier", "default"))
            else:
                tier = new_fast_tier
            settings["serviceTier"] = tier
        if persist:
            self._update_settings_and_persist(settings, edits, rollback)
        else:
            self._raw_request("thread/settings/update", settings)
        self.model = model_name
        self.reasoning_effort = effort
        self.service_tier = tier
        self._fast_tier_id = new_fast_tier

    def _handle_model_command(self, arguments: list[str]) -> None:
        catalog = self._load_model_catalog()
        if arguments and arguments[0].casefold() == "status":
            self._write_status(
                f"{getattr(self, 'model', None) or 'default'} · "
                f"{getattr(self, 'reasoning_effort', None) or 'default'}"
            )
            return
        persist = True
        if arguments:
            selected = self._find_catalog_model(catalog, arguments[0])
            if selected is None:
                raise RuntimeError("No unique model matched; run /model and choose from the list")
        else:
            visible = [model for model in catalog if not getattr(model, "hidden", False)]
            current = self._current_catalog_model(visible)
            current_index = visible.index(current) if current in visible else 0
            selection = self._get_renderer().select_menu(
                "Select model",
                [
                    (
                        self._setting_value(getattr(model, "display_name", None))
                        or self._setting_value(getattr(model, "model", None))
                        or "unknown",
                        self._setting_value(getattr(model, "description", None)) or "",
                    )
                    for model in visible
                ],
                subtitle="Switch between Codex models. Applies to this session and future sessions.",
                initial_index=current_index,
                efforts=[self._model_efforts(model) for model in visible],
                initial_effort=getattr(self, "reasoning_effort", None),
                allow_session_only=True,
            )
            if selection is not None:
                selected = visible[selection.index]
                effort = selection.effort
                persist = selection.persist
            else:
                if self._get_renderer()._native_stream and self._get_renderer().claude_like:
                    return
                labels = [
                    (
                        self._setting_value(getattr(model, "model", None)) or "",
                        self._setting_value(getattr(model, "display_name", None)) or "",
                    )
                    for model in visible
                ]
                chosen = self._choose("Model", labels)
                if chosen is None:
                    return
                selected = self._find_catalog_model(catalog, chosen)
                if selected is None:
                    return
                effort = None
        efforts = self._model_efforts(selected)
        if len(arguments) >= 2:
            effort = arguments[1].casefold()
        elif arguments:
            default_effort = self._setting_value(
                getattr(selected, "default_reasoning_effort", None)
            )
            current_effort = getattr(self, "reasoning_effort", None)
            effort = current_effort if current_effort in efforts else default_effort
        elif "effort" not in locals() or effort is None:
            effort = self._choose(
                "Reasoning",
                [(candidate, "") for candidate in efforts],
            )
        if not effort:
            return
        self._apply_model(selected, effort, persist=persist)
        self._write_status(
            f"Model: {self.model} · {self.reasoning_effort}",
            level=RenderLevel.SUCCESS,
        )

    def _handle_fast_command(self, arguments: list[str]) -> None:
        catalog = self._load_model_catalog()
        model = self._current_catalog_model(catalog)
        current = getattr(self, "service_tier", None) or "default"
        action = arguments[0].casefold() if arguments else "prompt"
        if action not in {"prompt", "on", "off", "status"}:
            raise RuntimeError("\u7528\u6cd5\uff1a/fast [on|off|status]")
        if model is None:
            self._fast_tier_id = None
            if action == "status":
                self._write_status("Fast: " + ("off" if current == "default" else current))
                return
            if action not in {"off", "status"}:
                raise RuntimeError("\u76ee\u524d\u6a21\u578b\u4e0d\u5728 catalog\uff0c\u7121\u6cd5\u78ba\u8a8d Fast tier")
            if current != "default":
                self._update_settings_and_persist(
                    {"threadId": self.thread.id, "serviceTier": "default"},
                    [("service_tier", "default")],
                    {"threadId": self.thread.id, "serviceTier": current},
                )
                self.service_tier = "default"
            self._write_status("Fast: off", level=RenderLevel.SUCCESS)
            return
        fast_tier = self._fast_tier(model)
        self._fast_tier_id = fast_tier
        if action == "status":
            self._write_status("Fast: " + ("on" if fast_tier and current == fast_tier else "off"))
            return
        if fast_tier is None and action != "off":
            raise RuntimeError("\u76ee\u524d\u6a21\u578b\u6c92\u6709 Fast tier")
        if action == "prompt":
            label = (
                self._setting_value(getattr(model, "display_name", None))
                or self._setting_value(getattr(model, "model", None))
                or "this model"
            )
            selected = self._get_renderer().select_fast_mode(
                enabled=current == fast_tier,
                model_label=label,
            )
            if selected is None:
                if self._get_renderer()._native_stream and self._get_renderer().claude_like:
                    return
                raise RuntimeError("\u4e92\u52d5\u5f0f\u7d42\u7aef\u8acb\u7528 /fast\uff1b\u975e\u4e92\u52d5\u6a21\u5f0f\u8acb\u7528 /fast on \u6216 /fast off")
            enable = selected
        else:
            enable = action == "on"
        selected_tier = fast_tier if enable else "default"
        self._update_settings_and_persist(
            {"threadId": self.thread.id, "serviceTier": selected_tier},
            [("service_tier", "fast" if enable else "default")],
            {"threadId": self.thread.id, "serviceTier": current},
        )
        self.service_tier = selected_tier
        self._write_status(
            "Fast: " + ("on" if enable else "off"),
            level=RenderLevel.SUCCESS,
        )

    def _edit_keybindings(self) -> None:
        path = DEFAULT_KEYBINDINGS_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(
                json.dumps(
                    {
                        "bindings": [
                            {
                                "context": "Chat",
                                "bindings": {},
                            }
                        ]
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "notepad.exe"
        command = shlex.split(editor, posix=False) + [str(path)]
        result = subprocess.run(command, cwd=self.project_root, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"\u7de8\u8f2f\u5668\u7d50\u675f\u78bc\uff1a{result.returncode}")
        ClaudePromptUI.load_keybindings(path)
        self._write_status("Keybindings reloaded.", level=RenderLevel.SUCCESS)

    def _handle_effort_command(self, argument: str = "") -> None:
        catalog = self._load_model_catalog()
        model = self._current_catalog_model(catalog)
        if model is None:
            raise RuntimeError("\u76ee\u524d\u6a21\u578b\u4e0d\u5728 catalog")
        efforts = self._model_efforts(model)
        requested = argument.strip().casefold()
        if requested:
            if requested not in efforts:
                raise RuntimeError("\u53ef\u7528 effort\uff1a" + ", ".join(efforts))
            effort = requested
        else:
            current = getattr(self, "reasoning_effort", None)
            initial = efforts.index(current) if current in efforts else 0
            selection = self._get_renderer().select_menu(
                "Set effort",
                [(candidate.capitalize(), "") for candidate in efforts],
                subtitle="Controls how much reasoning Codex uses",
                initial_index=initial,
            )
            if selection is None:
                if self._get_renderer()._native_stream and self._get_renderer().claude_like:
                    return
                effort = self._choose("Effort", [(candidate, "") for candidate in efforts])
                if not effort:
                    return
            else:
                effort = efforts[selection.index]
        self._apply_model(model, effort)
        self._write_status(f"Effort: {effort}", level=RenderLevel.SUCCESS)

    def _run_side_question(self, question: str) -> None:
        value = question.strip()
        if not value:
            raise RuntimeError("\u7528\u6cd5\uff1a/btw <question>")
        original = self.thread
        original_id = original.id
        saved_settings = (
            getattr(self, "model", None),
            getattr(self, "reasoning_effort", None),
            getattr(self, "service_tier", None),
        )
        forked = self.client.thread_fork(original_id, self._thread_fork_params())
        side_id = str(forked.thread.id)
        self.thread = self._Thread(self.client, side_id)
        try:
            self.run_turn(
                "[SIDE QUESTION — do not mutate the project]\n"
                "Answer this briefly without tools or project changes. This branch will be discarded.\n\n"
                + value,
                stream_text=True,
            )
        finally:
            self.thread = original
            self.model, self.reasoning_effort, self.service_tier = saved_settings
            try:
                self._delete_thread(side_id)
            finally:
                self._discard_thread_runtime_state(side_id)

    def _thread_fork_params(self, *, last_turn_id: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {
            "cwd": str(self.project_root),
            "developerInstructions": CONTINUOUS_DEVELOPER_INSTRUCTIONS,
        }
        model = getattr(self, "model", None)
        effort = getattr(self, "reasoning_effort", None)
        service_tier = getattr(self, "service_tier", None)
        if model:
            params["model"] = model
        config_overrides: dict[str, Any] = {}
        if effort:
            config_overrides["model_reasoning_effort"] = effort
        permission_profile = getattr(self, "active_permission_profile", None)
        if permission_profile:
            config_overrides["default_permissions"] = permission_profile
        if config_overrides:
            params["config"] = config_overrides
        if service_tier:
            params["serviceTier"] = service_tier
        if last_turn_id:
            params["lastTurnId"] = last_turn_id
        return params

    @classmethod
    def _turn_texts(cls, turn: Any) -> tuple[str, str]:
        data = cls._payload_dict(turn)
        user_parts: list[str] = []
        assistant_parts: list[str] = []
        for item in data.get("items", []) or []:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "userMessage":
                for content in item.get("content", []) or []:
                    if isinstance(content, dict) and content.get("type") == "text":
                        value = content.get("text")
                        if isinstance(value, str):
                            user_parts.append(value)
            elif item_type == "agentMessage":
                value = item.get("text")
                if isinstance(value, str):
                    assistant_parts.append(value)
        return "\n".join(user_parts), "\n".join(assistant_parts)

    @classmethod
    def _turn_user_input(cls, turn: Any) -> tuple[str, bool, bool]:
        data = cls._payload_dict(turn)
        user_parts: list[str] = []
        has_user_input = False
        has_non_text_input = False
        for item in data.get("items", []) or []:
            if not isinstance(item, dict) or item.get("type") != "userMessage":
                continue
            has_user_input = True
            content_items = item.get("content", []) or []
            if not isinstance(content_items, list):
                has_non_text_input = True
                continue
            for content in content_items:
                if not isinstance(content, dict) or content.get("type") != "text":
                    has_non_text_input = True
                    continue
                value = content.get("text")
                if isinstance(value, str):
                    user_parts.append(value)
        return "\n".join(user_parts), has_user_input, has_non_text_input

    @classmethod
    def _turn_id(cls, turn: Any) -> str | None:
        raw_id = getattr(turn, "id", None)
        if raw_id is None:
            raw_id = cls._payload_dict(turn).get("id")
        return str(raw_id) if raw_id is not None else None

    def _logical_user_input(
        self,
        thread_id: str,
        turn: Any,
    ) -> tuple[str, bool, bool]:
        persisted, has_user_input, has_non_text_input = self._turn_user_input(turn)
        if not has_user_input:
            return persisted, False, has_non_text_input
        turn_id = self._turn_id(turn)
        if turn_id is not None:
            override = getattr(self, "_logical_prompts", {}).get((thread_id, turn_id))
            if override is not None:
                return override, True, has_non_text_input
        if persisted.lstrip().startswith("[NATIVE /init REQUEST]"):
            return "/init", True, has_non_text_input
        new_user_marker = "[NEW USER MESSAGE — authoritative current objective]\n"
        if new_user_marker in persisted:
            return persisted.rsplit(new_user_marker, 1)[1], True, has_non_text_input
        return persisted, True, has_non_text_input

    def _remember_logical_prompt(
        self,
        thread_id: str,
        turn_id: str | None,
        prompt: str,
    ) -> None:
        if not turn_id:
            return
        prompts = getattr(self, "_logical_prompts", None)
        if prompts is None:
            prompts = {}
            self._logical_prompts = prompts
        prompts[(thread_id, turn_id)] = prompt

    def _copy_logical_prompts(
        self,
        source_id: str,
        candidate_id: str,
        turns: list[Any] | None = None,
    ) -> None:
        prompts = getattr(self, "_logical_prompts", None)
        if not prompts:
            return
        turn_ids = (
            {turn_id for turn in turns if (turn_id := self._turn_id(turn)) is not None}
            if turns is not None
            else None
        )
        for (thread_id, turn_id), value in list(prompts.items()):
            if thread_id != source_id:
                continue
            if turn_ids is None or turn_id in turn_ids:
                prompts[(candidate_id, turn_id)] = value

    def _drop_logical_prompts(self, thread_ids: list[str]) -> None:
        prompts = getattr(self, "_logical_prompts", None)
        if not prompts:
            return
        doomed = set(thread_ids)
        self._logical_prompts = {
            key: value for key, value in prompts.items() if key[0] not in doomed
        }

    @staticmethod
    def _is_controller_prompt(value: str) -> bool:
        return value.lstrip().startswith(
            (
                "[CONTROLLER-",
                "[CONTROLLER ",
                "[AUTOMATIC FRESH-THREAD HANDOFF]",
                "[AUTOMATIC TRANSPORT RECOVERY",
                "[AUTOMATIC CONTINUATION",
                "[AUTOMATIC UNAFFECTED-WORK CONTINUATION",
                "[GENERIC DISPLAY-FAILURE RECOVERY]",
            )
        )

    def _delete_thread(self, thread_id: str) -> None:
        self.client.request(
            "thread/delete",
            {"threadId": thread_id},
            response_model=self._ThreadDeleteResponse,
        )

    def fork_current_thread(self) -> None:
        if self.thread is None:
            raise RuntimeError("thread is not initialized")
        old_thread_id = self.thread.id
        forked = self.client.thread_fork(
            old_thread_id,
            self._thread_fork_params(),
        )
        self.thread = self._Thread(self.client, forked.thread.id)
        self._adopt_runtime_settings(forked)
        self._copy_logical_prompts(old_thread_id, self.thread.id)
        self._unsubscribe_thread(old_thread_id)
        self._discard_thread_runtime_state(old_thread_id)
        self.previous_input_tokens = None
        self.previous_context_window = None
        self.pending_handoff = None
        self._rewind_sources = []
        self._write_status("\u5df2\u5efa\u7acb\u5206\u652f\u5c0d\u8a71\u3002", level=RenderLevel.SUCCESS)

    def _checkpoint_path(self, raw_path: str) -> Path:
        root = Path(os.path.abspath(str(self.project_root)))
        path = Path(raw_path)
        if not path.is_absolute():
            path = root / path
        path = Path(os.path.abspath(str(path)))
        try:
            relative = path.relative_to(root)
        except ValueError as exc:
            raise RuntimeError(f"checkpoint path is outside the project: {path}") from exc
        current = root
        for part in relative.parts[:-1]:
            current = current / part
            if current.is_symlink():
                raise RuntimeError(f"checkpoint skips symlinked path: {path}")
        return path

    def _snapshot_file(self, path: Path) -> FileSnapshot:
        if os.path.lexists(path) and path.is_symlink():
            raise RuntimeError(f"checkpoint skips symlink: {path}")
        if not path.exists():
            return FileSnapshot(path, False, None, None)
        if not path.is_file():
            raise RuntimeError(f"checkpoint supports files only: {path}")
        file_stat = path.stat()
        if getattr(file_stat, "st_nlink", 1) > 1:
            raise RuntimeError(f"checkpoint skips hard-linked file: {path}")
        if file_stat.st_size > MAX_CHECKPOINT_FILE_BYTES:
            raise RuntimeError(f"checkpoint file is too large: {path}")
        return FileSnapshot(
            path,
            True,
            path.read_bytes(),
            stat.S_IMODE(file_stat.st_mode),
        )

    def _file_item_paths(self, item: dict[str, Any]) -> list[Path]:
        result: list[Path] = []
        for change in item.get("changes", []) or []:
            if not isinstance(change, dict):
                continue
            raw_paths = [str(change.get("path", "")).strip()]
            kind = change.get("kind")
            if isinstance(kind, dict):
                move_path = kind.get("movePath") or kind.get("move_path")
                if move_path:
                    raw_paths.append(str(move_path).strip())
            for raw_path in raw_paths:
                if raw_path:
                    path = self._checkpoint_path(raw_path)
                    if path not in result:
                        result.append(path)
        return result

    def _capture_file_item_before(
        self,
        item: dict[str, Any],
    ) -> dict[Path, FileSnapshot] | None:
        try:
            paths = self._file_item_paths(item)
            snapshots = {path: self._snapshot_file(path) for path in paths}
            total = sum(len(value.content or b"") for value in snapshots.values())
            if not snapshots or total > MAX_CHECKPOINT_TURN_BYTES:
                return None
            return snapshots
        except (OSError, RuntimeError):
            return None

    def _capture_file_item_after(
        self,
        before: dict[Path, FileSnapshot],
    ) -> FileCheckpoint | None:
        try:
            after = {path: self._snapshot_file(path) for path in before}
            total = sum(len(value.content or b"") for value in after.values())
            if total > MAX_CHECKPOINT_TURN_BYTES:
                return None
            return FileCheckpoint(before=dict(before), after=after)
        except (OSError, RuntimeError):
            return None

    @staticmethod
    def _snapshot_matches(left: FileSnapshot, right: FileSnapshot) -> bool:
        return left.exists == right.exists and left.content == right.content

    @staticmethod
    def _write_snapshot(snapshot: FileSnapshot) -> None:
        path = snapshot.path
        if not snapshot.exists:
            if os.path.lexists(path):
                if path.is_symlink() or not path.is_file():
                    raise RuntimeError(f"refusing to remove non-regular path: {path}")
                path.unlink()
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, raw_temp = tempfile.mkstemp(prefix=".codex-rewind-", dir=path.parent)
        temp_path = Path(raw_temp)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(snapshot.content or b"")
            if snapshot.mode is not None:
                os.chmod(temp_path, snapshot.mode)
            os.replace(temp_path, path)
        finally:
            temp_path.unlink(missing_ok=True)

    def _restore_file_checkpoints(self, checkpoints: list[FileCheckpoint]) -> None:
        if not checkpoints:
            raise RuntimeError("No safe file checkpoint is available for this turn.")
        paths = list(
            dict.fromkeys(
                path
                for checkpoint in checkpoints
                for path in checkpoint.before
            )
        )
        current = {path: self._snapshot_file(path) for path in paths}
        target = dict(current)
        for checkpoint in reversed(checkpoints):
            for path, expected in checkpoint.after.items():
                actual = target.get(path)
                if actual is None or not self._snapshot_matches(actual, expected):
                    raise RuntimeError(
                        f"File changed outside this checkpoint; code was not restored: {path}"
                    )
            for path, before in checkpoint.before.items():
                target[path] = before
        try:
            for snapshot in target.values():
                self._write_snapshot(snapshot)
        except BaseException:
            for snapshot in current.values():
                try:
                    self._write_snapshot(snapshot)
                except BaseException:
                    pass
            raise

    def _copy_file_checkpoints(
        self,
        source_id: str,
        candidate_id: str,
        turns: list[Any],
    ) -> None:
        retained = {
            turn_id
            for turn in turns
            if (turn_id := self._turn_id(turn)) is not None
        }
        for (thread_id, turn_id), checkpoints in list(self._file_checkpoints.items()):
            if thread_id == source_id and turn_id in retained:
                self._file_checkpoints[(candidate_id, turn_id)] = list(checkpoints)

    def rewind_previous_exchange(self) -> str | None:
        if self.thread is None:
            raise RuntimeError("thread is not initialized")
        original_thread = self.thread
        original_id = original_thread.id
        lineage = [original_id, *reversed(getattr(self, "_rewind_sources", []))]
        candidates: list[tuple[str, list[Any], int, str, bool, list[str]]] = []
        turns_by_thread: dict[str, list[Any]] = {}
        seen: set[str] = set()
        newer_threads: list[str] = []
        for thread_id in lineage:
            if thread_id in seen:
                continue
            seen.add(thread_id)
            read = self.client.thread_read(thread_id, include_turns=True)
            turns = list(getattr(read.thread, "turns", []) or [])
            turns_by_thread[thread_id] = turns
            for index in range(len(turns) - 1, -1, -1):
                user_text, has_user_input, has_non_text_input = self._logical_user_input(
                    thread_id,
                    turns[index]
                )
                if not has_user_input:
                    continue
                if user_text and self._is_controller_prompt(user_text):
                    continue
                candidates.append(
                    (
                        thread_id,
                        turns,
                        index,
                        user_text,
                        has_non_text_input,
                        list(newer_threads),
                    )
                )
            newer_threads.append(thread_id)
        if not candidates:
            return None
        renderer = self._get_renderer()
        if not renderer._native_stream or not renderer.claude_like:
            raise RuntimeError("Rewind requires an interactive terminal menu.")
        selected_index = 0
        if renderer._native_stream and renderer.claude_like:
            prompt_selection = renderer.select_menu(
                "Rewind",
                [
                    (
                        trim_text((text or "[attachment]").replace("\n", " "), 90),
                        "Most recent prompt" if index == 0 else f"{index + 1} prompts back",
                    )
                    for index, (_thread, _turns, _turn_index, text, _non_text, _newer)
                    in enumerate(candidates[:20])
                ],
                subtitle="Choose a message to rewind to",
            )
            if prompt_selection is None:
                return None
            selected_index = prompt_selection.index
        (
            source_id,
            turns,
            target_index,
            prefill,
            has_non_text_input,
            inspected_controller_threads,
        ) = candidates[selected_index]
        changed_turns: list[tuple[str, Any]] = [
            (source_id, turn) for turn in turns[target_index:]
        ]
        for thread_id in reversed(inspected_controller_threads):
            changed_turns.extend(
                (thread_id, turn) for turn in turns_by_thread.get(thread_id, [])
            )
        file_checkpoints = [
            checkpoint
            for thread_id, turn in changed_turns
            if (turn_id := self._turn_id(turn)) is not None
            for checkpoint in self._file_checkpoints.get((thread_id, turn_id), [])
        ] if ENABLE_LEGACY_FILE_REWIND else []
        action_kind = "conversation"
        if renderer._native_stream and renderer.claude_like:
            action_options = []
            action_kinds = []
            if file_checkpoints:
                action_options.append(
                    (
                        "Restore code and conversation",
                        "Reverse direct file edits and remove later messages",
                    )
                )
                action_kinds.append("both")
            action_options.append(
                (
                    "Restore conversation only",
                    "Remove later messages and refill this prompt",
                )
            )
            action_kinds.append("conversation")
            if file_checkpoints:
                action_options.append(
                    (
                        "Restore code only",
                        "Reverse direct file edits and keep the conversation",
                    )
                )
                action_kinds.append("code")
            action_options.append(("Never mind", "Return without changing anything"))
            action_kinds.append("cancel")
            action = renderer.select_menu(
                "What should be restored?",
                action_options,
                subtitle="Choose what to restore from this checkpoint",
            )
            if action is None:
                return None
            action_kind = action_kinds[action.index]
            if action_kind == "cancel":
                return None
        if action_kind == "code":
            self._restore_file_checkpoints(file_checkpoints)
            self._write_status("Code restored.", level=RenderLevel.SUCCESS)
            return None
        if has_non_text_input:
            raise RuntimeError("\u4e0a\u4e00\u8f2a\u542b\u5716\u7247\u6216\u9644\u4ef6\uff1b\u70ba\u907f\u514d\u8cc7\u6599\u907a\u5931\uff0c\u672a\u56de\u6efe\u3002")

        candidate_response: Any
        retained_turns = turns[:target_index]
        if target_index > 0:
            candidate_response = self.client.thread_fork(
                source_id,
                self._thread_fork_params(last_turn_id=str(turns[target_index - 1].id)),
            )
        else:
            candidate_response = self.client.thread_start(self._thread_start_params())
        candidate_id = str(candidate_response.thread.id)
        candidate_thread = self._Thread(self.client, candidate_id)
        if action_kind == "both":
            try:
                self._restore_file_checkpoints(file_checkpoints)
            except BaseException:
                try:
                    self.client.thread_archive(candidate_id)
                except BaseException:
                    pass
                raise
        replaced_ids: list[str] = []
        for thread_id in [*reversed(inspected_controller_threads), source_id]:
            if thread_id != candidate_id and thread_id not in replaced_ids:
                replaced_ids.append(thread_id)
        self.thread = candidate_thread
        self._adopt_runtime_settings(candidate_response)
        self._copy_logical_prompts(source_id, candidate_id, retained_turns)
        self._copy_file_checkpoints(source_id, candidate_id, retained_turns)
        self._drop_logical_prompts(replaced_ids)
        self._file_checkpoints = {
            key: value
            for key, value in self._file_checkpoints.items()
            if key[0] not in set(replaced_ids)
        }
        self.latest_user = ""
        self.latest_assistant = ""
        for retained_turn in reversed(retained_turns):
            retained_user, has_user_input, _has_non_text = self._logical_user_input(
                candidate_id,
                retained_turn
            )
            if not has_user_input or self._is_controller_prompt(retained_user):
                continue
            self.latest_user = retained_user
            _ignored_user, self.latest_assistant = self._turn_texts(retained_turn)
            break
        self.pending_handoff = None
        self._pending_turn_inputs = []
        self.previous_input_tokens = None
        self.previous_context_window = None
        self._rewind_sources = [
            thread_id
            for thread_id in getattr(self, "_rewind_sources", [])
            if thread_id not in replaced_ids
        ]
        for thread_id in replaced_ids:
            try:
                self.client.thread_archive(thread_id)
            except BaseException as exc:
                self._write_status(
                    "\u820a checkpoint thread \u672a\u80fd\u5c01\u5b58\uff1a" + trim_text(str(exc), 200),
                    verbose_only=True,
                    level=RenderLevel.DETAIL,
                )
            self._unsubscribe_thread(thread_id)
            self._discard_thread_runtime_state(thread_id)
        self._prefill_prompt = prefill
        with self._output_lock:
            renderer = self._get_renderer()
            renderer.reset_conversation_view(clear_input_history=True)
            renderer.clear_screen()
            self._sync_render_state()
        return prefill

    def _write_output_block(self, value: str) -> None:
        with self._output_lock:
            self._get_renderer().output_block(value)
            self._sync_render_state()

    def _copy_latest_response(self) -> None:
        value = getattr(self, "latest_assistant", "")
        if not value:
            raise RuntimeError("\u76ee\u524d\u6c92\u6709\u53ef\u8907\u88fd\u7684\u56de\u8986")
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "Set-Clipboard -Value ([Console]::In.ReadToEnd())",
            ],
            input=value,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(trim_text(result.stderr or "clipboard failed", 300))
        self._write_status("\u5df2\u8907\u88fd\u3002", level=RenderLevel.SUCCESS)

    def _git_diff(self) -> str:
        safe_git = ["git", "-c", "core.hooksPath=NUL"]
        probe = subprocess.run(
            safe_git + ["rev-parse", "--is-inside-work-tree"],
            cwd=self.project_root,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        if probe.returncode != 0:
            raise RuntimeError("\u76ee\u524d\u76ee\u9304\u4e0d\u662f Git worktree")
        sections: list[str] = []
        diff_flags = [
            "--no-textconv",
            "--no-ext-diff",
            "--submodule=short",
            "--ignore-submodules=dirty",
            "--no-color",
        ]
        for heading, arguments in (
            ("staged", safe_git + ["diff", "--cached", *diff_flags]),
            ("unstaged", safe_git + ["diff", *diff_flags]),
        ):
            result = subprocess.run(
                arguments,
                cwd=self.project_root,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=20,
                check=False,
            )
            if result.returncode not in {0, 1}:
                raise RuntimeError(trim_text(result.stderr or f"git {heading} failed", 300))
            if result.stdout.strip():
                sections.append(result.stdout.rstrip())
        untracked = subprocess.run(
            safe_git + ["ls-files", "--others", "--exclude-standard", "-z"],
            cwd=self.project_root,
            capture_output=True,
            timeout=10,
            check=False,
        )
        if untracked.returncode != 0:
            raise RuntimeError(trim_text(untracked.stderr.decode("utf-8", "replace"), 300))
        for raw_name in untracked.stdout.split(b"\0"):
            if not raw_name:
                continue
            relative = raw_name.decode("utf-8", "replace")
            path = (self.project_root / relative).resolve()
            try:
                path.relative_to(self.project_root.resolve())
            except ValueError:
                continue
            if not path.is_file():
                continue
            if path.stat().st_size > 262_144:
                sections.append(f"Untracked large file: {relative}")
                continue
            result = subprocess.run(
                safe_git
                + [
                    "diff",
                    "--no-index",
                    "--no-textconv",
                    "--no-ext-diff",
                    "--no-color",
                    "--",
                    "NUL",
                    str(path),
                ],
                cwd=self.project_root,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=20,
                check=False,
            )
            if result.returncode not in {0, 1}:
                sections.append(f"Untracked: {relative}")
            elif result.stdout.strip():
                sections.append(result.stdout.rstrip())
            if sum(len(section) for section in sections) >= 20_000:
                break
        output = "\n\n".join(sections).strip()
        if not output:
            return "No changes."
        return trim_text(output, 20_000)

    def _show_mcp(self, *, verbose: bool) -> None:
        cursor: str | None = None
        servers: list[dict[str, Any]] = []
        while True:
            response = self._raw_request(
                "mcpServerStatus/list",
                {
                    "threadId": self.thread.id,
                    "detail": "full" if verbose else "toolsAndAuthOnly",
                    "cursor": cursor,
                    "limit": 100,
                },
            )
            if not isinstance(response, dict):
                raise RuntimeError("mcpServerStatus/list returned invalid response")
            servers.extend(item for item in response.get("data", []) if isinstance(item, dict))
            cursor = response.get("nextCursor")
            if not isinstance(cursor, str) or not cursor:
                break
        lines: list[str] = []
        for server in servers:
            tools = server.get("tools") or {}
            auth = server.get("authStatus")
            line = f"{server.get('name', '?')}  {auth}  {len(tools) if isinstance(tools, dict) else 0} tools"
            lines.append(line)
            if verbose and isinstance(tools, dict):
                lines.extend(f"  {name}" for name in tools)
        self._write_output_block("\n".join(lines) if lines else "No MCP servers configured.")

    def _handle_personality_command(self, argument: str) -> None:
        choices = ("friendly", "pragmatic", "none")
        selected = argument.casefold().strip()
        if not selected:
            selected = self._choose("Personality", [(value, "") for value in choices]) or ""
        if selected not in choices:
            raise RuntimeError("\u7528\u6cd5\uff1a/personality [friendly|pragmatic|none]")
        self._update_settings_and_persist(
            {
                "threadId": self.thread.id,
                "personality": None if selected == "none" else selected,
            },
            [("personality", None if selected == "none" else selected)],
            {
                "threadId": self.thread.id,
                "personality": getattr(self, "personality", None),
            },
        )
        self.personality = None if selected == "none" else selected
        self._write_status(f"Personality: {selected}", level=RenderLevel.SUCCESS)

    def _handle_goal_command(self, argument: str) -> None:
        value = argument.strip()
        folded = value.casefold()
        if not value:
            response = self._raw_request("thread/goal/get", {"threadId": self.thread.id})
            goal = response.get("goal") if isinstance(response, dict) else None
            if not isinstance(goal, dict):
                self._write_status("Goal: none")
            else:
                self._write_status(
                    f"Goal: {goal.get('status', 'unknown')} · "
                    + trim_text(str(goal.get("objective", "")), 300)
                )
            return
        if folded == "clear":
            self.client.thread_goal_clear(self.thread.id)
            self._write_status("Goal cleared.", level=RenderLevel.SUCCESS)
            return
        raise RuntimeError("\u7528\u6cd5\uff1a/goal \u6216 /goal clear")

    def _handle_rename(self, name: str) -> None:
        name = name.strip().strip('"')
        if not name:
            name = input("Name: ").strip()
        if not name:
            return
        self.client.thread_set_name(self.thread.id, name)
        self._write_status("\u5df2\u91cd\u65b0\u547d\u540d\u3002", level=RenderLevel.SUCCESS)

    def _start_manual_compaction(self) -> None:
        self._manual_compaction_thread = self.thread.id
        self._manual_compaction_done.clear()
        self.thread.compact()
        self._write_status("\u6b63\u5728\u58d3\u7e2e\u5c0d\u8a71…", level=RenderLevel.PROGRESS)

    def _settle_manual_compaction(self) -> None:
        thread_id = getattr(self, "_manual_compaction_thread", None)
        if not thread_id:
            return
        if not self._manual_compaction_done.wait(timeout=60):
            raise RuntimeError("\u5c0d\u8a71\u58d3\u7e2e\u5c1a\u672a\u5b8c\u6210")
        self._compacted_threads.discard(thread_id)
        self._manual_compaction_thread = None
        self.previous_input_tokens = None
        self.previous_context_window = None

    def _handle_usage(self, argument: str) -> None:
        mode = argument.strip().casefold() or "daily"
        aliases = {"day": "daily", "week": "weekly", "total": "cumulative"}
        mode = aliases.get(mode, mode)
        if mode not in {"daily", "weekly", "cumulative"}:
            raise RuntimeError("\u7528\u6cd5\uff1a/usage [daily|weekly|cumulative]")
        response = self._raw_request("account/usage/read", None)
        if not isinstance(response, dict):
            raise RuntimeError("usage response is invalid")
        buckets = response.get("dailyUsageBuckets") or []
        summary = response.get("summary") or {}
        if mode == "cumulative":
            tokens = summary.get("lifetimeTokens")
            output = f"Lifetime: {tokens:,} tokens" if isinstance(tokens, int) else "Lifetime usage unavailable."
        else:
            count = 7 if mode == "weekly" else 1
            ordered = sorted(
                (item for item in buckets if isinstance(item, dict)),
                key=lambda item: str(item.get("startDate", "")),
            ) if isinstance(buckets, list) else []
            selected = ordered[-count:]
            lines = [
                f"{item.get('startDate', '?')}  {int(item.get('tokens', 0)):,}"
                for item in selected
                if isinstance(item, dict)
            ]
            output = "\n".join(lines) if lines else "Usage unavailable."
        self._write_output_block(output)

    def _handle_review(self, argument: str) -> Any:
        value = argument.strip()
        if not value or value.casefold() in {"working-tree", "uncommitted", "changes"}:
            target: dict[str, Any] = {"type": "uncommittedChanges"}
        elif value.casefold().startswith("base "):
            target = {"type": "baseBranch", "branch": value[5:].strip()}
        elif value.casefold().startswith("commit "):
            target = {"type": "commit", "sha": value[7:].strip()}
        else:
            target = {"type": "custom", "instructions": value}
        response = self._raw_request(
            "review/start",
            {"threadId": self.thread.id, "delivery": "inline", "target": target},
        )
        if not isinstance(response, dict):
            raise RuntimeError("review/start returned invalid response")
        thread_id = response.get("reviewThreadId")
        turn = response.get("turn") or {}
        turn_id = turn.get("id") if isinstance(turn, dict) else None
        if not isinstance(thread_id, str) or not isinstance(turn_id, str):
            raise RuntimeError("review/start did not return a turn")
        return self._TurnHandle(self.client, thread_id, turn_id)

    def _handle_skills(self, argument: str) -> None:
        response = self._raw_request(
            "skills/list",
            {"cwds": [str(self.project_root)], "forceReload": False},
        )
        entries = response.get("data", []) if isinstance(response, dict) else []
        skills: list[dict[str, Any]] = []
        for entry in entries:
            if isinstance(entry, dict):
                skills.extend(item for item in entry.get("skills", []) if isinstance(item, dict))
        enabled = [item for item in skills if item.get("enabled", True)]
        requested = argument.strip()
        if not requested:
            chosen = self._choose(
                "Skills",
                [
                    (str(item.get("name", "")), trim_text(str(item.get("description", "")), 100))
                    for item in enabled
                    if item.get("name")
                ],
            )
            if not chosen:
                return
            requested = chosen
        matches = [
            item for item in enabled if str(item.get("name", "")).casefold() == requested.casefold()
        ]
        if len(matches) != 1:
            raise RuntimeError("\u627e\u4e0d\u5230\u552f\u4e00 skill")
        skill = matches[0]
        self._pending_turn_inputs.append(
            self._SkillInput(str(skill["name"]), str(skill["path"]))
        )
        self._write_status(f"Skill ready: {skill['name']}", level=RenderLevel.SUCCESS)

    def _handle_apps(self, argument: str) -> None:
        response = self._raw_request(
            "app/list",
            {"threadId": self.thread.id, "forceRefetch": False},
        )
        apps = response.get("data", []) if isinstance(response, dict) else []
        available = [
            item
            for item in apps
            if isinstance(item, dict) and item.get("isEnabled", True) and item.get("isAccessible", False)
        ]
        requested = argument.strip()
        if not requested:
            requested = self._choose(
                "Apps",
                [
                    (str(item.get("name", "")), trim_text(str(item.get("description", "")), 100))
                    for item in available
                    if item.get("name")
                ],
            ) or ""
        matches = [
            item
            for item in available
            if requested.casefold() in {
                str(item.get("name", "")).casefold(),
                str(item.get("id", "")).casefold(),
            }
        ]
        if len(matches) != 1:
            if not requested and not available:
                self._write_status("No apps available.")
                return
            raise RuntimeError("\u627e\u4e0d\u5230\u552f\u4e00 app")
        app = matches[0]
        self._pending_turn_inputs.append(
            self._MentionInput(str(app["name"]), f"app://{app['id']}")
        )
        self._write_status(f"App ready: {app['name']}", level=RenderLevel.SUCCESS)

    def _show_plugins(self) -> None:
        response = self._raw_request(
            "plugin/list",
            {"cwds": [str(self.project_root)], "forceRefetch": False},
        )
        marketplaces = response.get("marketplaces", []) if isinstance(response, dict) else []
        lines: list[str] = []
        for marketplace in marketplaces:
            if not isinstance(marketplace, dict):
                continue
            for plugin in marketplace.get("plugins", []) or []:
                if isinstance(plugin, dict):
                    state = "on" if plugin.get("enabled") else "off"
                    installed = "installed" if plugin.get("installed") else "available"
                    lines.append(f"{plugin.get('name', plugin.get('id', '?'))}  {installed}/{state}")
        self._write_output_block("\n".join(lines) if lines else "No plugins.")

    def _show_hooks(self) -> None:
        response = self._raw_request("hooks/list", {"cwds": [str(self.project_root)]})
        entries = response.get("data", []) if isinstance(response, dict) else []
        lines: list[str] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            for hook in entry.get("hooks", []) or []:
                if isinstance(hook, dict):
                    lines.append(
                        f"{hook.get('key', '?')}  {hook.get('eventName', '?')}  "
                        f"{'on' if hook.get('enabled') else 'off'}"
                    )
        self._write_output_block("\n".join(lines) if lines else "No hooks.")

    def _handle_permissions(self, argument: str) -> None:
        response = self._raw_request(
            "permissionProfile/list",
            {"cwd": str(self.project_root)},
        )
        profiles = response.get("data", []) if isinstance(response, dict) else []
        profiles = [item for item in profiles if isinstance(item, dict) and item.get("allowed", True)]
        requested = argument.strip().casefold()
        aliases = {
            "read-only": ":read-only",
            "readonly": ":read-only",
            "workspace": ":workspace",
            "auto": ":workspace",
            "full": ":danger-full-access",
            "danger-full-access": ":danger-full-access",
        }
        requested = aliases.get(requested, requested)
        if not requested:
            requested = self._choose(
                "Permissions",
                [
                    (str(item.get("id", "")), str(item.get("description", "")))
                    for item in profiles
                    if item.get("id")
                ],
            ) or ""
        matches = [item for item in profiles if str(item.get("id", "")).casefold() == requested]
        if len(matches) != 1:
            raise RuntimeError("\u627e\u4e0d\u5230 permission profile")
        if requested == ":danger-full-access":
            if not self._confirm(
                "Enable full access?",
                "Allow commands and file changes outside the workspace sandbox",
            ):
                return
        self._raw_request(
            "thread/settings/update",
            {"threadId": self.thread.id, "permissions": matches[0]["id"]},
        )
        self.active_permission_profile = str(matches[0]["id"])
        self._write_status(f"Permissions: {matches[0]['id']}", level=RenderLevel.SUCCESS)

    def _show_background_terminals(self) -> None:
        response = self._raw_request(
            "thread/backgroundTerminals/list",
            {"threadId": self.thread.id, "cursor": None, "limit": None},
        )
        processes = response.get("data", []) if isinstance(response, dict) else []
        lines = [
            f"{item.get('processId', '?')}  {trim_text(str(item.get('command', '')), 120)}"
            for item in processes
            if isinstance(item, dict)
        ]
        self._write_output_block("\n".join(lines) if lines else "No background terminals.")

    @staticmethod
    def _config_key_names(value: Any) -> list[str]:
        if not isinstance(value, dict):
            return []
        return sorted(str(key) for key in value)

    def _show_debug_config(self) -> None:
        config = self._raw_request(
            "config/read",
            {"cwd": str(self.project_root), "includeLayers": True},
        )
        requirements = self._raw_request("configRequirements/read", None)
        response = config if isinstance(config, dict) else {}
        effective = response.get("config")
        summary = {
            "effective": {
                "model": getattr(self, "model", None) or "default",
                "reasoning_effort": getattr(self, "reasoning_effort", None) or "default",
                "service_tier": getattr(self, "service_tier", None) or "default",
                "permissions": getattr(self, "active_permission_profile", None) or "config",
                "personality": getattr(self, "personality", None) or "none",
            },
            "config_keys": self._config_key_names(effective),
            "layer_entries": len(response.get("layers", []) or []),
            "origin_keys": self._config_key_names(response.get("origins")),
            "requirement_keys": self._config_key_names(requirements),
        }
        self._write_output_block(json.dumps(summary, ensure_ascii=False, indent=2))

    def _resume_saved_thread(self, argument: str) -> None:
        response = self.client.thread_list(
            {
                "cwd": str(self.project_root),
                "limit": 30,
                "sortKey": "updated_at",
                "sortDirection": "desc",
            }
        )
        current_id = self.thread.id
        threads = [item for item in response.data if item.id != current_id]
        requested = argument.strip()
        selected: Any | None = None
        if not requested:
            choice = self._choose(
                "Resume",
                [
                    (
                        item.id,
                        trim_text(str(item.name or item.preview or "untitled"), 100),
                    )
                    for item in threads
                ],
            )
            if not choice:
                return
            selected = next((item for item in threads if item.id == choice), None)
        else:
            exact = [
                item
                for item in threads
                if requested.casefold()
                in {item.id.casefold(), str(item.name or "").casefold()}
            ]
            if len(exact) == 1:
                selected = exact[0]
            else:
                prefixes = [item for item in threads if item.id.casefold().startswith(requested.casefold())]
                if len(prefixes) == 1:
                    selected = prefixes[0]
        if selected is None:
            raise RuntimeError("\u627e\u4e0d\u5230\u552f\u4e00\u5c0d\u8a71")
        old_empty = False
        try:
            old_read = self.client.thread_read(current_id, include_turns=True)
            old_empty = not list(getattr(old_read.thread, "turns", []) or [])
        except BaseException:
            old_empty = False
        resume_params: dict[str, Any] = {
            "cwd": str(self.project_root),
            "developerInstructions": CONTINUOUS_DEVELOPER_INSTRUCTIONS,
        }
        permission_profile = getattr(self, "active_permission_profile", None)
        if permission_profile:
            resume_params["config"] = {"default_permissions": permission_profile}
        resumed = self.client.thread_resume(selected.id, resume_params)
        old_id = self.thread.id
        self.thread = self._Thread(self.client, resumed.thread.id)
        self._adopt_runtime_settings(resumed)
        old_deleted = False
        if old_empty:
            try:
                self._delete_thread(old_id)
                old_deleted = True
            except BaseException as exc:
                self._unsubscribe_thread(old_id)
                self._write_status(
                    "\u7a7a\u767d thread \u6e05\u7406\u5931\u6557\uff1a" + trim_text(str(exc), 300),
                    verbose_only=True,
                    level=RenderLevel.DETAIL,
                )
        else:
            self._unsubscribe_thread(old_id)
        self._discard_thread_runtime_state(old_id)
        turns = list(getattr(resumed.thread, "turns", []) or [])
        self.latest_user = ""
        self.latest_assistant = ""
        for historical_turn in reversed(turns):
            historical_user, has_user_input, _has_non_text = self._logical_user_input(
                self.thread.id,
                historical_turn,
            )
            if not has_user_input or self._is_controller_prompt(historical_user):
                continue
            self.latest_user = historical_user
            _ignored_user, self.latest_assistant = self._turn_texts(historical_turn)
            break
        if old_deleted:
            self._drop_logical_prompts([old_id])
        self.pending_handoff = None
        self._pending_turn_inputs = []
        self._rewind_sources = []
        self._prefill_prompt = ""
        self.previous_input_tokens = None
        self.previous_context_window = None
        with self._output_lock:
            self._get_renderer().clear_screen()
            self._sync_render_state()
        self._write_status("\u5df2\u63a5\u7e8c\u5c0d\u8a71\u3002", level=RenderLevel.SUCCESS)

    def _queue_mention(self, argument: str) -> None:
        value = argument.strip().strip('"')
        if not value:
            raise RuntimeError("\u7528\u6cd5\uff1a/mention <file-or-directory>")
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = self.project_root / path
        path = path.resolve()
        if not path.exists():
            raise RuntimeError(f"\u8def\u5f91\u4e0d\u5b58\u5728\uff1a{path}")
        self._pending_turn_inputs.append(self._MentionInput(path.name, str(path)))
        self._write_status(f"Mention ready: {path.name}", level=RenderLevel.SUCCESS)

    def handle_slash_command(self, prompt: str) -> SlashCommandOutcome:
        stripped = prompt.strip().lstrip("\ufeff").lstrip()
        if stripped == "?":
            command, argument = "/help", ""
        elif stripped.startswith("/"):
            command, _separator, argument = stripped.partition(" ")
            command = command.casefold()
        else:
            return SlashCommandOutcome(False)

        skill = getattr(self, "_slash_skills", {}).get(command)
        if skill is not None:
            self._pending_turn_inputs.append(
                self._SkillInput(str(skill["name"]), str(skill["path"]))
            )
            turn_prompt = argument.strip() or (
                f"Apply the {skill['name']} skill to the current project objective."
            )
            return SlashCommandOutcome(True, turn_prompt=turn_prompt)
        if command not in SLASH_COMMAND_NAMES and command not in {
            "/details",
            "/transcript",
        }:
            self._write_status("\u672a\u77e5\u6307\u4ee4\uff1b\u8f38\u5165 / \u67e5\u770b\u6e05\u55ae\u3002", level=RenderLevel.WARNING)
            return SlashCommandOutcome(True)
        if command in {"/exit", "/quit"}:
            return SlashCommandOutcome(True, exit_requested=True)
        if command == "/help":
            renderer = self._get_renderer()
            if renderer._native_stream and renderer.claude_like:
                options = list(getattr(self, "_prompt_commands", SLASH_COMMANDS))
                selection = renderer.select_menu(
                    "Commands",
                    options,
                    subtitle="Choose a command · Esc to return",
                )
                return SlashCommandOutcome(
                    True,
                    prefill=(options[selection.index][0] + " ")
                    if selection is not None
                    else None,
                )
            with self._output_lock:
                renderer.help()
                self._sync_render_state()
            return SlashCommandOutcome(True)
        if command == "/status":
            self._write_status(self.status())
            return SlashCommandOutcome(True)
        if command == "/usage":
            self._handle_usage(argument)
            return SlashCommandOutcome(True)
        if command == "/transcript":
            renderer = self._get_renderer()
            renderer.show_transcript(renderer.transcript_text())
            return SlashCommandOutcome(True)
        if command in {"/verbose", "/details"}:
            self.verbose = not self.verbose
            self._write_status("\u5de5\u5177\u7d30\u7bc0\uff1a" + ("\u958b" if self.verbose else "\u95dc") + "\u3002")
            return SlashCommandOutcome(True)
        if command == "/model":
            self._handle_model_command(argument.split())
            return SlashCommandOutcome(True)
        if command == "/effort":
            self._handle_effort_command(argument)
            return SlashCommandOutcome(True)
        if command == "/fast":
            self._handle_fast_command(argument.split())
            return SlashCommandOutcome(True)
        if command == "/keybindings":
            self._edit_keybindings()
            return SlashCommandOutcome(True)
        if command in {"/rewind", "/undo", "/checkpoint"}:
            prefill = self.rewind_previous_exchange()
            return SlashCommandOutcome(True, prefill=prefill)
        if command in {"/branch", "/fork"}:
            self.fork_current_thread()
            return SlashCommandOutcome(True)
        if command == "/btw":
            self._run_side_question(argument)
            return SlashCommandOutcome(True)
        if command in {"/new", "/clear"}:
            if command == "/clear":
                self.start_cleared_thread()
            else:
                self.start_fresh_thread()
            self.latest_user = ""
            self.latest_assistant = ""
            self.pending_handoff = None
            self._prefill_prompt = ""
            self._pending_turn_inputs = []
            self._rewind_sources = []
            if argument.strip():
                self.client.thread_set_name(self.thread.id, argument.strip())
            return SlashCommandOutcome(True)
        if command == "/resume":
            self._resume_saved_thread(argument)
            return SlashCommandOutcome(True)
        if command == "/compact":
            self._start_manual_compaction()
            return SlashCommandOutcome(True)
        if command == "/copy":
            self._copy_latest_response()
            return SlashCommandOutcome(True)
        if command == "/diff":
            self._write_output_block(self._git_diff())
            return SlashCommandOutcome(True)
        if command == "/mcp":
            mcp_argument = argument.strip().casefold()
            if mcp_argument not in {"", "verbose"}:
                raise RuntimeError("\u7528\u6cd5\uff1a/mcp [verbose]")
            self._show_mcp(verbose=mcp_argument == "verbose")
            return SlashCommandOutcome(True)
        if command == "/mention":
            self._queue_mention(argument)
            return SlashCommandOutcome(True)
        if command == "/skills":
            self._handle_skills(argument)
            return SlashCommandOutcome(True)
        if command == "/apps":
            self._handle_apps(argument)
            return SlashCommandOutcome(True)
        if command == "/plugins":
            self._show_plugins()
            return SlashCommandOutcome(True)
        if command == "/hooks":
            self._show_hooks()
            return SlashCommandOutcome(True)
        if command == "/permissions":
            self._handle_permissions(argument)
            return SlashCommandOutcome(True)
        if command == "/ps":
            self._show_background_terminals()
            return SlashCommandOutcome(True)
        if command == "/stop":
            self._raw_request(
                "thread/backgroundTerminals/clean",
                {"threadId": self.thread.id},
            )
            self._write_status("Background terminals stopped.", level=RenderLevel.SUCCESS)
            return SlashCommandOutcome(True)
        if command == "/debug-config":
            self._show_debug_config()
            return SlashCommandOutcome(True)
        if command == "/rename":
            self._handle_rename(argument)
            return SlashCommandOutcome(True)
        if command == "/personality":
            self._handle_personality_command(argument)
            return SlashCommandOutcome(True)
        if command == "/goal":
            self._handle_goal_command(argument)
            return SlashCommandOutcome(True)
        if command == "/review":
            return SlashCommandOutcome(
                True,
                turn_handle=self._handle_review(argument),
            )
        if command == "/init":
            agents_path = self.project_root / "AGENTS.md"
            if agents_path.exists():
                self._write_status("AGENTS.md \u5df2\u5b58\u5728\uff1b\u672a\u4fee\u6539\u3002")
                return SlashCommandOutcome(True)
            return SlashCommandOutcome(
                True,
                turn_prompt=(
                    "[NATIVE /init REQUEST]\n"
                    f"Create {agents_path} with concise, durable project instructions. "
                    "Never overwrite or modify an existing AGENTS.md; stop without changes if it now exists."
                ),
            )
        if command == "/archive":
            self.client.thread_archive(self.thread.id)
            return SlashCommandOutcome(True, exit_requested=True)
        if command == "/delete":
            if self._confirm(
                "Delete this conversation permanently?",
                "The server thread cannot be recovered",
            ):
                self._delete_thread(self.thread.id)
                return SlashCommandOutcome(True, exit_requested=True)
            return SlashCommandOutcome(True)
        if command == "/logout":
            if self._confirm("Sign out of Codex?", "Remove the current account session"):
                self.client.account_logout()
                return SlashCommandOutcome(True, exit_requested=True)
            return SlashCommandOutcome(True)

        self._write_status(
            f"{command} \u76ee\u524d\u9700\u8981\u539f\u751f Codex TUI\u3002",
            level=RenderLevel.WARNING,
        )
        return SlashCommandOutcome(True)

    def status(self) -> str:
        model = getattr(self, "model", None) or "default"
        effort = getattr(self, "reasoning_effort", None) or "default"
        service_tier = getattr(self, "service_tier", None) or "default"
        fast_tier = getattr(self, "_fast_tier_id", None)
        permissions = getattr(self, "active_permission_profile", None) or "config"
        fast_status = (
            "on"
            if fast_tier and service_tier == fast_tier
            else "off"
            if service_tier == "default"
            else service_tier
        )
        context = "unknown"
        if self.previous_input_tokens is not None and self.previous_context_window:
            used = self.previous_input_tokens / self.previous_context_window
            context = f"{used:.0%}"
        elif self.previous_input_tokens is not None:
            context = f"{self.previous_input_tokens} tokens"
        concise = (
            f"{model} · {effort} · Fast {fast_status} · "
            f"permissions {permissions} · context {context}"
        )
        if not self.verbose:
            return concise
        thread_id = self.thread.id if self.thread is not None else "none"
        return (
            concise
            + f" · thread {thread_id} · runtime {self.server_version}/sdk {self.sdk_version}"
            + f" · rollovers {self.rollovers} · threshold {self.rollover_ratio:.0%}"
            + f" · handoff {'ready' if self.pending_handoff else 'none'}"
        )

    def _prompt_footer(self) -> str:
        renderer = self._get_renderer()
        separator = " · " if renderer.capabilities.unicode else " | "
        model = getattr(self, "model", None) or "default"
        effort = getattr(self, "reasoning_effort", None)
        fast_tier = getattr(self, "_fast_tier_id", None)
        service_tier = getattr(self, "service_tier", None) or "default"
        model_with_reasoning = " ".join(
            value
            for value in (
                model,
                effort,
                "fast" if fast_tier and service_tier == fast_tier else None,
            )
            if value
        )
        values = [model_with_reasoning]
        if self.previous_input_tokens is not None and self.previous_context_window:
            remaining = max(
                0.0,
                1.0 - self.previous_input_tokens / self.previous_context_window,
            )
            values.append(f"Context {remaining:.0%} left")
        values.append(str(self.project_root))
        return separator.join(values)

    def _prompt_right_hint(self) -> str:
        effort = getattr(self, "reasoning_effort", None)
        return f"○ {effort} · /effort" if effort else ""

    def _prompt_tasks(self) -> list[str]:
        if not getattr(self, "_todos_visible", False):
            return []
        result: list[str] = []
        for item in getattr(self, "_todo_plan", [])[:8]:
            status = str(item.get("status", "pending")).casefold()
            marker = "☒" if status == "completed" else "◐" if status in {
                "inprogress",
                "in_progress",
                "in-progress",
            } else "☐"
            result.append(f"{marker} {trim_text(str(item.get('step', '')), 100)}")
        return result

    def _toggle_todos(self) -> None:
        self._todos_visible = not getattr(self, "_todos_visible", False)

    def _cycle_permissions(self) -> None:
        response = self._raw_request(
            "permissionProfile/list",
            {"cwd": str(self.project_root)},
        )
        profiles = response.get("data", []) if isinstance(response, dict) else []
        allowed = [
            str(item.get("id"))
            for item in profiles
            if isinstance(item, dict)
            and item.get("allowed", True)
            and item.get("id")
            and item.get("id") != ":danger-full-access"
        ]
        if not allowed:
            return
        current = getattr(self, "active_permission_profile", None)
        index = allowed.index(current) if current in allowed else -1
        selected = allowed[(index + 1) % len(allowed)]
        self._raw_request(
            "thread/settings/update",
            {"threadId": self.thread.id, "permissions": selected},
        )
        self.active_permission_profile = selected

    def _toggle_tool_details_in_prompt(self) -> None:
        self.verbose = not self.verbose

    def _run_shell_mode(self, command: str) -> None:
        value = command.strip()
        if not value:
            return
        renderer = self._get_renderer()
        with self._output_lock:
            renderer.output_block("! " + value)
            self._sync_render_state()
        process = subprocess.Popen(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-Command", value],
            cwd=self.project_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        captured: list[str] = []
        try:
            assert process.stdout is not None
            for line in process.stdout:
                clean = line.rstrip("\r\n")
                captured.append(clean)
                with self._output_lock:
                    renderer.output_block("  " + clean)
                    self._sync_render_state()
        except KeyboardInterrupt:
            process.terminate()
            process.wait(timeout=3)
            self._write_status("Shell command interrupted.", level=RenderLevel.WARNING)
            return
        return_code = process.wait()
        transcript = trim_text("\n".join(captured), MAX_COMMAND_OUTPUT_BUFFER_CHARS)
        self._pending_shell_context.append(
            f"Command: {value}\nExit code: {return_code}\nOutput:\n{transcript}"
        )
        if len(self._pending_shell_context) > 4:
            del self._pending_shell_context[:-4]
        if return_code != 0:
            self._write_status(f"Shell exited with code {return_code}.", level=RenderLevel.WARNING)

    def _edit_prompt_in_editor(self, draft: str) -> str:
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "notepad.exe"
        descriptor, raw_path = tempfile.mkstemp(prefix="codex-prompt-", suffix=".md")
        os.close(descriptor)
        path = Path(raw_path)
        try:
            path.write_text(draft, encoding="utf-8")
            command = shlex.split(editor, posix=False) + [str(path)]
            subprocess.run(command, cwd=self.project_root, check=False)
            return path.read_text(encoding="utf-8", errors="replace")
        finally:
            path.unlink(missing_ok=True)

    def _paste_clipboard_image(self) -> None:
        descriptor, raw_path = tempfile.mkstemp(prefix="codex-clipboard-", suffix=".png")
        os.close(descriptor)
        path = Path(raw_path)
        script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "Add-Type -AssemblyName System.Drawing; "
            "$image=[System.Windows.Forms.Clipboard]::GetImage(); "
            "if ($null -eq $image) { exit 2 }; "
            "$image.Save($args[0],[System.Drawing.Imaging.ImageFormat]::Png)"
        )
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-STA",
                "-Command",
                script,
                str(path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if result.returncode != 0 or not path.exists() or path.stat().st_size == 0:
            path.unlink(missing_ok=True)
            raise RuntimeError("Clipboard does not contain an image.")
        self._pending_temp_images.append(path)
        self._pending_turn_inputs.append(self._LocalImageInput(str(path)))
        self._write_status("Image pasted.", level=RenderLevel.SUCCESS)

    def _adopt_outcome(self, outcome: TurnOutcome) -> None:
        self.latest_assistant = outcome.final_response or outcome.error_message or ""
        current, window = usage_input_and_window(outcome.usage)
        self.previous_input_tokens = current
        self.previous_context_window = window

    def _settle_nonreplayable_outcome(self, outcome: TurnOutcome) -> None:
        if outcome.interrupted:
            return
        if self._settle_policy_boundary(outcome):
            return
        self._adopt_outcome(outcome)
        if self._unresolved_guardian_target_candidate() is not None:
            if outcome.error_message and is_recoverable_transport_error(outcome):
                repaired = self.recover_transport_failure(outcome.error_message)
            else:
                repaired = self._continue_unresolved_guardian_target(
                    outcome.error_message
                    or "repeated non-replayable result on guarded target",
                )
            self.run_automatic_objective(repaired)
            return
        if outcome_has_generic_block(outcome):
            self._write_status(
                "\u5b89\u5168\u63a5\u7e8c\u4e5f\u88ab\u6514\u622a\uff1b\u5df2\u505c\u6b62\u81ea\u52d5\u5617\u8a66\u3002",
                level=RenderLevel.WARNING,
            )
            self.prepare_rollover(
                "post-display-failure clean-thread reset",
                checkpoint=False,
                include_user_context=False,
            )
        elif outcome.error_message:
            self._write_status("\u5b89\u5168\u63a5\u7e8c\u5931\u6557\u3002", level=RenderLevel.ERROR)
            self._write_status(
                "assessment detail: " + trim_text(outcome.error_message, 400),
                verbose_only=True,
                level=RenderLevel.DETAIL,
            )
        else:
            if self._settle_finished_rollover(
                outcome,
                sentinel_has_priority=False,
                include_user_context=False,
            ):
                return
            reason = rollover_reason(
                outcome,
                None,
                getattr(self, "rollover_ratio", DEFAULT_ROLLOVER_RATIO),
            )
            if reason:
                self.run_automatic_rollovers(reason)

    def _run_automatic_objective_loop(
        self,
        *,
        reason: str | None,
        initial_outcome: TurnOutcome | None = None,
    ) -> None:
        progress_turns = 0
        rollover_attempts = 0
        current_reason = reason
        outcome = initial_outcome
        while True:
            if outcome is None:
                if current_reason is None:
                    return
                progress_turns = 0
                try:
                    if self._unresolved_guardian_target_candidate() is not None:
                        outcome = self._continue_unresolved_guardian_target(
                            current_reason,
                        )
                    else:
                        outcome = self.continue_after_rollover(current_reason)
                except Exception as exc:
                    try:
                        reconciliation = self._reconcile_rollover_journal_with_retry()
                    except Exception as recovery_exc:
                        raise RuntimeError(
                            "automatic rollover and durable local recovery failed: "
                            + trim_text(str(recovery_exc), 500)
                        ) from recovery_exc
                    if reconciliation == "none":
                        rollover_attempts += 1
                        if rollover_attempts < 3:
                            time.sleep(0.05 * rollover_attempts)
                            continue
                        raise RuntimeError(
                            f"automatic rollover failed: {trim_text(str(exc), 500)}"
                        ) from exc
                    outcome = self._continue_reconciled_rollover(reconciliation)
                    if outcome is None:
                        return
                rollover_attempts = 0
                current_reason = None
            if outcome.interrupted:
                return
            if self._settle_policy_boundary(outcome):
                return
            self._adopt_outcome(outcome)
            if outcome_has_generic_block(outcome):
                self._write_status(
                    "\u81ea\u52d5\u7e8c\u505a\u88ab\u6514\u622a\uff1b\u6b63\u5728\u5b89\u5168\u8655\u7406\u672a\u5b8c\u6210\u5de5\u4f5c\u3002",
                    level=RenderLevel.WARNING,
                )
                follow = self.continue_nonreplayable(
                    "generic content-unavailable during automatic continuation",
                )
                outcome = follow
                continue
            if outcome.error_message and is_recoverable_transport_error(outcome):
                previous_suppression = bool(
                    getattr(self, "_suppress_turn_output", False)
                )
                self._suppress_turn_output = True
                try:
                    outcome = self.recover_transport_failure(outcome.error_message)
                finally:
                    self._suppress_turn_output = previous_suppression
                continue
            if outcome.error_code == "contextWindowExceeded":
                outcome = self.continue_nonreplayable(
                    "context window exceeded",
                )
                continue
            if outcome.error_message:
                # A turn failure is neither a user-only blocker nor objective
                # completion. Continue through the guarded non-replayable path
                # instead of exposing an error receipt and reprompting.
                outcome = self.continue_nonreplayable(
                    "non-replayable automatic turn failure: "
                    + trim_text(outcome.error_message, 300),
                )
                continue
            completed_receipt = self._current_completed_guardian_receipt()
            if completed_receipt is not None and (
                outcome.guardian_finish_kind == "completed"
                and assistant_response_contradicts_guardian_completion(
                    outcome.final_response
                )
            ):
                self._pending_reopened_guardian_bundle = (
                    reopen_completed_guardian_session(
                        self.project_root,
                        str(self.thread.id),
                        completed_receipt,
                    )
                )
                outcome.guardian_finished = False
                outcome.guardian_finish_kind = None
                completed_receipt = None
            runtime_active = self._current_guardian_runtime_active()
            if (
                outcome.guardian_finished
                and outcome.guardian_finish_kind == "completed"
                and completed_receipt is not None
                and outcome_allows_terminal_settlement(outcome)
            ):
                self._wait_for_pending_guardian_finishes(outcome)
                self._publish_automatic_terminal_response(outcome)
                return
            # For a registered project, the exact target runtime is the
            # completion authority.  `turn/completed`, summaries, checkpoints,
            # and even a premature finish claim cannot stop this loop while the
            # runtime still exists.
            if self._unguarded_objective_completion_proven(outcome):
                self._wait_for_pending_guardian_finishes(outcome)
                self._publish_automatic_terminal_response(outcome)
                return
            if (
                runtime_active is None
                and assistant_requires_user_input(outcome.final_response)
            ):
                self._wait_for_pending_guardian_finishes(outcome)
                self._publish_automatic_terminal_response(outcome)
                return

            next_reason = rollover_reason(
                outcome,
                None,
                getattr(self, "rollover_ratio", DEFAULT_ROLLOVER_RATIO),
            )
            if next_reason:
                current_reason = next_reason
                outcome = None
                continue

            if progress_turns >= MAX_AUTOMATIC_PROGRESS_TURNS:
                current_reason = "automatic objective progress turn budget"
                outcome = None
                continue

            progress_turns += 1
            try:
                self._finish_guardian_sessions_after_dispatch(outcome)
                outcome = self.continue_active_objective(
                    outcome,
                    sequence=progress_turns,
                )
            except Exception as exc:
                raise RuntimeError(
                    "automatic objective continuation failed: "
                    + trim_text(str(exc), 500)
                ) from exc

    def run_automatic_rollovers(self, reason: str) -> None:
        previous = bool(getattr(self, "_automatic_objective_active", False))
        self._automatic_objective_active = True
        try:
            self._run_automatic_objective_loop(reason=reason)
        finally:
            self._automatic_objective_active = previous

    def run_automatic_objective(self, outcome: TurnOutcome) -> None:
        previous = bool(getattr(self, "_automatic_objective_active", False))
        self._automatic_objective_active = True
        try:
            self._run_automatic_objective_loop(
                reason=None,
                initial_outcome=outcome,
            )
        finally:
            self._automatic_objective_active = previous

    def interactive(self, *, resume_on_start: bool = False) -> int:
        reconciliation = self._reconcile_rollover_journal_with_retry()
        if reconciliation == "complete":
            outcome = getattr(self, "_reconciled_terminal_outcome", None)
            if not isinstance(outcome, TurnOutcome):
                raise RuntimeError("rollover completion lost its terminal response")
            self._publish_automatic_terminal_response(outcome)
            return 0
        if reconciliation == "awaiting_user":
            outcome = getattr(self, "_reconciled_user_blocker_outcome", None)
            if not isinstance(outcome, TurnOutcome):
                raise RuntimeError("rollover user blocker lost its exact question")
            self._publish_automatic_terminal_response(outcome)
        if reconciliation == "none":
            self.start_fresh_thread()
        renderer = self._get_renderer()
        if not getattr(self, "_banner_shown", False):
            banner = getattr(renderer, "banner", None)
            server_version = getattr(self, "server_version", None)
            project_root = getattr(self, "project_root", None)
            if callable(banner) and server_version and project_root:
                banner(
                    server_version,
                    str(project_root),
                    model=getattr(self, "model", None),
                    effort=getattr(self, "reasoning_effort", None),
                )
            self._banner_shown = True
        try:
            self._refresh_prompt_catalog()
        except BaseException:
            self._prompt_commands = SLASH_COMMANDS
            self._prompt_files = ()
        if reconciliation == "active":
            startup_prompt = (
                self.pending_handoff + "\n\n" + RECOVERED_ROLLOVER_DISPATCH_PROMPT
                if self.pending_handoff
                else RECOVERED_ROLLOVER_PROMPT
            )
        elif reconciliation == "awaiting_user":
            startup_prompt = None
        else:
            startup_prompt = STARTUP_RESUME_PROMPT if resume_on_start else None
        while True:
            controller_input = False
            if startup_prompt is not None:
                prompt = startup_prompt
                startup_prompt = None
                controller_input = True
            else:
                try:
                    default = getattr(self, "_prefill_prompt", "")
                    self._prefill_prompt = ""
                    with self._output_lock:
                        entered = self._get_renderer().prompt(
                            self._prompt_footer,
                            default=default,
                            commands=getattr(self, "_prompt_commands", SLASH_COMMANDS),
                            files=getattr(self, "_prompt_files", ()),
                            right_hint=self._prompt_right_hint,
                            tasks=self._prompt_tasks,
                        )
                        self._sync_render_state()
                    if entered == EXIT_TOKEN:
                        return 0
                    if entered in {
                        REWIND_TOKEN,
                        TRANSCRIPT_TOKEN,
                        MODEL_TOKEN,
                        FAST_TOKEN,
                        PERMISSIONS_TOKEN,
                        EFFORT_TOKEN,
                        EDITOR_TOKEN,
                        PASTE_IMAGE_TOKEN,
                        TASKS_TOKEN,
                    }:
                        action_draft = getattr(
                            self._get_renderer(),
                            "take_action_draft",
                            self._get_renderer().take_rewind_draft,
                        )
                        draft = action_draft()
                        self._prefill_prompt = draft
                        try:
                            if entered == TRANSCRIPT_TOKEN:
                                self._get_renderer().show_transcript(
                                    self._get_renderer().transcript_text()
                                )
                            elif entered == MODEL_TOKEN:
                                self._handle_model_command([])
                            elif entered == EFFORT_TOKEN:
                                self._handle_effort_command()
                            elif entered == FAST_TOKEN:
                                self._handle_fast_command([])
                            elif entered == PERMISSIONS_TOKEN:
                                self._cycle_permissions()
                            elif entered == EDITOR_TOKEN:
                                self._prefill_prompt = self._edit_prompt_in_editor(draft)
                            elif entered == PASTE_IMAGE_TOKEN:
                                self._paste_clipboard_image()
                            elif entered == TASKS_TOKEN:
                                self._toggle_todos()
                            elif entered == REWIND_TOKEN:
                                prefill = self.rewind_previous_exchange()
                                if prefill is not None:
                                    self._prefill_prompt = prefill
                        except Exception as exc:
                            self._write_status(
                                trim_text(str(exc), 500),
                                level=RenderLevel.ERROR,
                            )
                        continue
                    prompt = entered.strip()
                except EOFError:
                    return 0
                except KeyboardInterrupt:
                    with self._output_lock:
                        self._get_renderer().ensure_newline()
                        self._sync_render_state()
                    continue
            if not prompt:
                continue
            if prompt.startswith("!"):
                try:
                    self._run_shell_mode(prompt[1:])
                except Exception as exc:
                    self._write_status(trim_text(str(exc), 500), level=RenderLevel.ERROR)
                continue
            submitted_prompt = prompt
            try:
                slash = self.handle_slash_command(prompt)
            except Exception as exc:
                self._write_status(
                    trim_text(str(exc), 500),
                    level=RenderLevel.ERROR,
                )
                continue
            if slash.handled:
                if slash.exit_requested:
                    return 0
                if slash.prefill is not None:
                    self._prefill_prompt = slash.prefill
                if slash.turn_prompt is None:
                    if slash.turn_handle is None:
                        continue
                if slash.turn_prompt is not None:
                    prompt = slash.turn_prompt

            try:
                self._settle_manual_compaction()
            except Exception as exc:
                self._write_status(trim_text(str(exc), 300), level=RenderLevel.ERROR)
                continue

            if not controller_input:
                self.latest_user = submitted_prompt
            turn_prompt = prompt
            if getattr(self, "_pending_shell_context", None):
                shell_context = "\n\n".join(self._pending_shell_context)
                self._pending_shell_context = []
                turn_prompt = (
                    "[LOCAL SHELL MODE CONTEXT]\n"
                    + trim_text(shell_context, MAX_COMMAND_OUTPUT_BUFFER_CHARS)
                    + "\n\n[USER MESSAGE]\n"
                    + turn_prompt
                )
            dispatched_handoff = self.pending_handoff
            handoff_dispatched = bool(dispatched_handoff)
            automatic_origin = controller_input or handoff_dispatched
            guardian_buffered = False
            if not automatic_origin:
                turn_prompt, guardian_buffered = (
                    self._prepare_normal_guardian_user_turn(turn_prompt)
                )
            silent_origin = automatic_origin or guardian_buffered
            if handoff_dispatched:
                turn_prompt = (
                    dispatched_handoff
                    + "\n\n[NEW USER MESSAGE — authoritative current objective]\n"
                    + prompt
                )
            transport_recovery = False
            try:
                if silent_origin:
                    outcome = self._run_controller_turn_silently(
                        turn_prompt,
                        handle=slash.turn_handle
                        if slash.handled
                        else None,
                    )
                elif slash.handled and slash.turn_handle is not None:
                    outcome = self.run_turn(
                        turn_prompt,
                        stream_text=True,
                        handle=slash.turn_handle,
                    )
                else:
                    outcome = self.run_turn(turn_prompt, stream_text=True)
            except Exception as exc:
                try:
                    previous_suppression = bool(
                        getattr(self, "_suppress_turn_output", False)
                    )
                    if silent_origin:
                        self._suppress_turn_output = True
                    try:
                        outcome = self.recover_transport_failure(str(exc))
                    finally:
                        self._suppress_turn_output = previous_suppression
                    transport_recovery = True
                except Exception as recovery_exc:
                    raise RuntimeError(
                        "turn transport recovery failed: "
                        + trim_text(str(recovery_exc), 400)
                    ) from recovery_exc
            if not controller_input and not transport_recovery:
                self._remember_logical_prompt(
                    self.thread.id,
                    outcome.turn_id,
                    submitted_prompt,
                )
            if handoff_dispatched:
                self._complete_handoff_dispatch(
                    outcome,
                    dispatched_handoff=dispatched_handoff,
                )
            if outcome.interrupted:
                continue
            previous = self.previous_input_tokens
            if self._settle_policy_boundary(outcome):
                continue
            self._adopt_outcome(outcome)
            if not handoff_dispatched:
                self._finish_guardian_sessions_after_dispatch(outcome)

            if transport_recovery:
                if outcome_has_generic_block(outcome) or outcome.error_message:
                    self.run_automatic_objective(outcome)
                    continue
                self.run_automatic_objective(outcome)
                continue

            if outcome_has_generic_block(outcome):
                if should_retry_generic(outcome):
                    try:
                        retry = self.recover_generic_block()
                    except Exception as exc:
                        raise RuntimeError(
                            f"fresh-thread recovery failed: {trim_text(str(exc), 400)}"
                        ) from exc
                    if retry.interrupted:
                        continue
                    if self._settle_policy_boundary(retry):
                        continue
                    self._adopt_outcome(retry)
                    if outcome_has_generic_block(retry):
                        self._write_status(
                            "\u518d\u6b21\u5617\u8a66\u4ecd\u88ab\u6514\u622a\uff1b\u6b63\u5728\u5b89\u5168\u8655\u7406\u672a\u5b8c\u6210\u5de5\u4f5c\u3002",
                            level=RenderLevel.WARNING,
                        )
                        follow = self.continue_nonreplayable(
                            "repeated generic content-unavailable",
                        )
                        self._settle_nonreplayable_outcome(follow)
                    elif retry.error_message:
                        self._write_status(
                            f"\u518d\u6b21\u5617\u8a66\u5931\u6557\uff1a{trim_text(retry.error_message, 400)}",
                            level=RenderLevel.ERROR,
                        )
                    elif self._settle_finished_rollover(retry):
                        pass
                    else:
                        retry_reason = rollover_reason(
                            retry,
                            None,
                            self.rollover_ratio,
                        )
                        if retry_reason:
                            self.run_automatic_rollovers(retry_reason)
                else:
                    self._write_status(
                        "\u5de5\u5177\u5df2\u57f7\u884c\uff1b\u70ba\u907f\u514d\u91cd\u8907\u4fee\u6539\uff0c\u4e0d\u76f2\u76ee\u91cd\u9001\uff0c"
                        "\u81ea\u52d5\u63a5\u7e8c\u5176\u9918\u5de5\u4f5c\u3002",
                        level=RenderLevel.WARNING,
                    )
                    follow = self.continue_nonreplayable(
                        "generic content-unavailable after a non-replayable turn",
                    )
                    self._settle_nonreplayable_outcome(follow)
                continue

            if outcome.error_message:
                if is_recoverable_transport_error(outcome):
                    recovered = self.recover_transport_failure(outcome.error_message)
                    self.run_automatic_objective(recovered)
                    continue
                if outcome.error_code == "contextWindowExceeded":
                    recovered = self.continue_nonreplayable(
                        "context window exceeded",
                    )
                    self.run_automatic_objective(recovered)
                    continue
                code = f" ({outcome.error_code})" if outcome.error_code else ""
                self._write_status(
                    f"\u56de\u5408\u5931\u6557{code}\uff1a{trim_text(outcome.error_message, 400)}",
                    level=RenderLevel.ERROR,
                )

            if not automatic_origin and self._settle_finished_rollover(outcome):
                if guardian_buffered:
                    self._publish_automatic_terminal_response(outcome)
                continue

            reason = rollover_reason(outcome, previous, self.rollover_ratio)
            if reason:
                self.run_automatic_rollovers(reason)
            elif (
                automatic_origin
                or outcome.guardian_finished
                or self._current_guardian_runtime_active() is True
            ):
                self.run_automatic_objective(outcome)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Continuous fresh-thread Codex CLI")
    parser.add_argument("--project", default=os.getcwd(), help="project working directory")
    parser.add_argument("--codex-bin", help="explicit path to codex.exe")
    parser.add_argument("--model", help="optional model override; config default is preferred")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="explicitly resume bounded project state before reading user input",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="show detailed tool start/completion events",
    )
    parser.add_argument(
        "--plain",
        action="store_true",
        help="use flat text without decorative borders, color, or animation",
    )
    parser.add_argument(
        "--rollover-ratio",
        type=float,
        default=DEFAULT_ROLLOVER_RATIO,
        help="fresh-thread threshold as a 0-1 context ratio (default: 0.55)",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    project_root = Path(args.project).expanduser().resolve()
    if not project_root.is_dir():
        raise RuntimeError(f"\u5c08\u6848\u76ee\u9304\u4e0d\u5b58\u5728\uff1a{project_root}")
    if not 0.40 <= args.rollover_ratio <= 0.85:
        raise RuntimeError("--rollover-ratio \u5fc5\u9808\u4ecb\u65bc 0.40 \u8207 0.85")
    codex_bin = find_codex_binary(args.codex_bin)
    version = codex_version(codex_bin)
    version_match = re.search(r"(\d+\.\d+\.\d+)", version)
    short_version = version_match.group(1) if version_match else version
    renderer = TerminalRenderer(force_plain=args.plain)
    client = ContinuousCodex(
        project_root,
        codex_bin=codex_bin,
        rollover_ratio=args.rollover_ratio,
        model=args.model,
        verbose=args.verbose,
        renderer=renderer,
    )
    client.server_version = client.server_version or short_version
    try:
        return client.interactive(resume_on_start=args.resume)
    finally:
        client.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
