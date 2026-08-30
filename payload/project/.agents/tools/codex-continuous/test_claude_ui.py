from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Callable, Sequence

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from claude_ui import (
    EXIT_TOKEN,
    MODEL_TOKEN,
    PERMISSIONS_TOKEN,
    REWIND_TOKEN,
    TASKS_TOKEN,
    TRANSCRIPT_TOKEN,
    ClaudePromptUI,
    MenuSelection,
)


class ClaudePromptUITests(unittest.TestCase):
    @staticmethod
    def _run_prompt(
        keys: str,
        *,
        ui: ClaudePromptUI | None = None,
        commands: Sequence[tuple[str, str]] = (),
        default: str = "",
        keybindings_path: Path | None = None,
    ) -> tuple[str, ClaudePromptUI]:
        prompt_ui = ui or ClaudePromptUI()
        with create_pipe_input() as pipe_input:
            pipe_input.send_text(keys)
            result = prompt_ui.run(
                footer=lambda: "ready",
                default=default,
                commands=commands,
                keybindings_path=keybindings_path,
                input=pipe_input,
                output=DummyOutput(),
            )
        return result, prompt_ui

    @staticmethod
    def _wait_for(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        raise AssertionError("PromptToolkit input was not processed before timeout")

    def test_normal_submission_is_returned_and_added_to_history(self) -> None:
        result, ui = self._run_prompt("ship it\r")

        self.assertEqual(result, "ship it")
        self.assertEqual(list(ui.history.get_strings()), ["ship it"])

    def test_slash_menu_arrows_choose_the_highlighted_command(self) -> None:
        result, ui = self._run_prompt(
            "/\x1b[B\r",
            commands=(("/model", "choose model"), ("/mcp", "show MCP tools")),
        )

        self.assertEqual(result, "/mcp")
        self.assertEqual(list(ui.history.get_strings()), ["/mcp"])

    def test_double_escape_clears_draft_then_up_restores_it(self) -> None:
        ui = ClaudePromptUI()
        result: dict[str, Any] = {}
        errors: list[BaseException] = []

        with create_pipe_input() as pipe_input:
            def run() -> None:
                try:
                    result["value"] = ui.run(
                        footer=lambda: "ready",
                        input=pipe_input,
                        output=DummyOutput(),
                    )
                except BaseException as exc:  # pragma: no cover - surfaced below
                    errors.append(exc)

            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            pipe_input.send_text("recover this\x1b\x1b")
            self._wait_for(lambda: ui.last_cleared_draft == "recover this")
            self.assertTrue(worker.is_alive())
            pipe_input.send_text("\x1b[A\r")
            worker.join(timeout=2.0)

        self.assertFalse(worker.is_alive())
        if errors:
            raise errors[0]
        self.assertEqual(result.get("value"), "recover this")
        self.assertEqual(ui.last_cleared_draft, "")

    def test_double_escape_on_blank_prompt_returns_rewind_token(self) -> None:
        result, ui = self._run_prompt("\x1b\x1b")

        self.assertEqual(result, REWIND_TOKEN)
        self.assertEqual(ui.action_draft, "")

    def test_ctrl_o_returns_transcript_token_without_losing_draft(self) -> None:
        result, ui = self._run_prompt("unfinished draft\x0f")

        self.assertEqual(result, TRANSCRIPT_TOKEN)
        self.assertEqual(ui.action_draft, "unfinished draft")

    def test_ctrl_s_stashes_then_restores_the_prompt(self) -> None:
        ui = ClaudePromptUI()
        result: dict[str, Any] = {}
        errors: list[BaseException] = []

        with create_pipe_input() as pipe_input:
            def run() -> None:
                try:
                    result["value"] = ui.run(
                        footer=lambda: "ready",
                        input=pipe_input,
                        output=DummyOutput(),
                    )
                except BaseException as exc:  # pragma: no cover - surfaced below
                    errors.append(exc)

            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            pipe_input.send_text("saved draft\x13")
            self._wait_for(lambda: ui.stash == "saved draft")
            self.assertTrue(worker.is_alive())
            pipe_input.send_text("\x13\r")
            worker.join(timeout=2.0)

        self.assertFalse(worker.is_alive())
        if errors:
            raise errors[0]
        self.assertEqual(result.get("value"), "saved draft")
        self.assertEqual(ui.stash, "")

    def test_alt_p_and_shift_tab_return_actions_with_draft_preserved(self) -> None:
        cases = (
            ("model draft\x1bp", MODEL_TOKEN, "model draft"),
            ("permission draft\x1b[Z", PERMISSIONS_TOKEN, "permission draft"),
        )
        for keys, expected_token, expected_draft in cases:
            with self.subTest(token=expected_token):
                result, ui = self._run_prompt(keys)
                self.assertEqual(result, expected_token)
                self.assertEqual(ui.action_draft, expected_draft)

    def test_ctrl_c_and_ctrl_d_require_a_second_press_to_exit(self) -> None:
        cases = (("\x03", "_last_ctrl_c"), ("\x04", "_last_ctrl_d"))
        for key, timestamp_name in cases:
            with self.subTest(key=repr(key)):
                ui = ClaudePromptUI()
                result: dict[str, Any] = {}
                errors: list[BaseException] = []
                with create_pipe_input() as pipe_input:
                    def run() -> None:
                        try:
                            result["value"] = ui.run(
                                footer=lambda: "ready",
                                input=pipe_input,
                                output=DummyOutput(),
                            )
                        except BaseException as exc:  # pragma: no cover
                            errors.append(exc)

                    worker = threading.Thread(target=run, daemon=True)
                    worker.start()
                    pipe_input.send_text(key)
                    self._wait_for(lambda: getattr(ui, timestamp_name) > 0.0)
                    self.assertTrue(worker.is_alive())
                    pipe_input.send_text(key)
                    worker.join(timeout=2.0)

                self.assertFalse(worker.is_alive())
                if errors:
                    raise errors[0]
                self.assertEqual(result.get("value"), EXIT_TOKEN)

    def test_ctrl_c_clears_a_draft_and_the_second_press_exits(self) -> None:
        result, ui = self._run_prompt("discard me\x03\x03")

        self.assertEqual(result, EXIT_TOKEN)
        self.assertGreater(ui._last_ctrl_c, 0.0)

    def test_new_submission_supersedes_a_cleared_draft_in_history(self) -> None:
        ui = ClaudePromptUI()
        result, ui = self._run_prompt("old draft\x1b\x1bnew prompt\r", ui=ui)

        self.assertEqual(result, "new prompt")
        self.assertEqual(list(ui.history.get_strings()), ["old draft", "new prompt"])
        restored: dict[str, Any] = {}
        with create_pipe_input() as pipe_input:
            worker = threading.Thread(
                target=lambda: restored.setdefault(
                    "value",
                    ui.run(
                        footer=lambda: "ready",
                        keybindings_path=None,
                        input=pipe_input,
                        output=DummyOutput(),
                    ),
                ),
                daemon=True,
            )
            worker.start()
            time.sleep(0.05)
            pipe_input.send_text("\x1b[A\r")
            worker.join(timeout=2.0)
        self.assertFalse(worker.is_alive())
        self.assertEqual(restored.get("value"), "new prompt")

    def test_ctrl_t_returns_tasks_token(self) -> None:
        result, ui = self._run_prompt("draft\x14")

        self.assertEqual(result, TASKS_TOKEN)
        self.assertEqual(ui.action_draft, "draft")

    def test_slash_menu_can_reach_items_beyond_the_eight_row_viewport(self) -> None:
        commands = tuple((f"/command-{index}", f"description {index}") for index in range(12))
        result, _ui = self._run_prompt(
            "/" + "\x1b[B" * 9 + "\r",
            commands=commands,
        )

        self.assertEqual(result, "/command-9")

    def test_escape_dismisses_slash_menu_without_replacing_the_draft(self) -> None:
        ui = ClaudePromptUI()
        result: dict[str, Any] = {}
        with create_pipe_input() as pipe_input:
            worker = threading.Thread(
                target=lambda: result.setdefault(
                    "value",
                    ui.run(
                        footer=lambda: "ready",
                        commands=(("/model", "choose model"),),
                        keybindings_path=None,
                        input=pipe_input,
                        output=DummyOutput(),
                    ),
                ),
                daemon=True,
            )
            worker.start()
            pipe_input.send_text("/\x1b")
            time.sleep(0.75)
            pipe_input.send_text("\r")
            worker.join(timeout=2.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual(result.get("value"), "/")

    def test_escape_enter_inserts_a_newline(self) -> None:
        result, _ui = self._run_prompt("first\x1b\rsecond\r")

        self.assertEqual(result, "first\nsecond")

    def test_ctrl_r_searches_prompt_history(self) -> None:
        ui = ClaudePromptUI()
        self._run_prompt("first command\r", ui=ui)
        self._run_prompt("second command\r", ui=ui)

        result, _ui = self._run_prompt("first\x12\r", ui=ui)
        self.assertEqual(result, "first command")

    def test_custom_keybinding_overrides_the_default_action(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            path = Path(raw_directory) / "keybindings.json"
            path.write_text(
                json.dumps(
                    {
                        "bindings": [
                            {
                                "context": "Chat",
                                "bindings": {"ctrl+k": "app:toggleTranscript"},
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            result, ui = self._run_prompt(
                "keep this\x0b",
                keybindings_path=path,
            )

        self.assertEqual(result, TRANSCRIPT_TOKEN)
        self.assertEqual(ui.action_draft, "keep this")

    def test_fast_mode_panel_requires_confirmation(self) -> None:
        with create_pipe_input() as pipe_input:
            pipe_input.send_text("\t\r")
            result = ClaudePromptUI.fast_mode(
                enabled=False,
                model_label="GPT",
                input=pipe_input,
                output=DummyOutput(),
            )

        self.assertTrue(result)

    def test_model_picker_supports_arrows_and_session_only(self) -> None:
        options = (("Alpha", "first"), ("Beta", "second"), ("Gamma", "third"))
        efforts = (("low", "high"), ("medium", "high"), ("xhigh", "max"))
        ui = ClaudePromptUI()

        with create_pipe_input() as pipe_input:
            pipe_input.send_text("\x1b[B\x1b[Cs")
            session_only = ui.select(
                title="Model",
                subtitle="Choose model and effort",
                options=options,
                initial_index=0,
                efforts=efforts,
                initial_effort="low",
                allow_session_only=True,
                input=pipe_input,
                output=DummyOutput(),
            )

        with create_pipe_input() as pipe_input:
            pipe_input.send_text("\x1b[A\x1b[D\r")
            persistent = ui.select(
                title="Model",
                subtitle="Choose model and effort",
                options=options,
                initial_index=0,
                efforts=efforts,
                initial_effort="low",
                allow_session_only=True,
                input=pipe_input,
                output=DummyOutput(),
            )

        self.assertEqual(session_only, MenuSelection(index=1, effort="high", persist=False))
        self.assertEqual(persistent, MenuSelection(index=2, effort="max", persist=True))


if __name__ == "__main__":
    unittest.main()
