from __future__ import annotations

import json
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence


REWIND_TOKEN = "\x00codex-continuous-rewind\x00"
TRANSCRIPT_TOKEN = "\x00codex-continuous-transcript\x00"
MODEL_TOKEN = "\x00codex-continuous-model\x00"
FAST_TOKEN = "\x00codex-continuous-fast\x00"
PERMISSIONS_TOKEN = "\x00codex-continuous-permissions\x00"
EFFORT_TOKEN = "\x00codex-continuous-effort\x00"
EDITOR_TOKEN = "\x00codex-continuous-editor\x00"
PASTE_IMAGE_TOKEN = "\x00codex-continuous-paste-image\x00"
TASKS_TOKEN = "\x00codex-continuous-tasks\x00"
EXIT_TOKEN = "\x00codex-continuous-exit\x00"
DEFAULT_KEYBINDINGS_PATH = Path.home() / ".codex" / "keybindings.json"


@dataclass(frozen=True, slots=True)
class MenuItem:
    value: str
    description: str = ""
    kind: str = "command"


@dataclass(frozen=True, slots=True)
class MenuSelection:
    index: int
    effort: str | None = None
    persist: bool = True


class ClaudePromptUI:
    """Prompt-toolkit state machine matching Claude Code's classic input surface."""

    _HELP_LINES = (
        "  ! for shell mode        double tap esc to clear input      ctrl + shift + _ to undo",
        "  / for commands          shift + tab to auto-accept edits   alt + v to paste images",
        "  @ for file paths        ctrl + o to open transcript        alt + p to switch model",
        "  /btw for side question  ctrl + t to toggle tasks           ctrl + s to stash prompt",
        "                          shift + enter for newline          ctrl + g to edit in $EDITOR",
        "                                                             /keybindings to customize",
    )
    _PLACEHOLDERS = (
        "Explain this codebase",
        "Summarize recent commits",
        "Implement {feature}",
        "Find and fix a bug in @filename",
        "Write tests for @filename",
        "Improve documentation in @filename",
        "Run /review on my current changes",
        "Use /skills to list available skills",
    )

    def __init__(self) -> None:
        from prompt_toolkit.history import InMemoryHistory

        self.history = InMemoryHistory()
        self.stash = ""
        self.action_draft = ""
        self.last_cleared_draft = ""
        self._last_ctrl_c = 0.0
        self._last_ctrl_d = 0.0
        self._last_ctrl_l = 0.0
        self.placeholder = secrets.choice(self._PLACEHOLDERS)

    def reset_history(self) -> None:
        from prompt_toolkit.history import InMemoryHistory

        self.history = InMemoryHistory()
        self.stash = ""
        self.action_draft = ""
        self.last_cleared_draft = ""

    @staticmethod
    def _parse_key_sequence(value: str) -> tuple[str, ...] | None:
        result: list[str] = []
        aliases = {"esc": "escape", "return": "enter", "control": "ctrl", "meta": "alt"}
        for stroke in value.strip().casefold().split():
            parts = [aliases.get(part, part) for part in stroke.split("+")]
            if "alt" in parts:
                keys = [part for part in parts if part != "alt"]
                if len(keys) != 1:
                    return None
                result.extend(("escape", keys[0]))
            elif "ctrl" in parts:
                keys = [part for part in parts if part != "ctrl" and part != "shift"]
                if len(keys) != 1 or len(keys[0]) != 1:
                    return None
                result.append("c-" + keys[0])
            elif parts == ["shift", "tab"]:
                result.append("s-tab")
            elif parts == ["shift", "enter"]:
                result.extend(("escape", "enter"))
            elif len(parts) == 1:
                result.append(parts[0])
            else:
                return None
        return tuple(result) if result else None

    @classmethod
    def load_keybindings(cls, path: Path) -> list[tuple[tuple[str, ...], str | None]]:
        if not path.is_file():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        blocks = data.get("bindings", []) if isinstance(data, dict) else []
        result: list[tuple[tuple[str, ...], str | None]] = []
        for block in blocks:
            if not isinstance(block, dict) or block.get("context") not in {"Chat", "Global"}:
                continue
            values = block.get("bindings", {})
            if not isinstance(values, dict):
                continue
            for key, action in values.items():
                sequence = cls._parse_key_sequence(str(key))
                if sequence and (action is None or isinstance(action, str)):
                    result.append((sequence, action))
        return result

    @staticmethod
    def _append_history(history: Any, value: str) -> None:
        clean = value.rstrip()
        if not clean:
            return
        existing = list(history.get_strings())
        if not existing or existing[-1] != clean:
            history.append_string(clean)

    @staticmethod
    def _safe_bind(bindings: Any, *keys: str):
        def decorator(function: Callable[[Any], None]) -> Callable[[Any], None]:
            try:
                bindings.add(*keys)(function)
            except ValueError:
                pass
            return function

        return decorator

    def run(
        self,
        *,
        footer: Callable[[], str],
        right_hint: Callable[[], str] | None = None,
        default: str = "",
        commands: Sequence[tuple[str, str]] = (),
        files: Sequence[str] = (),
        tasks: Callable[[], Sequence[str]] | None = None,
        placeholder: str | None = None,
        keybindings_path: Path | None = DEFAULT_KEYBINDINGS_PATH,
        input: Any | None = None,
        output: Any | None = None,
    ) -> str:
        from prompt_toolkit.application import Application
        from prompt_toolkit.buffer import Buffer
        from prompt_toolkit.document import Document
        from prompt_toolkit.filters import Condition
        from prompt_toolkit.formatted_text import FormattedText
        from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings
        from prompt_toolkit.key_binding.defaults import load_key_bindings as load_default_key_bindings
        from prompt_toolkit.layout import (
            BufferControl,
            ConditionalContainer,
            FormattedTextControl,
            HSplit,
            Layout,
            Window,
            WindowAlign,
        )
        from prompt_toolkit.layout.processors import BeforeInput, ConditionalProcessor
        from prompt_toolkit.styles import Style

        buffer = Buffer(history=self.history, multiline=True)
        buffer.set_document(Document(default, cursor_position=len(default)), bypass_readonly=True)
        bindings = KeyBindings()
        placeholder_text = placeholder or self.placeholder
        state: dict[str, Any] = {
            "selected": 0,
            "help": False,
            "notice": "",
            "notice_until": 0.0,
            "search_query": None,
            "search_index": 0,
            "dismissed_menu_text": None,
        }

        command_items = tuple(MenuItem(name, description, "command") for name, description in commands)
        file_items = tuple(MenuItem("@" + path, path, "file") for path in files)

        def current_items() -> list[MenuItem]:
            text = buffer.document.text_before_cursor
            if state["dismissed_menu_text"] == text:
                return []
            items: list[MenuItem]
            if "\n" not in text and text.startswith("/") and not any(
                character.isspace() for character in text
            ):
                folded = text.casefold()
                items = [item for item in command_items if item.value.casefold().startswith(folded)]
            else:
                match = re.search(r"(?:^|\s)@([^\s]*)$", text)
                if match:
                    query = match.group(1).replace("\\", "/").casefold()
                    items = [
                        item
                        for item in file_items
                        if query in item.description.replace("\\", "/").casefold()
                    ]
                else:
                    items = []
            if items:
                state["selected"] %= len(items)
            else:
                state["selected"] = 0
            return items

        def menu_fragments() -> FormattedText:
            fragments: list[tuple[str, str]] = []
            items = current_items()
            count = min(8, len(items))
            first = max(0, min(state["selected"] - count // 2, len(items) - count))
            for index, item in enumerate(items[first:first + count], start=first):
                value = item.value[:35]
                selected = index == state["selected"]
                fragments.append(("class:menu", "  "))
                fragments.append(("class:menu.pointer", "❯ " if selected else "  "))
                fragments.append(("class:menu", value))
                if selected and item.description:
                    fragments.append(("class:menu.description", "  " + item.description))
                fragments.append(("class:menu", "\n"))
            return FormattedText(fragments)

        def footer_fragments() -> FormattedText:
            notice = state["notice"] if time.monotonic() < state["notice_until"] else ""
            value = notice or footer()
            return FormattedText([("class:footer", "  " + value)])

        def hint_fragments() -> FormattedText:
            value = right_hint() if right_hint is not None else ""
            return FormattedText([("class:hint", value)])

        def task_fragments() -> FormattedText:
            values = list(tasks() if tasks is not None else ())
            return FormattedText(
                [
                    ("class:tasks", "  " + value + ("\n" if index < len(values) - 1 else ""))
                    for index, value in enumerate(values)
                ]
            )

        def apply_selected(*, submit_command: bool) -> bool:
            items = current_items()
            if not items:
                return False
            item = items[state["selected"]]
            if item.kind == "command":
                if submit_command:
                    self.last_cleared_draft = ""
                    self._append_history(self.history, item.value)
                    app.exit(result=item.value)
                else:
                    buffer.set_document(
                        Document(item.value + " ", cursor_position=len(item.value) + 1),
                        bypass_readonly=True,
                    )
                return True
            before = buffer.document.text_before_cursor
            match = re.search(r"(?:^|\s)@[^\s]*$", before)
            if match is None:
                return False
            replacement = item.value + " "
            start = match.start()
            if before[start:start + 1].isspace():
                start += 1
            text = buffer.text[:start] + replacement + buffer.document.text_after_cursor
            cursor = start + len(replacement)
            buffer.set_document(Document(text, cursor_position=cursor), bypass_readonly=True)
            return True

        def exit_with(token: str) -> None:
            self.action_draft = buffer.text
            app.exit(result=token)

        @bindings.add("enter")
        def accept(event: Any) -> None:
            if buffer.document.text_before_cursor.endswith("\\"):
                buffer.delete_before_cursor(1)
                buffer.insert_text("\n")
                return
            if current_items() and buffer.text.startswith("/"):
                apply_selected(submit_command=True)
                return
            if buffer.text.strip():
                self.last_cleared_draft = ""
                self._append_history(self.history, buffer.text)
                event.app.exit(result=buffer.text)

        @bindings.add("c-j")
        def newline(_event: Any) -> None:
            buffer.insert_text("\n")

        @self._safe_bind(bindings, "s-enter")
        def shift_enter(_event: Any) -> None:
            buffer.insert_text("\n")

        @bindings.add("escape", "enter")
        def meta_enter(_event: Any) -> None:
            buffer.insert_text("\n")

        @bindings.add("up")
        def up(event: Any) -> None:
            items = current_items()
            if items:
                state["selected"] = (state["selected"] - 1) % len(items)
                event.app.invalidate()
                return
            if not buffer.text and self.last_cleared_draft:
                restored = self.last_cleared_draft
                self.last_cleared_draft = ""
                buffer.set_document(Document(restored, len(restored)), bypass_readonly=True)
                return
            buffer.auto_up()

        @bindings.add("down")
        def down(event: Any) -> None:
            items = current_items()
            if items:
                state["selected"] = (state["selected"] + 1) % len(items)
                event.app.invalidate()
                return
            buffer.auto_down()

        @bindings.add("tab")
        def tab(_event: Any) -> None:
            if not apply_selected(submit_command=False):
                buffer.insert_text("    ")

        @bindings.add("escape", "escape")
        def double_escape(event: Any) -> None:
            if buffer.text:
                draft = buffer.text
                self._append_history(self.history, draft)
                self.last_cleared_draft = draft
                buffer.reset()
                state["notice"] = "Input cleared · ↑ to restore"
                state["notice_until"] = time.monotonic() + 1.5
                event.app.invalidate()
            else:
                exit_with(REWIND_TOKEN)

        @bindings.add("escape")
        def escape(event: Any) -> None:
            if state["help"]:
                state["help"] = False
            elif current_items():
                state["dismissed_menu_text"] = buffer.document.text_before_cursor
            event.app.invalidate()

        @bindings.add("c-c")
        def ctrl_c(event: Any) -> None:
            now = time.monotonic()
            if buffer.text:
                buffer.reset()
                self._last_ctrl_c = now
                return
            if now - self._last_ctrl_c <= 0.8:
                event.app.exit(result=EXIT_TOKEN)
            else:
                self._last_ctrl_c = now
                state["notice"] = "Press Ctrl-C again to exit"
                state["notice_until"] = now + 0.8
                event.app.invalidate()

        @bindings.add("c-d")
        def ctrl_d(event: Any) -> None:
            if buffer.text:
                if buffer.cursor_position < len(buffer.text):
                    buffer.delete(1)
                return
            now = time.monotonic()
            if now - self._last_ctrl_d <= 0.8:
                event.app.exit(result=EXIT_TOKEN)
            else:
                self._last_ctrl_d = now
                state["notice"] = "Press Ctrl-D again to exit"
                state["notice_until"] = now + 0.8
                event.app.invalidate()

        @bindings.add("c-l")
        def ctrl_l(event: Any) -> None:
            now = time.monotonic()
            event.app.renderer.clear()
            if now - self._last_ctrl_l <= 0.8:
                state["notice"] = "Screen cleared"
                state["notice_until"] = now + 0.8
            self._last_ctrl_l = now
            event.app.invalidate()

        @bindings.add("c-o")
        def transcript(_event: Any) -> None:
            exit_with(TRANSCRIPT_TOKEN)

        @bindings.add("c-s")
        def stash(event: Any) -> None:
            if buffer.text:
                self.stash = buffer.text
                buffer.reset()
                state["notice"] = "Prompt stashed · Ctrl-S to restore"
                state["notice_until"] = time.monotonic() + 1.5
            elif self.stash:
                restored = self.stash
                self.stash = ""
                buffer.set_document(Document(restored, len(restored)), bypass_readonly=True)
            event.app.invalidate()

        @bindings.add("c-r")
        def history_search(event: Any) -> None:
            query = state["search_query"]
            if query is None:
                query = buffer.text
                state["search_query"] = query
                state["search_index"] = 0
            matches = [
                value
                for value in reversed(list(self.history.get_strings()))
                if str(query).casefold() in value.casefold()
            ]
            if matches:
                index = state["search_index"] % len(matches)
                value = matches[index]
                state["search_index"] = index + 1
                buffer.set_document(Document(value, len(value)), bypass_readonly=True)
                state["notice"] = f"reverse-i-search: {query}"
            else:
                state["notice"] = f"failing reverse-i-search: {query}"
            state["notice_until"] = time.monotonic() + 2.0
            event.app.invalidate()

        @bindings.add("s-tab")
        def cycle_permissions(_event: Any) -> None:
            exit_with(PERMISSIONS_TOKEN)

        @bindings.add("escape", "m")
        def alt_m(_event: Any) -> None:
            exit_with(PERMISSIONS_TOKEN)

        @bindings.add("escape", "p")
        def alt_p(_event: Any) -> None:
            exit_with(MODEL_TOKEN)

        @bindings.add("escape", "o")
        def alt_o(_event: Any) -> None:
            exit_with(FAST_TOKEN)

        @bindings.add("escape", "t")
        def alt_t(_event: Any) -> None:
            exit_with(EFFORT_TOKEN)

        @bindings.add("escape", "v")
        def alt_v(_event: Any) -> None:
            exit_with(PASTE_IMAGE_TOKEN)

        @bindings.add("c-t")
        def toggle_tasks(_event: Any) -> None:
            exit_with(TASKS_TOKEN)

        @bindings.add("c-g")
        def editor(_event: Any) -> None:
            exit_with(EDITOR_TOKEN)

        @bindings.add("c-x", "c-e")
        def editor_chord(_event: Any) -> None:
            exit_with(EDITOR_TOKEN)

        @self._safe_bind(bindings, "?")
        def help_toggle(event: Any) -> None:
            if not buffer.text:
                state["help"] = not state["help"]
                event.app.invalidate()
            else:
                buffer.insert_text("?")

        custom_bindings = KeyBindings()
        custom_actions: dict[str, Callable[[Any], None]] = {
            "app:exit": lambda event: event.app.exit(result=EXIT_TOKEN),
            "app:toggleTodos": toggle_tasks,
            "app:toggleTranscript": transcript,
            "history:search": history_search,
            "chat:cycleMode": cycle_permissions,
            "chat:modelPicker": alt_p,
            "chat:fastMode": alt_o,
            "chat:thinkingToggle": alt_t,
            "chat:externalEditor": editor,
            "chat:stash": stash,
            "chat:imagePaste": alt_v,
            "chat:newline": newline,
            "chat:submit": accept,
        }
        reserved = {("c-c",), ("c-d",)}
        if keybindings_path is not None:
            try:
                configured_bindings = self.load_keybindings(keybindings_path)
            except (OSError, json.JSONDecodeError):
                configured_bindings = []
            for sequence, action in configured_bindings:
                if sequence in reserved:
                    continue
                handler = custom_actions.get(action) if action is not None else None
                if action is not None and handler is None:
                    continue

                def configured(event: Any, callback: Callable[[Any], None] | None = handler) -> None:
                    if callback is not None:
                        callback(event)

                try:
                    custom_bindings.add(*sequence, eager=True)(configured)
                except ValueError:
                    continue

        menu = ConditionalContainer(
            Window(
                FormattedTextControl(menu_fragments),
                height=lambda: max(1, min(8, len(current_items()))),
                dont_extend_height=True,
            ),
            filter=Condition(lambda: bool(current_items())),
        )
        hint = ConditionalContainer(
            Window(
                FormattedTextControl(hint_fragments),
                height=1,
                align=WindowAlign.RIGHT,
                dont_extend_height=True,
            ),
            filter=Condition(lambda: bool(right_hint and right_hint())),
        )
        task_window = ConditionalContainer(
            Window(
                FormattedTextControl(task_fragments),
                height=lambda: max(1, len(list(tasks() if tasks is not None else ()))),
                dont_extend_height=True,
            ),
            filter=Condition(lambda: bool(tasks and list(tasks()))),
        )
        input_control = BufferControl(
            buffer=buffer,
            focusable=True,
            input_processors=[
                ConditionalProcessor(
                    BeforeInput(FormattedText([("class:placeholder", placeholder_text)])),
                    filter=Condition(lambda: not buffer.text),
                )
            ],
        )
        input_window = Window(
            input_control,
            height=lambda: max(1, min(8, buffer.document.line_count)),
            wrap_lines=True,
            get_line_prefix=lambda line_number, _wrap_count: FormattedText(
                [("class:prompt", ">\u00a0" if line_number == 0 else "  ")]
            ),
        )
        help_window = ConditionalContainer(
            Window(
                FormattedTextControl(
                    lambda: FormattedText(
                        [("class:help", line + ("\n" if index < len(self._HELP_LINES) - 1 else ""))
                         for index, line in enumerate(self._HELP_LINES)]
                    )
                ),
                height=len(self._HELP_LINES),
                dont_extend_height=True,
            ),
            filter=Condition(lambda: bool(state["help"])),
        )
        footer_window = ConditionalContainer(
            Window(
                FormattedTextControl(footer_fragments),
                height=1,
                dont_extend_height=True,
            ),
            filter=Condition(lambda: not state["help"]),
        )
        separator = lambda: Window(char="─", height=1, style="class:separator")
        root = HSplit(
            [
                menu,
                task_window,
                hint,
                separator(),
                input_window,
                separator(),
                help_window,
                footer_window,
            ]
        )
        style = Style.from_dict(
            {
                "prompt": "",
                "placeholder": "dim",
                "separator": "#888888",
                "footer": "#999999",
                "hint": "#999999",
                "help": "#b0b0b0",
                "tasks": "#999999",
                "menu": "",
                "menu.pointer": "#d77757",
                "menu.description": "#777777",
            }
        )
        app_kwargs: dict[str, Any] = {}
        if input is not None:
            app_kwargs["input"] = input
        if output is not None:
            app_kwargs["output"] = output
        app = Application(
            layout=Layout(root, focused_element=input_control),
            key_bindings=merge_key_bindings(
                [load_default_key_bindings(), bindings, custom_bindings]
            ),
            style=style,
            full_screen=False,
            erase_when_done=True,
            mouse_support=False,
            **app_kwargs,
        )
        app.timeoutlen = 0.25
        app.ttimeoutlen = 0.1
        result = app.run()
        return str(result or "")

    def select(
        self,
        *,
        title: str,
        subtitle: str,
        options: Sequence[tuple[str, str]],
        initial_index: int = 0,
        efforts: Sequence[Sequence[str]] | None = None,
        initial_effort: str | None = None,
        allow_session_only: bool = False,
        input: Any | None = None,
        output: Any | None = None,
    ) -> MenuSelection | None:
        if not options:
            return None
        from prompt_toolkit.application import Application
        from prompt_toolkit.formatted_text import FormattedText
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout import FormattedTextControl, HSplit, Layout, Window
        from prompt_toolkit.styles import Style

        selected_index = max(0, min(initial_index, len(options) - 1))
        state = {
            "focus": selected_index,
            "current": selected_index,
            "effort": initial_effort,
        }
        bindings = KeyBindings()

        def supported_efforts() -> list[str]:
            if efforts is None or state["focus"] >= len(efforts):
                return []
            return list(efforts[state["focus"]])

        def normalize_effort() -> None:
            available = supported_efforts()
            if not available:
                state["effort"] = None
            elif state["effort"] not in available:
                state["effort"] = available[0]

        def options_text() -> FormattedText:
            normalize_effort()
            count = min(8, len(options))
            first = max(0, min(state["focus"] - count // 2, len(options) - count))
            fragments: list[tuple[str, str]] = []
            for offset, (label, description) in enumerate(options[first:first + count], start=first):
                focused = offset == state["focus"]
                current = offset == state["current"]
                marker = "❯" if focused else " "
                fragments.append(("class:option", "   "))
                fragments.append(("class:pointer", marker + " "))
                fragments.append(("class:option", f"{offset + 1}. {label}"))
                if current:
                    fragments.append(("class:check", "  ✔"))
                if description and focused:
                    fragments.append(("class:description", "  " + description))
                fragments.append(("class:option", "\n"))
            return FormattedText(fragments)

        def effort_text() -> FormattedText:
            normalize_effort()
            value = state["effort"]
            if not value:
                return FormattedText([])
            return FormattedText([("class:effort", f"   ○ {value.capitalize()} effort  ←/→ to adjust")])

        def finish(event: Any, persist: bool) -> None:
            normalize_effort()
            event.app.exit(
                result=MenuSelection(
                    index=state["focus"],
                    effort=state["effort"],
                    persist=persist,
                )
            )

        @bindings.add("up")
        def up(event: Any) -> None:
            state["focus"] = (state["focus"] - 1) % len(options)
            normalize_effort()
            event.app.invalidate()

        @bindings.add("down")
        def down(event: Any) -> None:
            state["focus"] = (state["focus"] + 1) % len(options)
            normalize_effort()
            event.app.invalidate()

        @bindings.add("left")
        def left(event: Any) -> None:
            available = supported_efforts()
            if available:
                current = available.index(state["effort"]) if state["effort"] in available else 0
                state["effort"] = available[(current - 1) % len(available)]
                event.app.invalidate()

        @bindings.add("right")
        def right(event: Any) -> None:
            available = supported_efforts()
            if available:
                current = available.index(state["effort"]) if state["effort"] in available else 0
                state["effort"] = available[(current + 1) % len(available)]
                event.app.invalidate()

        @bindings.add("enter")
        def enter(event: Any) -> None:
            finish(event, True)

        @bindings.add("s")
        def session(event: Any) -> None:
            if allow_session_only:
                finish(event, False)

        @bindings.add("escape")
        @bindings.add("c-c")
        def cancel(event: Any) -> None:
            event.app.exit(result=None)

        for number in range(1, min(9, len(options)) + 1):
            def choose_number(event: Any, index: int = number - 1) -> None:
                state["focus"] = index
                finish(event, True)

            bindings.add(str(number))(choose_number)

        footer = (
            "   Enter to set as default · s to use this session only · Esc to cancel"
            if allow_session_only
            else "   Enter to select · Esc to cancel"
        )
        root = HSplit(
            [
                Window(char="▔", height=1, style="class:border"),
                Window(FormattedTextControl(lambda: [("class:title", "   " + title)]), height=1),
                Window(
                    FormattedTextControl(lambda: [("class:subtitle", "   " + subtitle)]),
                    height=1,
                ),
                Window(FormattedTextControl(options_text), dont_extend_height=True),
                Window(FormattedTextControl(effort_text), height=1, dont_extend_height=True),
                Window(FormattedTextControl(lambda: [("class:footer", footer)]), height=1),
            ]
        )
        kwargs: dict[str, Any] = {}
        if input is not None:
            kwargs["input"] = input
        if output is not None:
            kwargs["output"] = output
        app = Application(
            layout=Layout(root),
            key_bindings=bindings,
            style=Style.from_dict(
                {
                    "border": "#666666",
                    "title": "bold",
                    "subtitle": "#999999",
                    "option": "",
                    "pointer": "#d77757",
                    "check": "#6abf69",
                    "description": "#888888",
                    "effort": "#999999",
                    "footer": "#999999",
                }
            ),
            full_screen=False,
            erase_when_done=True,
            **kwargs,
        )
        return app.run()

    @staticmethod
    def fast_mode(
        *,
        enabled: bool,
        model_label: str,
        input: Any | None = None,
        output: Any | None = None,
    ) -> bool | None:
        from prompt_toolkit.application import Application
        from prompt_toolkit.formatted_text import FormattedText
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout import FormattedTextControl, HSplit, Layout, Window
        from prompt_toolkit.styles import Style
        from prompt_toolkit.widgets import Frame

        state = {"enabled": enabled}
        bindings = KeyBindings()

        def status_text() -> FormattedText:
            value = "ON" if state["enabled"] else "OFF"
            return FormattedText(
                [
                    ("class:label", "  Fast mode  "),
                    ("class:on" if state["enabled"] else "class:off", value),
                ]
            )

        def toggle(event: Any) -> None:
            state["enabled"] = not state["enabled"]
            event.app.invalidate()

        bindings.add("tab")(toggle)
        bindings.add("s-tab")(toggle)

        @bindings.add("enter")
        def confirm(event: Any) -> None:
            event.app.exit(result=bool(state["enabled"]))

        @bindings.add("escape")
        @bindings.add("c-c")
        def cancel(event: Any) -> None:
            event.app.exit(result=None)

        description = (
            f"High-speed service tier for {model_label}. "
            "Separate usage limits may apply."
        )
        body = HSplit(
            [
                Window(
                    FormattedTextControl(
                        FormattedText([("class:title", "↯ Fast mode (research preview)")])
                    ),
                    height=1,
                ),
                Window(
                    FormattedTextControl(
                        FormattedText([("class:description", description)])
                    ),
                    height=2,
                    wrap_lines=True,
                ),
                Window(height=1),
                Window(FormattedTextControl(status_text), height=1),
            ]
        )
        root = HSplit(
            [
                Frame(body, style="class:frame"),
                Window(
                    FormattedTextControl(
                        FormattedText(
                            [("class:guide", "  tab to toggle · enter to confirm · esc to cancel")]
                        )
                    ),
                    height=1,
                ),
            ]
        )
        kwargs: dict[str, Any] = {}
        if input is not None:
            kwargs["input"] = input
        if output is not None:
            kwargs["output"] = output
        return Application(
            layout=Layout(root),
            key_bindings=bindings,
            style=Style.from_dict(
                {
                    "frame": "#b58a3d",
                    "title": "bold #d6a44c",
                    "description": "#999999",
                    "label": "",
                    "on": "bold #6abf69",
                    "off": "#999999",
                    "guide": "italic #888888",
                }
            ),
            full_screen=False,
            erase_when_done=True,
            **kwargs,
        ).run()

    @staticmethod
    def transcript(
        text: str,
        *,
        input: Any | None = None,
        output: Any | None = None,
    ) -> None:
        from prompt_toolkit.application import Application
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout import HSplit, Layout, Window
        from prompt_toolkit.layout.controls import FormattedTextControl
        from prompt_toolkit.styles import Style
        from prompt_toolkit.widgets import TextArea

        bindings = KeyBindings()
        viewer = TextArea(text=text, read_only=True, scrollbar=True, wrap_lines=True)

        @bindings.add("q")
        @bindings.add("escape")
        @bindings.add("c-c")
        @bindings.add("c-o")
        def close(event: Any) -> None:
            event.app.exit()

        root = HSplit(
            [
                Window(
                    FormattedTextControl(
                        [("class:title", " Transcript"), ("class:hint", "   Esc or Ctrl-O to return")]
                    ),
                    height=1,
                ),
                viewer,
                Window(char="─", height=1, style="class:border"),
            ]
        )
        kwargs: dict[str, Any] = {}
        if input is not None:
            kwargs["input"] = input
        if output is not None:
            kwargs["output"] = output
        Application(
            layout=Layout(root, focused_element=viewer),
            key_bindings=bindings,
            style=Style.from_dict(
                {"title": "bold", "hint": "#999999", "border": "#666666"}
            ),
            full_screen=True,
            mouse_support=False,
            **kwargs,
        ).run()
