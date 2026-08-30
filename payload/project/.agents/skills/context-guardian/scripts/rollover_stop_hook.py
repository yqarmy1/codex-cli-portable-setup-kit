#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Any


CONTROLLER_ROLLOVER_MARKER = "[CONTROLLER-CREATED AUTOMATIC ROLLOVER]"
TRUSTED_WORKSPACE_ROOT = Path(__file__).resolve().parents[4]
LIFECYCLE_RECEIPT_SCHEMA_VERSION = 2
CONTINUOUS_USER_INPUT_REQUIRED_MARKER = "[CONTINUOUS_USER_INPUT_REQUIRED]"
WORK_PROTOCOLS = {
    "function_call": "function_call_output",
    "custom_tool_call": "custom_tool_call_output",
}


def valid_task_id(value: Any) -> bool:
    return bool(
        isinstance(value, str)
        and re.fullmatch(r"[A-Za-z0-9._-]{1,128}", value)
    )


def configure_protocol_streams() -> None:
    """Use the UTF-8 encoding required by the hook JSON protocol.

    On Windows, ``py.exe`` can otherwise inherit a legacy console code page.
    Codex writes UTF-8 JSON to stdin, so a non-ASCII cwd would be decoded into
    a different path and the guard would silently treat the session as out of
    scope.
    """

    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8")


def emit(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def valid_rollover_policy(value: Any) -> bool:
    return isinstance(value, dict) and all(
        (
            value.get("enabled") is True,
            value.get("continue_until_objective_complete") is True,
            value.get("objective_completion_authority")
            == "exact-task-completed-lifecycle-receipt",
            value.get("source_retirement_authority")
            == "exact-source-retired-receipt-after-verified-target-work",
            value.get("desktop_scheduler") == "native-goal-worker-lease",
            value.get("desktop_stop_boundary_guard")
            == "trusted-synchronous-user-hook",
            value.get("desktop_visual_silence") is False,
            value.get("strict_clear_semantics") == "app-server-only",
        )
    )


def is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except (OSError, ValueError):
        return False
    return True


def find_workspace_root(cwd: Path) -> Path | None:
    """Return only the workspace that owns this installed trusted hook.

    This is a global user hook. Discovering a script from the event cwd would
    let an unrelated checkout impersonate Context Guardian and turn the hook
    into arbitrary code execution. The executable, registry, and policy must
    therefore all come from the workspace containing this installed script.
    """

    root = TRUSTED_WORKSPACE_ROOT.resolve()
    resolved_cwd = cwd.resolve()
    if not is_within(resolved_cwd, root):
        return None
    if not (
        root.joinpath(".context", "registry.json").is_file()
        and root.joinpath(
            ".agents",
            "skills",
            "context-guardian",
            "scripts",
            "contextctl.py",
        ).is_file()
    ):
        return None
    return root


def select_project(
    root: Path,
    registry: dict[str, Any],
    cwd: Path,
) -> dict[str, Any] | None:
    matches: list[tuple[int, dict[str, Any], Path]] = []
    for project in registry.get("projects", []):
        if not isinstance(project, dict) or not isinstance(project.get("path"), str):
            continue
        project_root = root.joinpath(project["path"]).resolve()
        if is_within(cwd, project_root):
            matches.append((len(project_root.parts), project, project_root))
    if not matches:
        return None
    _, selected, selected_root = max(matches, key=lambda item: item[0])

    # Do not let any registered project leak its state into an unregistered
    # nested repository.  This applies to child projects as well as the root.
    if cwd.resolve() != selected_root:
        for candidate in (cwd.resolve(), *cwd.resolve().parents):
            if candidate == selected_root:
                break
            if candidate.joinpath(".git").exists():
                return None
    return selected


def select_task_bound_project(
    root: Path,
    registry: dict[str, Any],
    cwd: Path,
    task_id: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Resolve an existing exact-task lifecycle binding before cwd fallback.

    A controller-created child task can retain the workspace root as its event
    cwd after the first marker-bearing prompt.  Its exact runtime or completion
    receipt is therefore the durable project identity for later hook events.
    Multiple matches are never guessed, and nested-repository boundaries remain
    authoritative outside the explicit workspace-root fallback.
    """

    resolved_cwd = cwd.resolve()
    # Preserve the unregistered nested-repository boundary before consulting
    # workspace-global lifecycle artifacts.  A registered sibling cwd is safe,
    # but an unrelated nested Git root remains out of scope.
    if resolved_cwd != root.resolve() and select_project(
        root, registry, resolved_cwd
    ) is None:
        return None, None
    matches: list[dict[str, Any]] = []
    for project in registry.get("projects", []):
        if (
            not isinstance(project, dict)
            or not isinstance(project.get("id"), str)
            or not isinstance(project.get("path"), str)
        ):
            continue
        session = runtime_file(root, project, task_id)
        receipt = lifecycle_receipt_file(root, project, task_id)
        if session.is_file() or receipt.is_file():
            matches.append(project)
    if len(matches) > 1:
        # Older hook revisions could preflight the event cwd before a later
        # marker-bearing controller prompt rebound the same Desktop task to
        # its real target project.  That leaves two internally valid runtime
        # files for one globally unique rollout.  Treat the runtime whose
        # trusted rollout telemetry progressed furthest as the authoritative
        # binding, but only when every candidate proves it belongs to the
        # exact same rollout.  Receipts and malformed/cross-rollout evidence
        # remain ambiguous and fail closed.
        if all(
            runtime_file(root, project, task_id).is_file()
            and not lifecycle_receipt_file(root, project, task_id).is_file()
            for project in matches
        ):
            evidence = [
                task_runtime_binding_evidence(root, project, task_id)
                for project in matches
            ]
            if all(item is not None for item in evidence):
                proven = [item for item in evidence if item is not None]
                rollout_ids = {item[0] for item in proven}
                offsets = [item[1] for item in proven]
                highest_offset = max(offsets)
                leaders = [
                    index
                    for index, offset in enumerate(offsets)
                    if offset == highest_offset
                ]
                if len(rollout_ids) == 1 and len(leaders) == 1:
                    return matches[leaders[0]], None
                if len(rollout_ids) == 1 and all(
                    proven[index][2] is not None for index in leaders
                ):
                    latest_seen = max(proven[index][2] for index in leaders)
                    latest = [
                        index
                        for index in leaders
                        if proven[index][2] == latest_seen
                    ]
                    if len(latest) == 1:
                        return matches[latest[0]], None
        return None, "exact task lifecycle artifacts match multiple registered projects"
    if not matches:
        return None, None

    return matches[0], None


def select_controller_project(
    root: Path,
    registry: dict[str, Any],
    cwd: Path,
    prompt: str,
) -> dict[str, Any] | None:
    contract = parse_controller_resume_contract(prompt)
    if contract is None:
        return None
    project_id = str(contract["project_id"])
    declared_path = str(contract["declared_path"])
    matches = [
        project
        for project in registry.get("projects", [])
        if isinstance(project, dict) and project.get("id") == project_id
    ]
    if len(matches) != 1:
        return None
    project = matches[0]
    registered_root = root.joinpath(str(project.get("path", "."))).resolve()
    declared = Path(declared_path)
    declared_root = (
        declared.resolve()
        if declared.is_absolute()
        else root.joinpath(declared).resolve()
    )
    if declared_root != registered_root:
        return None
    resolved_cwd = cwd.resolve()
    if resolved_cwd != root.resolve() and not is_within(resolved_cwd, registered_root):
        return None
    if is_within(resolved_cwd, registered_root) and resolved_cwd != registered_root:
        for candidate in (resolved_cwd, *resolved_cwd.parents):
            if candidate == registered_root:
                break
            if candidate.joinpath(".git").exists():
                return None
    return project


def parse_controller_resume_contract(prompt: str) -> dict[str, str] | None:
    """Parse the exact source and state binding of a controller-created target."""

    if prompt.count(CONTROLLER_ROLLOVER_MARKER) != 1:
        return None
    project_matches = re.findall(
        r"(?im)^Registered project:\s*`?([A-Za-z0-9._-]+)`?\s+at\s+`?([^`\r\n]+?)`?\s*$",
        prompt,
    )
    source_matches = re.findall(
        r"(?im)^Source task:\s*`?([A-Za-z0-9._-]{1,128})`?\s*$",
        prompt,
    )
    state_matches = re.findall(
        r"(?im)^Checkpoint state SHA-256:\s*`?([0-9a-f]{64})`?\s*$",
        prompt,
    )
    if not (
        len(project_matches) == 1
        and len(source_matches) == 1
        and len(state_matches) == 1
    ):
        return None
    project_id, declared_path = project_matches[0]
    return {
        "project_id": project_id,
        "declared_path": declared_path.strip(),
        "source_task_id": source_matches[0],
        "checkpoint_sha256": state_matches[0].lower(),
    }


def runtime_file(root: Path, project: dict[str, Any], task_id: str) -> Path:
    project_root = root.joinpath(str(project["path"])).resolve()
    state = project_root.joinpath(
        str(project.get("state", ".context/state.json"))
    ).resolve()
    return state.parent / "runtime" / f"{task_id}.json"


def lifecycle_receipt_file(root: Path, project: dict[str, Any], task_id: str) -> Path:
    return runtime_file(root, project, task_id).parent / "receipts" / f"{task_id}.json"


def task_runtime_binding_evidence(
    root: Path,
    project: dict[str, Any],
    task_id: str,
) -> tuple[str, int, dt.datetime | None] | None:
    """Return comparable exact-rollout progress for a runtime binding.

    ``started_rollout_file_id`` is the strongest identity.  Legacy runtimes
    predate that field, so their telemetry path is accepted only when its
    basename contains the exact task id.  The observed telemetry offset is
    monotonic within one rollout and therefore distinguishes an early cwd
    preflight from the later project-bound preflight without relying on wall
    clock ordering.
    """

    try:
        runtime = json.loads(
            runtime_file(root, project, task_id).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return None
    project_id = project.get("id")
    if (
        not isinstance(runtime, dict)
        or runtime.get("schema_version") != 1
        or runtime.get("project_id") != project_id
        or runtime.get("task_id") != task_id
    ):
        return None
    telemetry = runtime.get("codex_telemetry")
    if not isinstance(telemetry, dict) or telemetry.get("parse_errors", 0) != 0:
        return None

    rollout_file_id = runtime.get("started_rollout_file_id")
    if isinstance(rollout_file_id, str) and rollout_file_id:
        rollout_identity = f"file-id:{rollout_file_id}"
    else:
        rollout_path = telemetry.get("rollout_path")
        if not isinstance(rollout_path, str) or not rollout_path:
            return None
        normalized_path = rollout_path.replace("\\", "/").casefold()
        basename = normalized_path.rsplit("/", 1)[-1]
        if task_id.casefold() not in basename or not basename.endswith(".jsonl"):
            return None
        rollout_identity = f"path:{normalized_path}"

    observed_offset = telemetry.get("rollout_offset")
    if (
        not isinstance(observed_offset, int)
        or isinstance(observed_offset, bool)
        or observed_offset < 0
    ):
        return None
    last_seen: dt.datetime | None = None
    try:
        parsed_last_seen = dt.datetime.fromisoformat(str(runtime.get("last_seen_at")))
        if parsed_last_seen.tzinfo is not None:
            last_seen = parsed_last_seen
    except ValueError:
        pass
    return rollout_identity, observed_offset, last_seen


def valid_lifecycle_receipt(
    root: Path,
    project: dict[str, Any],
    project_id: str,
    task_id: str,
) -> bool:
    path = lifecycle_receipt_file(root, project, task_id)
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if (
        not isinstance(receipt, dict)
        or receipt.get("schema_version") != LIFECYCLE_RECEIPT_SCHEMA_VERSION
        or receipt.get("project_id") != project_id
        or receipt.get("task_id") != task_id
    ):
        return False
    kind = receipt.get("kind")
    if kind not in {"completed", "retired"}:
        return False
    for field_name in (
        "state_sha256",
        "started_state_sha256",
        "started_rules_fingerprint_sha256",
        "audited_state_sha256",
        "audit_rules_fingerprint_sha256",
        "audit_fingerprint_sha256",
    ):
        if re.fullmatch(r"[0-9a-f]{64}", str(receipt.get(field_name, ""))) is None:
            return False
    if receipt.get("audited_state_sha256") != receipt.get("state_sha256"):
        return False
    source_task_id = receipt.get("source_task_id")
    source_state = receipt.get("source_audited_state_sha256")
    source_rules = receipt.get("source_audit_rules_fingerprint_sha256")
    source_audit = receipt.get("source_audit_fingerprint_sha256")
    if source_task_id is None:
        if any(value is not None for value in (source_state, source_rules, source_audit)):
            return False
    else:
        if (
            not isinstance(source_task_id, str)
            or re.fullmatch(r"[A-Za-z0-9._-]{1,128}", source_task_id) is None
            or source_task_id == task_id
        ):
            return False
        if any(
            re.fullmatch(r"[0-9a-f]{64}", str(value or "")) is None
            for value in (source_state, source_rules, source_audit)
        ):
            return False
        if receipt.get("started_state_sha256") != source_state:
            return False
        if receipt.get("started_rules_fingerprint_sha256") != source_rules:
            return False
        if not valid_work_evidence(receipt.get("work_evidence"), task_id):
            return False
    replacement_task_id = receipt.get("replacement_task_id")
    if kind == "completed" and replacement_task_id is not None:
        return False
    if kind == "retired" and (
        not valid_task_id(replacement_task_id)
        or replacement_task_id == task_id
    ):
        return False
    if kind == "retired" and not valid_work_evidence(
        receipt.get("replacement_work_evidence"), str(replacement_task_id)
    ):
        return False
    if kind == "completed" and receipt.get("replacement_work_evidence") is not None:
        return False
    try:
        recorded_at = dt.datetime.fromisoformat(str(receipt.get("recorded_at")))
    except ValueError:
        return False
    return recorded_at.tzinfo is not None


def valid_work_evidence(value: Any, task_id: str) -> bool:
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        return False
    if value.get("task_id") != task_id:
        return False
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
        return False
    if not isinstance(value.get("rollout_file_id"), str) or not value["rollout_file_id"]:
        return False
    call_type = value.get("call_type")
    if (
        not isinstance(call_type, str)
        or WORK_PROTOCOLS.get(call_type) != value.get("output_type")
    ):
        return False
    for field_name in ("call_id_sha256", "call_record_sha256", "output_record_sha256"):
        if re.fullmatch(r"[0-9a-f]{64}", str(value.get(field_name, ""))) is None:
            return False
    try:
        observed_at = dt.datetime.fromisoformat(str(value.get("observed_at")))
    except ValueError:
        return False
    return observed_at.tzinfo is not None


def assistant_requests_manual_fresh_thread(value: Any) -> bool:
    text = re.sub(r"\s+", " ", value.strip()).casefold() if isinstance(value, str) else ""
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
    boundary = any(
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
    return closure and boundary and manual_action


def completion_semantic_text(value: Any) -> str:
    text = (
        unicodedata.normalize("NFKC", value.strip())
        if isinstance(value, str)
        else ""
    )
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

    # Structured completion summaries often name the state sections literally,
    # for example "Open / Uncertain and Next Actions are empty".  Without
    # removing that explicitly empty status, the broad future-action detector
    # below can pair the word "next" with a later mention of ``finish`` and
    # misclassify a completed receipt as contradictory.  Keep this narrow: a
    # populated Next Actions section (or one without an explicit empty value)
    # must still fail closed.
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


def assistant_response_contradicts_completion(value: Any) -> bool:
    """Reject final prose that directly says work remains after a completion receipt."""

    raw_text = value if isinstance(value, str) else ""
    text = completion_semantic_text(raw_text)
    if not text:
        return False
    if CONTINUOUS_USER_INPUT_REQUIRED_MARKER.casefold() in raw_text.casefold():
        return True
    if assistant_requests_manual_fresh_thread(text):
        return True

    chinese_pending = (
        r"(?:(?:(?:\u5c1a|\u4ecd|[\u9084\u8fd8])\s*\u672a|[\u9084\u8fd8]\s*[\u6c92\u6ca1](?:\u6709)?|\u672a)\s*"
        r"(?:(?:\u5168(?:\u90e8|\u6578)?|\u5b8c\u5168|\u771f\u6b63|\u5be6\u969b|\u6b63\u5f0f|\u6700\u7d42|\u5fb9\u5e95|\u5b8c\u6574)\s*)?"
        r"(?:\u5b8c\u6210|\u8655\u7406|\u5904\u7406|\u9a57\u8b49|\u9a8c\u8bc1|\u7a3d\u6838|\u5be9\u8a08|\u5ba1\u8ba1|\u7d50\u675f|\u7ed3\u675f|\u6536\u5c3e)"
        r"|(?:(?:\u4ecd|[\u9084\u8fd8])\s*(?:\u9700|\u9700\u8981|\u5f85)|\u5c1a\s*\u5f85|\u5f85)\s*"
        r"(?:\u5b8c\u6210|\u8655\u7406|\u5904\u7406|\u9a57\u8b49|\u9a8c\u8bc1|\u7a3d\u6838|\u5be9\u8a08|\u5ba1\u8ba1|\u57f7\u884c|\u6267\u884c|"
        r"\u88dc(?:\u505a|\u9f4a)?|\u4fee(?:\u6b63|\u5fa9)?|\u5be6\u4f5c|\u5b9e\u73b0|\u6e2c\u8a66|\u6d4b\u8bd5|\u6536\u5c3e))"
    )
    chinese_negator = (
        r"(?:(?:[\u4e26\u5e76]\s*)?(?:\u6c92\u6709|\u6ca1\u6709|\u7121|\u65e0)"
        r"(?:\s*(?:\u4efb\u4f55|\u4e00\u9805|\u4e00\u9879|\u4e00\u500b|\u4e00\u4e2a))?"
        r"|\u4e0d\u5b58\u5728(?:\s*(?:\u4efb\u4f55|\u4e00\u9805|\u4e00\u9879|\u4e00\u500b|\u4e00\u4e2a))?"
        r"|\u672a\s*(?:\u767c\u73fe|\u53d1\u73b0)(?:\s*(?:\u4efb\u4f55|\u4e00\u9805|\u4e00\u9879|\u4e00\u500b|\u4e00\u4e2a))?)"
    )
    semantic = re.sub(rf"{chinese_negator}\s*{chinese_pending}", "", text)
    future_action = (
        r"(?:\u7e7c\u7e8c|\u7ee7\u7eed|\u7e8c\u63a5|\u63a5\u7e8c|\u63a5\u7eed|\u7e8c\u505a|\u5b8c\u6210|\u8655\u7406|\u5904\u7406|\u9a57\u8b49|\u9a8c\u8bc1|"
        r"\u7a3d\u6838|\u5be9\u8a08|\u5ba1\u8ba1|\u57f7\u884c|\u6267\u884c|\u88dc|\u4fee\u6b63|\u4fee\u5fa9|\u5be6\u4f5c|\u5b9e\u73b0|\u6e2c\u8a66|\u6d4b\u8bd5)"
    )
    semantic = re.sub(
        rf"(?:\u4e0d|\u7121\u9700|\u65e0\u9700|\u4e0d\u7528)\s*(?:\u5c07|\u5c06|\u6703|\u4f1a)?"
        rf"[^\u3002\uff1b;.!?\uff01\uff1f\n]{{0,80}}{future_action}",
        "",
        semantic,
    )
    english_work = (
        r"(?:work|tasks?|items?|verification|audit|testing|tests?|fix(?:es)?|"
        r"implementation)"
    )
    for pattern in (
        rf"\b(?:there\s+(?:is|are)\s+)?no\s+"
        rf"(?:remaining|outstanding|unfinished|incomplete)\s+{english_work}\b",
        rf"\bno\s+(?:further|additional)\s+{english_work}\s+"
        r"(?:is|are)\s+(?:required|needed|pending|unfinished|incomplete)\b",
        rf"\b(?:there\s+(?:is|are)\s+)?no\s+{english_work}\s+"
        r"(?:that\s+)?remains?\s+(?:pending|unfinished|incomplete)\b",
        r"\bnothing\s+remains?\s+(?:pending|unfinished|incomplete)\b",
        rf"\bnone\s+of\s+the\s+{english_work}\s+"
        r"remains?\s+(?:pending|unfinished|incomplete)\b",
        rf"\b{english_work}\s+(?:is|are)\s+not\s+"
        r"(?:pending|unfinished|incomplete)\b",
        r"\b(?:the\s+)?(?:objective|project)\s+(?:is|was)\s+not\s+"
        r"(?:pending|unfinished|incomplete)\b",
        r"\b(?:will|would|should)\s+not\s+(?:need\s+to\s+)?"
        r"(?:continue|resume|finish|complete|run|verify|audit|test|fix|implement|handle)\w*\b",
        r"\b(?:will|would)\s+no\s+longer\s+"
        r"(?:continue|resume|finish|complete|run|verify|audit|test|fix|implement|handle)\w*\b",
        rf"\bno\s+(?:further|additional)\s+{english_work}\s+"
        r"(?:will|needs?\s+to|must)\s+(?:be\s+)?"
        r"(?:continue|resume|finish|complete|run|verify|audit|test|fix|implement|handle)\w*\b",
    ):
        semantic = re.sub(pattern, "", semantic)

    return bool(
        re.search(
            rf"{chinese_pending}"
            r"|(?:\u5269\u9918|\u5269\u4f59|\u9918\u4e0b|\u4f59\u4e0b).{0,80}"
            r"(?:\u5de5\u4f5c|\u9805\u76ee|\u9879\u76ee|\u9a57\u8b49|\u9a8c\u8bc1|\u7a3d\u6838|\u5be9\u8a08|\u5ba1\u8ba1|\u6e2c\u8a66|\u6d4b\u8bd5|\u4fee\u5fa9|\u4fee\u590d)"
            r"|(?:\u5c07|\u5c06|\u6703|\u4f1a|\u63a5\u4e0b\u4f86|\u63a5\u4e0b\u6765|\u4e0b\u4e00\u6b65|\u4e0b\u4e00\u8f2a|\u4e0b\u4e00\u8f6e|\u7a0d\u5f8c|\u7a0d\u540e|"
            r"\u5f8c\u7e8c|\u540e\u7eed).{0,80}"
            rf"{future_action}"
            r"|(?:\u81ea\u52d5|\u81ea\u52a8)(?:\u7e8c\u63a5|\u63a5\u7e8c|\u63a5\u7eed|\u7e7c\u7e8c|\u7ee7\u7eed)"
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
            r"|\b(?:still (?:need|needs|require|requires|must)|"
            r"remains? (?:pending|unfinished|incomplete))\b"
            r"|\b(?:will|next|later).{0,120}"
            r"(?:continue|resume|finish|complete|run|verify|audit|test|fix|implement|handle)\w*\b"
            r"|\b(?:remaining|outstanding).{0,120}"
            r"(?:work|tasks?|verification|audit|tests?)\b",
            semantic,
        )
    ) or bool(
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
            semantic,
        )
    )


def lifecycle_receipt_allows_stop(
    root: Path,
    project: dict[str, Any],
    project_id: str,
    task_id: str,
    last_assistant_message: Any,
) -> bool:
    if not valid_lifecycle_receipt(root, project, project_id, task_id):
        return False
    try:
        receipt = json.loads(
            lifecycle_receipt_file(root, project, task_id).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return False
    if receipt.get("kind") == "retired":
        return True
    return not assistant_response_contradicts_completion(last_assistant_message)


def completed_receipt_contradicts_assistant(
    root: Path,
    project: dict[str, Any],
    project_id: str,
    task_id: str,
    last_assistant_message: Any,
) -> bool:
    if not valid_lifecycle_receipt(root, project, project_id, task_id):
        return False
    try:
        receipt = json.loads(
            lifecycle_receipt_file(root, project, task_id).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return False
    return receipt.get("kind") == "completed" and assistant_response_contradicts_completion(
        last_assistant_message
    )


def controller_resume_bundle_error(
    bundle: str,
    task_id: str,
    checkpoint_sha256: str | None = None,
) -> str | None:
    if not bundle:
        return "preflight returned no bounded bundle"
    if not bundle.startswith("# BOUNDED CONTEXT BUNDLE"):
        return "preflight output was not a bounded context bundle"
    if re.search(r"(?im)^\s*CONTEXT_ROLLOVER_REQUIRED(?:\s|$)", bundle):
        return "preflight inherited a rollover sentinel"
    if f"- Task ID: `{task_id}`" not in bundle:
        return "preflight bundle was not bound to the target task"
    if (
        checkpoint_sha256 is not None
        and f"> State SHA-256: `{checkpoint_sha256}`" not in bundle
    ):
        return "preflight bundle state did not match the controller checkpoint"
    return None


def controller_target_runtime_error(
    root: Path,
    project: dict[str, Any],
    project_id: str,
    task_id: str,
    source_task_id: str,
    checkpoint_sha256: str,
) -> str | None:
    """Validate the exact source-audit lineage created by replacement preflight."""

    try:
        target = json.loads(runtime_file(root, project, task_id).read_text(encoding="utf-8"))
        source = json.loads(
            runtime_file(root, project, source_task_id).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return "target or source Guardian runtime was unreadable"
    if not isinstance(target, dict) or not isinstance(source, dict):
        return "target or source Guardian runtime was not an object"
    expected_target = {
        "schema_version": 1,
        "project_id": project_id,
        "task_id": task_id,
        "base_sha256": checkpoint_sha256,
        "started_state_sha256": checkpoint_sha256,
        "source_task_id": source_task_id,
        "source_audited_state_sha256": checkpoint_sha256,
    }
    for field_name, expected in expected_target.items():
        if target.get(field_name) != expected:
            return f"target runtime {field_name} did not match its controller binding"
    if (
        source.get("schema_version") != 1
        or source.get("project_id") != project_id
        or source.get("task_id") != source_task_id
        or source.get("base_sha256") != checkpoint_sha256
        or source.get("audited_state_sha256") != checkpoint_sha256
    ):
        return "source runtime did not match its audited controller checkpoint"
    for target_field, source_field in (
        ("source_audit_rules_fingerprint_sha256", "audit_rules_fingerprint_sha256"),
        ("source_audit_fingerprint_sha256", "audit_fingerprint_sha256"),
    ):
        target_value = str(target.get(target_field, ""))
        if (
            re.fullmatch(r"[0-9a-f]{64}", target_value) is None
            or target_value != source.get(source_field)
        ):
            return f"target runtime {target_field} did not match the source audit"
    telemetry = target.get("codex_telemetry")
    if (
        not isinstance(telemetry, dict)
        or telemetry.get("available") is not True
        or telemetry.get("parse_errors", 0) != 0
    ):
        return "target runtime telemetry was unavailable or unreliable"
    return None


def continuation_reason(project_id: str, task_id: str, sentinel: str) -> str:
    if sentinel.startswith("CONTEXT_ROLLOVER_REQUIRED"):
        action = (
            "Silently use $context-guardian now: checkpoint and audit the bounded "
            "state, then follow references/desktop-rollover.md to create one local "
            "fresh task and start concrete work in its initial turn. Validate the "
            "private target runtime receipt before retiring the source runtime."
        )
    else:
        action = (
            "Silently use $context-guardian now to checkpoint and audit the bounded "
            "state, then execute the next concrete unfinished action in this task."
        )
    return (
        "[INTERNAL CONTEXT CONTROL — DO NOT ECHO]\n\n"
        f"{action} Do not output task IDs, checkpoint details, handoff status, or "
        "rollover success. If the objective is already complete, validate, audit, "
        "and finish instead. Never ask for /new, /clear, or a separate continue."
    )


def active_objective_reason(project_id: str, task_id: str) -> str:
    return (
        "[INTERNAL CONTINUATION — DO NOT ECHO]\n\n"
        "The underlying user objective remains active. Do not stop with a status, "
        "handoff, checkpoint, or continuation report. Silently execute the next "
        "concrete unfinished action with tools and continue through later actions. "
        "Only after the objective is genuinely complete may you validate, audit, "
        "and finish the active Guardian runtime. If an unavoidable user decision "
        "is the sole blocker, use the request-user-input tool. Never ask for /new, "
        "/clear, or a separate continue."
    )


def contradictory_completion_reason(reopened: bool) -> str:
    recovery = (
        "The hook has already reopened the exact Guardian runtime."
        if reopened
        else
        "The hook could not reopen the exact Guardian runtime; retry its bounded "
        "preflight recovery in place."
    )
    return (
        "[INTERNAL CONTINUATION — DO NOT ECHO]\n\n"
        "The exact completed lifecycle receipt conflicts with the assistant's own "
        "statement that required work remains or that the user must create another "
        f"task. {recovery} Inspect bounded state and direct evidence, then execute "
        "the first concrete unfinished action. "
        "Do not ask the user for /new, /clear, or a separate continue."
    )


def reopen_contradictory_completion(
    contextctl: Path,
    root: Path,
    project: dict[str, Any],
    project_id: str,
    task_id: str,
    environment: dict[str, str],
) -> bool:
    session_runtime = runtime_file(root, project, task_id)
    receipt_path = lifecycle_receipt_file(root, project, task_id)
    if not valid_lifecycle_receipt(root, project, project_id, task_id):
        return False
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if receipt.get("kind") != "completed":
        return False
    source_task_id = receipt.get("source_task_id")
    command = [
        sys.executable,
        str(contextctl),
        "--root",
        str(root),
        "preflight",
        "--project",
        project_id,
        "--task-id",
        task_id,
        "--resume",
    ]
    if source_task_id is not None:
        if not valid_task_id(source_task_id):
            return False
        command.extend(["--replaces-task", source_task_id])
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if (
        completed.returncode != 0
        or not session_runtime.is_file()
        or receipt_path.is_file()
    ):
        return False
    try:
        runtime = json.loads(session_runtime.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    expected = {
        "project_id": project_id,
        "task_id": task_id,
        # contextctl preserves the original task-start lineage when reopening a
        # completed receipt.  The completed state and audit-rule fingerprints
        # may legitimately differ after checkpoints or rule edits.
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
        return False
    return not any(
        name in runtime
        for name in (
            "audited_state_sha256",
            "audit_rules_fingerprint_sha256",
            "audit_fingerprint_sha256",
            "work_evidence",
        )
    )


def run(payload: dict[str, Any]) -> dict[str, Any]:
    event = payload.get("hook_event_name")
    if event not in {"Stop", "UserPromptSubmit"}:
        return {}
    task_id = payload.get("session_id")
    cwd_value = payload.get("cwd")
    if not isinstance(task_id, str) or not task_id:
        return {}
    if not isinstance(cwd_value, str) or not cwd_value:
        return {}

    cwd = Path(cwd_value)
    root = find_workspace_root(cwd)
    if root is None:
        return {}
    if not valid_task_id(task_id):
        return {
            "decision": "block",
            "reason": (
                "Context Guardian session identity is invalid. Keep the current "
                "objective active, repair the exact task binding, and do not ask "
                "the user to continue."
            ),
        }
    try:
        registry_value = json.loads(
            root.joinpath(".context", "registry.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return {
            "decision": "block",
            "reason": (
                "Context Guardian registry is unreadable. Keep this objective active, "
                "repair the bounded registry, and do not ask the user to continue."
            ),
        }
    if not isinstance(registry_value, dict):
        return {
            "decision": "block",
            "reason": (
                "Context Guardian registry is invalid. Keep this objective active, "
                "repair the bounded registry, and do not ask the user to continue."
            ),
        }
    policy = registry_value.get("automatic_rollover")
    if not valid_rollover_policy(policy):
        return {
            "decision": "block",
            "reason": (
                "Context Guardian automatic-rollover policy is missing or invalid. "
                "Keep this objective active, repair the bounded policy, and do not "
                "ask the user to continue."
            ),
        }
    prompt = payload.get("prompt") if event == "UserPromptSubmit" else None
    controller_resume = bool(
        isinstance(prompt, str) and CONTROLLER_ROLLOVER_MARKER in prompt
    )
    controller_contract = (
        parse_controller_resume_contract(prompt)
        if controller_resume and isinstance(prompt, str)
        else None
    )
    task_binding_error: str | None = None
    if controller_resume and isinstance(prompt, str):
        project = select_controller_project(root, registry_value, cwd, prompt)
    else:
        project, task_binding_error = select_task_bound_project(
            root,
            registry_value,
            cwd,
            task_id,
        )
        if project is None and task_binding_error is None:
            project = select_project(root, registry_value, cwd)
    if task_binding_error is not None:
        return {
            "decision": "block",
            "reason": (
                "Context Guardian exact task project binding is ambiguous or "
                f"inconsistent: {task_binding_error}. Keep the current objective "
                "active, repair the bounded lifecycle artifacts, and do not ask "
                "the user to continue."
            ),
        }
    if project is None:
        if controller_resume:
            return {
                "decision": "block",
                "reason": (
                    "Automatic rollover target project binding is invalid. Keep "
                    "the source Guardian session active and repair the existing "
                    "target; do not ask the user for /new or continue."
                ),
            }
        return {}
    project_id = project.get("id")
    if not isinstance(project_id, str) or not project_id:
        return {}
    session_runtime = runtime_file(root, project, task_id)

    contextctl = root.joinpath(
        ".agents", "skills", "context-guardian", "scripts", "contextctl.py"
    )
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    if event == "UserPromptSubmit":
        if session_runtime.is_file() and not controller_resume:
            return {}
        command = [
            sys.executable,
            str(contextctl),
            "--root",
            str(root),
            "preflight",
            "--project",
            project_id,
            "--task-id",
            task_id,
        ]
        if controller_resume:
            command.append("--resume")
            assert controller_contract is not None
            command.extend(
                ["--replaces-task", str(controller_contract["source_task_id"])]
            )
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                env=environment,
            )
        except (OSError, subprocess.SubprocessError):
            if controller_resume:
                return {
                    "decision": "block",
                    "reason": (
                        "Automatic rollover target preflight could not run before "
                        "dispatch. Keep the source Guardian session active, repair "
                        "the target in place, and do not ask the user for /new or "
                        "continue."
                    ),
                }
            return {
                "decision": "block",
                "reason": (
                    "Context Guardian preflight could not run. Keep this objective "
                    "active, repair the bounded session, and do not ask the user "
                    "for /new or continue."
                ),
            }
        if completed.returncode != 0:
            if controller_resume:
                return {
                    "decision": "block",
                    "reason": (
                        "Automatic rollover target preflight failed before dispatch. "
                        "Keep the source Guardian session active, repair the target "
                        "in place, and do not ask the user for /new or continue."
                    ),
                }
            return {
                "decision": "block",
                "reason": (
                    "Context Guardian preflight failed. Keep this objective active, "
                    "repair the bounded session, and do not ask the user for /new "
                    "or continue."
                ),
            }
        bundle = completed.stdout.strip()
        if controller_resume:
            assert controller_contract is not None
            bundle_error = controller_resume_bundle_error(
                bundle,
                task_id,
                str(controller_contract["checkpoint_sha256"]),
            )
            if bundle_error is not None:
                return {
                    "decision": "block",
                    "reason": (
                        "Automatic rollover target preflight produced an invalid "
                        f"bundle before dispatch: {bundle_error}. Keep the source "
                        "Guardian session active, repair the target in place, and do "
                        "not ask the user for /new or continue."
                    ),
                }
            runtime_error = controller_target_runtime_error(
                root,
                project,
                project_id,
                task_id,
                str(controller_contract["source_task_id"]),
                str(controller_contract["checkpoint_sha256"]),
            )
            if runtime_error is not None:
                return {
                    "decision": "block",
                    "reason": (
                        "Automatic rollover target preflight produced invalid source "
                        f"lineage before dispatch: {runtime_error}. Keep the source "
                        "Guardian session active, repair the target in place, and do "
                        "not ask the user for /new or continue."
                    ),
                }
        if not bundle:
            return {
                "decision": "block",
                "reason": (
                    "Context Guardian preflight returned no bounded context. Keep "
                    "this objective active and repair the session before work."
                ),
            }
        return {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": bundle[:20000],
            }
        }

    if not session_runtime.is_file():
        if lifecycle_receipt_allows_stop(
            root,
            project,
            project_id,
            task_id,
            payload.get("last_assistant_message"),
        ):
            return {}
        if completed_receipt_contradicts_assistant(
            root,
            project,
            project_id,
            task_id,
            payload.get("last_assistant_message"),
        ):
            reopened = reopen_contradictory_completion(
                contextctl,
                root,
                project,
                project_id,
                task_id,
                environment,
            )
            return {
                "decision": "block",
                "reason": contradictory_completion_reason(reopened),
            }
        # Raw runtime disappearance is not a completion signal.  Keep working
        # and let the continuation recreate the exact guarded session.
        return {
            "decision": "block",
            "reason": active_objective_reason(project_id, task_id),
        }
    try:
        completed = subprocess.run(
            [
                sys.executable,
                str(contextctl),
                "--root",
                str(root),
                "pulse",
                "--project",
                project_id,
                "--task-id",
                task_id,
                "--count",
                "0",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError):
        return {
            "decision": "block",
            "reason": active_objective_reason(project_id, task_id),
        }

    sentinel = completed.stdout.strip().splitlines()[0] if completed.stdout.strip() else ""
    if sentinel.startswith(("CONTEXT_ROLLOVER_REQUIRED", "CONTEXT_CHECKPOINT_DUE")):
        return {
            "decision": "block",
            "reason": continuation_reason(project_id, task_id, sentinel),
        }
    if completed.returncode not in (0, 2):
        return {
            "decision": "block",
            "reason": active_objective_reason(project_id, task_id),
        }
    # Runtime existence is the deterministic unfinished-work receipt.  This is
    # intentionally enforced even when stop_hook_active is true: a Stop-created
    # continuation is still not allowed to end at a handoff/progress summary.
    # A genuine completion removes this exact runtime and writes an exact
    # lifecycle receipt via contextctl finish.
    if session_runtime.is_file():
        return {
            "decision": "block",
            "reason": active_objective_reason(project_id, task_id),
        }
    if lifecycle_receipt_allows_stop(
        root,
        project,
        project_id,
        task_id,
        payload.get("last_assistant_message"),
    ):
        return {}
    if completed_receipt_contradicts_assistant(
        root,
        project,
        project_id,
        task_id,
        payload.get("last_assistant_message"),
    ):
        reopened = reopen_contradictory_completion(
            contextctl,
            root,
            project,
            project_id,
            task_id,
            environment,
        )
        return {
            "decision": "block",
            "reason": contradictory_completion_reason(reopened),
        }
    return {
        "decision": "block",
        "reason": active_objective_reason(project_id, task_id),
    }


def main() -> int:
    configure_protocol_streams()
    try:
        payload = json.load(sys.stdin)
    except (OSError, UnicodeError, json.JSONDecodeError):
        emit({})
        return 0
    emit(run(payload) if isinstance(payload, dict) else {})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
