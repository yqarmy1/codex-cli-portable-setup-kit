#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import socket
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
STATE_BYTE_LIMIT = 8 * 1024
ACTIVE_BYTE_LIMIT = 4 * 1024
ACTIVE_LINE_LIMIT = 120
AGENT_BYTE_LIMIT = 8 * 1024
ROOT_AGENT_BYTE_LIMIT = 4 * 1024
DEFAULT_BUNDLE_LIMIT = 16 * 1024
DEFAULT_CHECKPOINT_AFTER = 20
DEFAULT_MAX_SESSION_COMPACTIONS = 1
DEFAULT_MAX_CONTEXT_FILL_PERCENT = 55
DEFAULT_MAX_TOOL_OUTPUT_CHARS = 750_000
DEFAULT_MAX_SINGLE_TOOL_OUTPUT_CHARS = 100_000
DEFAULT_MAX_TOOL_CALLS = 120
LIFECYCLE_RECEIPT_SCHEMA_VERSION = 2
LOCK_STALE_SECONDS = 5 * 60
LIST_FIELDS = ("confirmed", "open", "next_actions", "validation", "lookup")
WORK_EVIDENCE_CALL_OUTPUT_TYPES = {
    "custom_tool_call": "custom_tool_call_output",
    "function_call": "function_call_output",
}
OPAQUE_OUTPUT_KEYS = {
    "audio_url",
    "blob",
    "encrypted_content",
    "image_url",
}
BINARY_OUTPUT_TYPES = {
    "audio",
    "blob",
    "image",
    "input_audio",
    "input_image",
}
TOP_LEVEL_TYPE = re.compile(
    rb'^\s*\{\s*"timestamp"\s*:\s*"(?:\\.|[^"\\])*"\s*,'
    rb'\s*"type"\s*:\s*"([^"\\]+)"'
)
TRUNCATION_MARKER = re.compile(
    r"(?:…|\.\.\.)\s*\d+\s+"
    r"(?:tokens?|chars?|characters?|lines?|bytes?)\s+truncated\s*"
    r"(?:…|\.\.\.)",
    re.IGNORECASE,
)


class ContextError(RuntimeError):
    pass


def configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ContextError(f"missing file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ContextError(f"invalid JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ContextError(f"JSON root must be an object: {path}")
    return value


def repo_hooks_disabled(root: Path) -> bool:
    """Return true only for an explicit, valid project-local hooks opt-out."""
    config_path = root / ".codex" / "config.toml"
    if not config_path.is_file():
        return False
    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return False
    features = config.get("features")
    return isinstance(features, dict) and features.get("hooks") is False


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def canonical_state_bytes(state: dict[str, Any]) -> bytes:
    return json.dumps(
        state,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def state_sha256(state: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_state_bytes(state)).hexdigest()


def require_state_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ContextError(f"{label} must be a lowercase SHA-256 value")
    return value


def process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        process_query_limited_information = 0x1000
        handle = kernel32.OpenProcess(
            process_query_limited_information,
            False,
            pid,
        )
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return ctypes.get_last_error() == 5
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        if getattr(exc, "winerror", None) in {87, 1168}:
            return False
        return False
    return True


def lock_is_stale(lock_path: Path) -> bool:
    try:
        stat = lock_path.stat()
    except FileNotFoundError:
        return False
    age = max(0.0, dt.datetime.now().timestamp() - stat.st_mtime)
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return age >= LOCK_STALE_SECONDS
    if not isinstance(payload, dict):
        return age >= LOCK_STALE_SECONDS
    hostname = payload.get("hostname")
    pid = payload.get("pid")
    if hostname == socket.gethostname() and isinstance(pid, int):
        return not process_is_alive(pid)
    return age >= LOCK_STALE_SECONDS


@contextlib.contextmanager
def checkpoint_lock(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = -1
    for attempt in range(2):
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError as exc:
            if attempt == 0 and lock_is_stale(lock_path):
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    pass
                continue
            raise ContextError(
                f"checkpoint lock is active: {lock_path}"
            ) from exc
    try:
        payload = json.dumps(
            {
                "pid": os.getpid(),
                "hostname": socket.gethostname(),
                "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            },
            sort_keys=True,
        ).encode("utf-8")
        os.write(fd, payload)
        os.close(fd)
        fd = -1
        yield
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def load_registry(root: Path) -> dict[str, Any]:
    registry = read_json(root / ".context" / "registry.json")
    if registry.get("schema_version") != SCHEMA_VERSION:
        raise ContextError("registry schema_version is unsupported")
    projects = registry.get("projects")
    if not isinstance(projects, list) or not projects:
        raise ContextError("registry projects must be a non-empty list")
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    for index, project in enumerate(projects):
        if not isinstance(project, dict):
            raise ContextError(f"registry projects[{index}] must be an object")
        for field in ("id", "name", "path"):
            value = project.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ContextError(
                    f"registry projects[{index}].{field} must be a non-empty string"
                )
        project_id = str(project["id"]).casefold()
        project_path = str(project["path"]).casefold()
        if project_id in seen_ids:
            raise ContextError(f"duplicate registry project id: {project['id']}")
        if project_path in seen_paths:
            raise ContextError(f"duplicate registry project path: {project['path']}")
        seen_ids.add(project_id)
        seen_paths.add(project_path)
        required_auth = project.get("required_authorization")
        if required_auth is not None and not isinstance(required_auth, dict):
            raise ContextError(
                f"registry project {project['id']} required_authorization must be an object"
            )
    checkpoint_after = registry.get(
        "checkpoint_after_iterations", DEFAULT_CHECKPOINT_AFTER
    )
    if (
        not isinstance(checkpoint_after, int)
        or isinstance(checkpoint_after, bool)
        or not 1 <= checkpoint_after <= 100
    ):
        raise ContextError("checkpoint_after_iterations must be an integer from 1 to 100")
    integer_limits = (
        ("max_session_compactions", DEFAULT_MAX_SESSION_COMPACTIONS, 0, 100),
        ("max_context_fill_percent", DEFAULT_MAX_CONTEXT_FILL_PERCENT, 1, 100),
        ("max_tool_output_chars", DEFAULT_MAX_TOOL_OUTPUT_CHARS, 1, 100_000_000),
        (
            "max_single_tool_output_chars",
            DEFAULT_MAX_SINGLE_TOOL_OUTPUT_CHARS,
            1,
            10_000_000,
        ),
        ("max_tool_calls", DEFAULT_MAX_TOOL_CALLS, 1, 100_000),
    )
    for name, default, minimum, maximum in integer_limits:
        value = registry.get(name, default)
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or not minimum <= value <= maximum
        ):
            raise ContextError(
                f"{name} must be an integer from {minimum} to {maximum}"
            )
    return registry


def resolve_project(root: Path, registry: dict[str, Any], selector: str) -> dict[str, Any]:
    folded = selector.casefold()
    matches = [
        item
        for item in registry["projects"]
        if isinstance(item, dict)
        and folded
        in {
            str(item.get("id", "")).casefold(),
            str(item.get("name", "")).casefold(),
            str(item.get("path", "")).casefold(),
        }
    ]
    if len(matches) != 1:
        raise ContextError(f"project selector must match exactly one registry entry: {selector}")
    project = matches[0]
    project_path = (root / str(project["path"])).resolve()
    try:
        project_path.relative_to(root.resolve())
    except ValueError as exc:
        raise ContextError(f"project escapes root: {project_path}") from exc
    if not project_path.is_dir():
        raise ContextError(f"project directory is missing: {project_path}")
    return project


def project_paths(root: Path, project: dict[str, Any]) -> dict[str, Path]:
    base = (root / str(project["path"])).resolve()
    return {
        "base": base,
        "agent": base / str(project.get("agent", "AGENTS.md")),
        "active": base / str(project.get("active", "ACTIVE_STATE.md")),
        "state": base / str(project.get("state", ".context/state.json")),
        "history": base / str(project.get("history", ".context/history")),
    }


def validate_state(state: dict[str, Any], expected_id: str | None = None) -> None:
    errors: list[str] = []
    if state.get("schema_version") != SCHEMA_VERSION:
        errors.append("schema_version must be 1")
    project_id = state.get("project_id")
    if not isinstance(project_id, str) or not project_id.strip():
        errors.append("project_id must be a non-empty string")
    elif expected_id and project_id != expected_id:
        errors.append(f"project_id must be {expected_id!r}")
    if not isinstance(state.get("project"), str) or not state["project"].strip():
        errors.append("project must be a non-empty string")
    updated = state.get("updated_at")
    if not isinstance(updated, str):
        errors.append("updated_at must be YYYY-MM-DD")
    else:
        try:
            dt.date.fromisoformat(updated)
        except ValueError:
            errors.append("updated_at must be YYYY-MM-DD")
    objective = state.get("objective")
    if not isinstance(objective, str) or not objective.strip():
        errors.append("objective must be a non-empty string")
    elif len(objective) > 800:
        errors.append("objective exceeds 800 characters")

    for field in LIST_FIELDS:
        value = state.get(field)
        if not isinstance(value, list):
            errors.append(f"{field} must be a list")
            continue
        if len(value) > 12:
            errors.append(f"{field} exceeds 12 items")
        for index, item in enumerate(value):
            if not isinstance(item, str) or not item.strip():
                errors.append(f"{field}[{index}] must be a non-empty string")
            elif len(item) > 600:
                errors.append(f"{field}[{index}] exceeds 600 characters")

    authorization = state.get("authorization")
    if authorization is not None:
        if not isinstance(authorization, dict):
            errors.append("authorization must be an object")
        else:
            if authorization.get("schema_version") != 1:
                errors.append("authorization.schema_version must be 1")
            for field in (
                "status",
                "scope",
                "executor",
                "project_id",
                "decision_rule",
                "continuation_rule",
            ):
                value = authorization.get(field)
                if not isinstance(value, str) or not value.strip():
                    errors.append(
                        f"authorization.{field} must be a non-empty string"
                    )
            if expected_id and authorization.get("project_id") != expected_id:
                errors.append(
                    f"authorization.project_id must be {expected_id!r}"
                )

    encoded = json.dumps(state, ensure_ascii=False, sort_keys=True).encode("utf-8")
    if len(encoded) > STATE_BYTE_LIMIT:
        errors.append(f"state exceeds {STATE_BYTE_LIMIT} bytes")
    if errors:
        raise ContextError("; ".join(errors))


def validate_project_state(
    state: dict[str, Any],
    project: dict[str, Any],
    root: Path | None = None,
) -> None:
    text = json.dumps(state, ensure_ascii=False, sort_keys=True)
    errors: list[str] = []
    for term in project.get("required_state_terms", []):
        if not isinstance(term, str) or not term:
            errors.append("registry required_state_terms must contain non-empty strings")
        elif term not in text:
            errors.append(f"state missing required project invariant: {term}")
    for term in project.get("forbidden_state_terms", []):
        if not isinstance(term, str) or not term:
            errors.append("registry forbidden_state_terms must contain non-empty strings")
        elif term in text:
            errors.append(f"state contains forbidden project invariant: {term}")
    required_authorization = project.get("required_authorization")
    if required_authorization is not None:
        authorization = state.get("authorization")
        if not isinstance(authorization, dict):
            errors.append("state missing structured authorization")
        else:
            for field, expected in required_authorization.items():
                actual = authorization.get(field)
                if actual != expected:
                    errors.append(
                        "authorization invariant mismatch: "
                        f"{field} must be {expected!r}, got {actual!r}"
                    )
    if root is not None:
        workspace_root = root.resolve()
        project_root = (workspace_root / str(project["path"])).resolve()
        for index, item in enumerate(state.get("lookup", [])):
            candidate = Path(item)
            if not candidate.is_absolute():
                continue
            try:
                resolved = candidate.resolve()
                resolved.relative_to(workspace_root)
            except (OSError, ValueError):
                continue
            try:
                resolved.relative_to(project_root)
            except ValueError:
                errors.append(
                    f"lookup[{index}] points outside the registered project root"
                )
    if errors:
        raise ContextError("; ".join(errors))


def render_state(state: dict[str, Any]) -> str:
    validate_state(state)
    lines = [
        f"# {state['project']} — ACTIVE STATE",
        "",
        "> Generated by contextctl from `.context/state.json`; do not edit by hand.",
        f"> Updated: {state['updated_at']}",
    ]
    authorization = state.get("authorization")
    if isinstance(authorization, dict):
        lines.extend([
            "",
            "## Scope Metadata",
            "",
            "- Structured project scope is present and validated.",
            "- Load its values from `.context/state.json` only when the current "
            "action requires a scope review.",
        ])
    lines.extend(["", "## Objective", "", state["objective"].strip()])
    sections = (
        ("Confirmed", "confirmed"),
        ("Open / Uncertain", "open"),
        ("Next Actions", "next_actions"),
        ("Validation", "validation"),
    )
    for title, field in sections:
        lines.extend(["", f"## {title}", ""])
        values = state[field]
        if values:
            lines.extend(f"- {item.strip()}" for item in values)
        else:
            lines.append("- None.")
    rendered = "\n".join(lines).rstrip() + "\n"
    encoded = rendered.encode("utf-8")
    if len(encoded) > ACTIVE_BYTE_LIMIT:
        raise ContextError(f"rendered ACTIVE_STATE exceeds {ACTIVE_BYTE_LIMIT} bytes")
    if len(rendered.splitlines()) > ACTIVE_LINE_LIMIT:
        raise ContextError(f"rendered ACTIVE_STATE exceeds {ACTIVE_LINE_LIMIT} lines")
    return rendered


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_bundle(
    root: Path,
    registry: dict[str, Any],
    project: dict[str, Any],
    *,
    full_rules: bool = False,
    include_active_state: bool = True,
) -> str:
    paths = project_paths(root, project)
    root_agent_path = root / "AGENTS.md"
    state = read_json(paths["state"])
    validate_state(state, str(project["id"]))
    validate_project_state(state, project, root)
    active = render_state(state).strip()
    state_hash = state_sha256(state)
    if include_active_state:
        state_notice = (
            "> Historical files are excluded. Structured machine state overrides stale prose.\n\n"
        )
    else:
        state_notice = (
            "> Fresh task: prior task state is intentionally not injected. The current user "
            "request is authoritative; use `preflight --resume` only for an explicit continuation.\n\n"
        )
    header = (
        f"# BOUNDED CONTEXT BUNDLE — {project['name']}\n\n"
        + state_notice
        + f"> State SHA-256: `{state_hash}`\n\n"
    )
    project_agent_path = paths["agent"].resolve()
    same_agent = project_agent_path == root_agent_path.resolve()
    if full_rules:
        root_agent = root_agent_path.read_text(encoding="utf-8").strip()
        rules = "## Root Rules\n\n" + root_agent + "\n\n"
        if not same_agent:
            project_agent = project_agent_path.read_text(encoding="utf-8").strip()
            rules += "## Project Rules\n\n" + project_agent + "\n\n"
    else:
        rules = (
            "## Static Rule References\n\n"
            f"- Root `AGENTS.md` SHA-256: `{file_sha256(root_agent_path)}`\n"
            f"- Project `AGENTS.md` SHA-256: `{file_sha256(project_agent_path)}`\n"
            "- Codex loads the root `AGENTS.md`; child rules are injected below when distinct.\n"
            "- If the host does not load the root rules, rerun preflight with `--full-rules`.\n\n"
        )
        if not same_agent:
            project_agent = project_agent_path.read_text(encoding="utf-8").strip()
            rules += (
                "## Project Rules\n\n"
                "> Injected because a task opened from the workspace root may not load "
                "an ignored child repository's AGENTS.md.\n\n"
                + project_agent
                + "\n\n"
            )
    if include_active_state:
        state_section = "## Active State\n\n" + active + "\n"
    else:
        state_section = (
            "## Prior State Pointer\n\n"
            f"- Snapshot: `{paths['active']}`\n"
            "- Load it only when the current user request explicitly resumes prior work.\n"
        )
    bundle = header + rules + state_section
    limit = int(registry.get("bundle_limit_bytes", DEFAULT_BUNDLE_LIMIT))
    size = len(bundle.encode("utf-8"))
    if size > limit:
        raise ContextError(f"preflight bundle is {size} bytes; limit is {limit}")
    return bundle


def resolve_task_id(requested: str | None) -> str | None:
    task_id = requested or os.environ.get("CODEX_THREAD_ID")
    if task_id is None:
        return None
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", task_id):
        raise ContextError("task id must use 1-128 letters, digits, dot, underscore, or dash")
    return task_id


def runtime_path(paths: dict[str, Path], task_id: str) -> Path:
    return paths["state"].parent / "runtime" / f"{task_id}.json"


def completion_receipt_path(paths: dict[str, Path], task_id: str) -> Path:
    return paths["state"].parent / "runtime" / "receipts" / f"{task_id}.json"


def validate_lifecycle_receipt(
    receipt: dict[str, Any],
    project_id: str,
    task_id: str,
    *,
    expected_kind: str | None = None,
    expected_replacement_task_id: str | None = None,
) -> None:
    if receipt.get("schema_version") != LIFECYCLE_RECEIPT_SCHEMA_VERSION:
        raise ContextError("lifecycle receipt schema is invalid")
    if receipt.get("project_id") != project_id or receipt.get("task_id") != task_id:
        raise ContextError("lifecycle receipt identity is invalid")
    kind = receipt.get("kind")
    if kind not in {"completed", "retired"}:
        raise ContextError("lifecycle receipt kind is invalid")
    if expected_kind is not None and kind != expected_kind:
        raise ContextError("lifecycle receipt kind does not match this request")
    require_state_sha256(receipt.get("state_sha256"), "receipt state_sha256")
    started_state_sha256 = require_state_sha256(
        receipt.get("started_state_sha256"),
        "receipt started_state_sha256",
    )
    started_rules_fingerprint_sha256 = require_state_sha256(
        receipt.get("started_rules_fingerprint_sha256"),
        "receipt started_rules_fingerprint_sha256",
    )
    audited_state_sha256 = require_state_sha256(
        receipt.get("audited_state_sha256"),
        "receipt audited_state_sha256",
    )
    require_state_sha256(
        receipt.get("audit_rules_fingerprint_sha256"),
        "receipt audit_rules_fingerprint_sha256",
    )
    require_state_sha256(
        receipt.get("audit_fingerprint_sha256"),
        "receipt audit_fingerprint_sha256",
    )
    if audited_state_sha256 != receipt.get("state_sha256"):
        raise ContextError("lifecycle receipt audit state does not match its state")
    source_task_id = receipt.get("source_task_id")
    source_state_sha256 = receipt.get("source_audited_state_sha256")
    source_rules_fingerprint_sha256 = receipt.get(
        "source_audit_rules_fingerprint_sha256"
    )
    source_audit_fingerprint_sha256 = receipt.get(
        "source_audit_fingerprint_sha256"
    )
    if source_task_id is None:
        if any(
            value is not None
            for value in (
                source_state_sha256,
                source_rules_fingerprint_sha256,
                source_audit_fingerprint_sha256,
            )
        ):
            raise ContextError("lifecycle receipt source lineage is incomplete")
    else:
        if (
            not isinstance(source_task_id, str)
            or re.fullmatch(r"[A-Za-z0-9._-]{1,128}", source_task_id) is None
            or source_task_id == task_id
        ):
            raise ContextError("lifecycle receipt source task is invalid")
        source_state_sha256 = require_state_sha256(
            source_state_sha256,
            "receipt source_audited_state_sha256",
        )
        source_rules_fingerprint_sha256 = require_state_sha256(
            source_rules_fingerprint_sha256,
            "receipt source_audit_rules_fingerprint_sha256",
        )
        require_state_sha256(
            source_audit_fingerprint_sha256,
            "receipt source_audit_fingerprint_sha256",
        )
        if started_state_sha256 != source_state_sha256:
            raise ContextError("lifecycle receipt start state does not match its source")
        if started_rules_fingerprint_sha256 != source_rules_fingerprint_sha256:
            raise ContextError("lifecycle receipt start rules do not match its source")
        validate_work_evidence(receipt.get("work_evidence"), task_id)
    replacement_task_id = receipt.get("replacement_task_id")
    if kind == "completed":
        if replacement_task_id is not None:
            raise ContextError("completed receipt cannot name a replacement task")
    elif (
        not isinstance(replacement_task_id, str)
        or re.fullmatch(r"[A-Za-z0-9._-]{1,128}", replacement_task_id) is None
        or replacement_task_id == task_id
    ):
        raise ContextError("retired receipt replacement task is invalid")
    if kind == "retired":
        validate_work_evidence(
            receipt.get("replacement_work_evidence"),
            str(replacement_task_id),
        )
    elif receipt.get("replacement_work_evidence") is not None:
        raise ContextError("completed receipt cannot contain replacement work evidence")
    if replacement_task_id != expected_replacement_task_id:
        raise ContextError("lifecycle receipt replacement does not match this request")
    recorded_at = receipt.get("recorded_at")
    if not isinstance(recorded_at, str) or not recorded_at:
        raise ContextError("lifecycle receipt recorded_at is invalid")
    try:
        parsed_at = dt.datetime.fromisoformat(recorded_at)
    except ValueError as exc:
        raise ContextError("lifecycle receipt recorded_at is invalid") from exc
    if parsed_at.tzinfo is None:
        raise ContextError("lifecycle receipt recorded_at must include a timezone")


def validate_work_evidence(value: Any, task_id: str) -> None:
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ContextError("target concrete-work evidence is missing or invalid")
    if value.get("task_id") != task_id:
        raise ContextError("target concrete-work evidence task is invalid")
    baseline_offset = value.get("baseline_offset")
    observed_offset = value.get("observed_offset")
    if (
        not isinstance(baseline_offset, int)
        or isinstance(baseline_offset, bool)
        or baseline_offset < 0
        or not isinstance(observed_offset, int)
        or isinstance(observed_offset, bool)
        or observed_offset <= baseline_offset
    ):
        raise ContextError("target concrete-work offsets are invalid")
    rollout_file_id = value.get("rollout_file_id")
    if not isinstance(rollout_file_id, str) or not rollout_file_id:
        raise ContextError("target concrete-work rollout identity is invalid")
    call_type = value.get("call_type")
    output_type = value.get("output_type")
    if (
        not isinstance(call_type, str)
        or WORK_EVIDENCE_CALL_OUTPUT_TYPES.get(call_type) != output_type
    ):
        raise ContextError("target concrete-work call/output types are invalid")
    for field_name in ("call_id_sha256", "call_record_sha256", "output_record_sha256"):
        require_state_sha256(value.get(field_name), f"target concrete-work {field_name}")
    try:
        observed_at = dt.datetime.fromisoformat(str(value.get("observed_at")))
    except ValueError as exc:
        raise ContextError("target concrete-work timestamp is invalid") from exc
    if observed_at.tzinfo is None:
        raise ContextError("target concrete-work timestamp must include a timezone")


def concrete_call_record(payload: dict[str, Any]) -> bool:
    call_type = payload.get("type")
    call_id = payload.get("call_id")
    status = str(payload.get("status", "completed")).casefold()
    if (
        not isinstance(call_type, str)
        or call_type not in WORK_EVIDENCE_CALL_OUTPUT_TYPES
        or not isinstance(call_id, str)
        or not call_id
        or status not in {"completed", "success", "succeeded"}
    ):
        return False
    name = str(payload.get("name", "")).casefold()
    if name in {
        "create_goal",
        "get_goal",
        "request_user_input",
        "update_goal",
        "update_plan",
    }:
        return False
    serialized_input = json.dumps(
        payload.get("input"), ensure_ascii=False, sort_keys=True, default=str
    )
    nested_tools = {
        match.casefold()
        for match in re.findall(r"tools\.([A-Za-z0-9_]+)", serialized_input)
    }
    if nested_tools and nested_tools.issubset(
        {"create_goal", "get_goal", "request_user_input", "update_goal", "update_plan"}
    ):
        return False
    if re.search(r"(?i)contextctl\.py", serialized_input) and re.search(
        r"(?i)(?:^|[\s\"'])"
        r"(?:preflight|pulse|refresh|checkpoint|audit|finish)"
        r"(?:$|[\s\"'])",
        serialized_input,
    ):
        return False
    return True


def find_post_baseline_work_pair(
    task_id: str,
    rollout: Path,
    rollout_file_id: str,
    baseline_offset: int,
    observed_offset: int,
) -> dict[str, Any]:
    pending: dict[str, tuple[bytes, dict[str, Any]]] = {}
    try:
        with rollout.open("rb") as handle:
            handle.seek(baseline_offset)
            while handle.tell() < observed_offset:
                raw = handle.readline()
                if not raw or handle.tell() > observed_offset:
                    break
                try:
                    record = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    raise ContextError(
                        "target concrete-work rollout contains an invalid record"
                    ) from exc
                payload = record.get("payload") if isinstance(record, dict) else None
                if (
                    not isinstance(record, dict)
                    or record.get("type") != "response_item"
                    or not isinstance(payload, dict)
                ):
                    continue
                item_type = payload.get("type")
                call_id = payload.get("call_id")
                if not isinstance(item_type, str) or not isinstance(call_id, str):
                    continue
                if item_type in WORK_EVIDENCE_CALL_OUTPUT_TYPES and concrete_call_record(
                    payload
                ):
                    pending[call_id] = (raw, payload)
                    continue
                if call_id not in pending:
                    continue
                call_raw, call_payload = pending[call_id]
                if (
                    WORK_EVIDENCE_CALL_OUTPUT_TYPES.get(call_payload["type"])
                    != item_type
                ):
                    continue
                evidence = {
                    "schema_version": 1,
                    "task_id": task_id,
                    "rollout_file_id": rollout_file_id,
                    "baseline_offset": baseline_offset,
                    "observed_offset": handle.tell(),
                    "call_type": call_payload["type"],
                    "output_type": item_type,
                    "call_id_sha256": hashlib.sha256(call_id.encode("utf-8")).hexdigest(),
                    "call_record_sha256": hashlib.sha256(call_raw).hexdigest(),
                    "output_record_sha256": hashlib.sha256(raw).hexdigest(),
                    "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                }
                validate_work_evidence(evidence, task_id)
                return evidence
    except OSError as exc:
        raise ContextError("target concrete-work rollout is unreadable") from exc
    raise ContextError("target has no completed concrete tool call after preflight")


def observe_runtime_work(
    runtime: dict[str, Any],
    task_id: str,
    runtime_file: Path,
) -> dict[str, Any]:
    """Refresh Codex rollout telemetry and prove post-preflight tool activity."""

    telemetry = refresh_codex_telemetry(task_id, runtime.get("codex_telemetry"))
    validate_codex_telemetry(telemetry)
    runtime["codex_telemetry"] = telemetry
    if telemetry.get("available") is not True or telemetry.get("parse_errors") != 0:
        raise ContextError("target concrete-work telemetry is unavailable or unreliable")
    baseline_offset = runtime.get("started_rollout_offset")
    baseline_file_id = runtime.get("started_rollout_file_id")
    if (
        not isinstance(baseline_offset, int)
        or isinstance(baseline_offset, bool)
        or baseline_offset < 0
        or not isinstance(baseline_file_id, str)
        or not baseline_file_id
    ):
        runtime["started_rollout_offset"] = int(telemetry.get("rollout_offset", 0))
        runtime["started_rollout_file_id"] = telemetry.get("rollout_file_id")
        runtime["started_tool_calls"] = int(telemetry.get("tool_calls", 0))
        runtime.pop("work_evidence", None)
        atomic_write(
            runtime_file,
            json.dumps(runtime, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
        )
        raise ContextError(
            "target runtime lacked a trusted work baseline; it was rebaselined, "
            "so perform one more concrete tool action"
        )
    current_file_id = telemetry.get("rollout_file_id")
    current_offset = int(telemetry.get("rollout_offset", 0))
    if current_file_id != baseline_file_id or current_offset < baseline_offset:
        runtime["started_rollout_offset"] = current_offset
        runtime["started_rollout_file_id"] = current_file_id
        runtime["started_tool_calls"] = int(telemetry.get("tool_calls", 0))
        runtime.pop("work_evidence", None)
        atomic_write(
            runtime_file,
            json.dumps(runtime, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        raise ContextError(
            "target concrete-work rollout changed after preflight; it was "
            "rebaselined, so perform one more concrete tool action"
        )
    rollout = find_codex_rollout(task_id, telemetry.get("rollout_path"))
    if rollout is None:
        raise ContextError("target concrete-work rollout is unavailable")
    evidence = find_post_baseline_work_pair(
        task_id,
        rollout,
        baseline_file_id,
        baseline_offset,
        current_offset,
    )
    runtime["work_evidence"] = evidence
    runtime["last_seen_at"] = evidence["observed_at"]
    return evidence


def validate_runtime_identity(
    runtime: dict[str, Any],
    project_id: str,
    task_id: str,
) -> None:
    if runtime.get("project_id") != project_id:
        raise ContextError("runtime session project mismatch")
    if runtime.get("task_id") != task_id:
        raise ContextError("runtime session task mismatch")


def empty_codex_telemetry() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "available": False,
        "token_count_seen": False,
        "rollout_offset": 0,
        "outer_compactions": 0,
        "event_compactions": 0,
        "compactions": 0,
        "latest_input_tokens": 0,
        "context_window": 0,
        "tool_calls": 0,
        "tool_output_chars": 0,
        "max_tool_output_chars": 0,
        "truncated_tool_outputs": 0,
        "parse_errors": 0,
    }


def normalize_codex_telemetry(previous: Any) -> dict[str, Any]:
    telemetry = empty_codex_telemetry()
    if not isinstance(previous, dict):
        return telemetry
    for field in (
        "rollout_offset",
        "outer_compactions",
        "event_compactions",
        "latest_input_tokens",
        "context_window",
        "tool_calls",
        "tool_output_chars",
        "max_tool_output_chars",
        "truncated_tool_outputs",
        "parse_errors",
    ):
        value = previous.get(field)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            telemetry[field] = value
    rollout = previous.get("rollout_path")
    if isinstance(rollout, str) and rollout:
        telemetry["rollout_path"] = rollout
    telemetry["available"] = bool(previous.get("available"))
    telemetry["token_count_seen"] = bool(previous.get("token_count_seen")) or bool(
        telemetry["latest_input_tokens"]
    )
    rollout_file_id = previous.get("rollout_file_id")
    if isinstance(rollout_file_id, str) and rollout_file_id:
        telemetry["rollout_file_id"] = rollout_file_id
    telemetry["compactions"] = max(
        int(telemetry["outer_compactions"]),
        int(telemetry["event_compactions"]),
    )
    return telemetry


def codex_home_root() -> Path:
    configured = os.environ.get("CODEX_HOME")
    codex_home = Path(configured) if configured else Path.home() / ".codex"
    return codex_home.expanduser().resolve()


def codex_sessions_root() -> Path:
    return codex_home_root() / "sessions"


def path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except (OSError, ValueError):
        return False
    return True


def find_codex_rollout(task_id: str, cached: Any = None) -> Path | None:
    sessions_root = codex_sessions_root()
    expected_suffix = f"-{task_id}.jsonl"
    if isinstance(cached, str) and cached:
        cached_path = Path(cached)
        if not cached_path.is_absolute():
            cached_path = codex_home_root() / cached_path
        if (
            cached_path.name.endswith(expected_suffix)
            and path_is_within(cached_path, sessions_root)
            and cached_path.is_file()
        ):
            return cached_path.resolve()
    if not sessions_root.is_dir():
        return None
    matches = [
        path
        for path in sessions_root.rglob(f"*{expected_suffix}")
        if path.is_file() and path.name.endswith(expected_suffix)
    ]
    if not matches:
        return None
    try:
        return max(matches, key=lambda path: path.stat().st_mtime_ns).resolve()
    except OSError:
        return matches[-1].resolve()


def iter_tool_output_text(value: Any, parent_key: str | None = None):
    if parent_key in OPAQUE_OUTPUT_KEYS:
        return
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, list):
        for item in value:
            yield from iter_tool_output_text(item)
        return
    if isinstance(value, dict):
        block_type = value.get("type")
        mime_type = value.get("mimeType", value.get("mime_type"))
        binary_block = (
            isinstance(block_type, str)
            and block_type.casefold() in BINARY_OUTPUT_TYPES
        ) or (
            isinstance(mime_type, str)
            and mime_type.casefold().startswith(("image/", "audio/"))
        )
        for key, item in value.items():
            if isinstance(key, str):
                if key == "data" and binary_block:
                    continue
                yield from iter_tool_output_text(item, key)


def tool_output_metrics(value: Any) -> tuple[int, bool]:
    total = 0
    truncated = False
    for text in iter_tool_output_text(value):
        total += len(text)
        if not truncated and TRUNCATION_MARKER.search(text):
            truncated = True
    return total, truncated


def update_codex_telemetry_from_record(
    telemetry: dict[str, Any],
    raw: bytes,
) -> None:
    head = raw[:1024]
    top_level_match = TOP_LEVEL_TYPE.match(head)
    if top_level_match and top_level_match.group(1) == b"compacted":
        telemetry["outer_compactions"] += 1
        return
    relevant_fragments = (
        b'"type":"session_meta"',
        b'"type": "session_meta"',
        b'"type":"token_count"',
        b'"type": "token_count"',
        b'"type":"context_compacted"',
        b'"type": "context_compacted"',
        b'_call"',
        b'_output"',
    )
    if not any(fragment in head for fragment in relevant_fragments):
        return
    record = json.loads(raw)
    if not isinstance(record, dict):
        return
    outer_type = record.get("type")
    payload = record.get("payload")
    if outer_type == "compacted":
        telemetry["outer_compactions"] += 1
        return
    if not isinstance(payload, dict):
        return
    inner_type = payload.get("type")
    if outer_type == "event_msg" and inner_type == "context_compacted":
        telemetry["event_compactions"] += 1
        return
    if outer_type == "session_meta":
        context_window = payload.get("context_window")
        if isinstance(context_window, int) and not isinstance(context_window, bool):
            telemetry["context_window"] = max(0, context_window)
        return
    if outer_type == "event_msg" and inner_type == "token_count":
        info = payload.get("info")
        if not isinstance(info, dict):
            return
        last_usage = info.get("last_token_usage")
        if isinstance(last_usage, dict):
            input_tokens = last_usage.get("input_tokens")
            if isinstance(input_tokens, int) and not isinstance(input_tokens, bool):
                telemetry["latest_input_tokens"] = max(0, input_tokens)
                telemetry["token_count_seen"] = True
        context_window = info.get("model_context_window")
        if isinstance(context_window, int) and not isinstance(context_window, bool):
            telemetry["context_window"] = max(0, context_window)
        return
    if outer_type != "response_item" or not isinstance(inner_type, str):
        return
    if inner_type.endswith("_call"):
        telemetry["tool_calls"] += 1
        return
    if not inner_type.endswith("_output"):
        return
    if "output" in payload:
        output = payload["output"]
    elif inner_type == "tool_search_output":
        output = payload.get("tools")
    else:
        output = payload.get("content", payload.get("result"))
    output_chars, truncated = tool_output_metrics(output)
    telemetry["tool_output_chars"] += output_chars
    telemetry["max_tool_output_chars"] = max(
        int(telemetry["max_tool_output_chars"]), output_chars
    )
    if truncated:
        telemetry["truncated_tool_outputs"] += 1


def refresh_codex_telemetry(task_id: str, previous: Any = None) -> dict[str, Any]:
    telemetry = normalize_codex_telemetry(previous)
    lifetime_floor: dict[str, Any] | None = None
    rollout = find_codex_rollout(task_id, telemetry.get("rollout_path"))
    if rollout is None:
        telemetry["available"] = False
        return telemetry
    try:
        rollout_stat = rollout.stat()
        size = rollout_stat.st_size
    except OSError:
        telemetry["available"] = False
        return telemetry
    cached_path = telemetry.get("rollout_path")
    try:
        stored_rollout_path = str(rollout.relative_to(codex_home_root()))
    except ValueError:
        stored_rollout_path = str(rollout)
    rollout_file_id = f"{rollout_stat.st_dev}:{rollout_stat.st_ino}"
    cached_file_id = telemetry.get("rollout_file_id")
    offset = int(telemetry["rollout_offset"])
    if (
        cached_path != stored_rollout_path
        or offset > size
        or (
            isinstance(cached_file_id, str)
            and cached_file_id
            and cached_file_id != rollout_file_id
        )
    ):
        lifetime_floor = telemetry
        telemetry = empty_codex_telemetry()
        offset = 0
    telemetry["available"] = True
    telemetry["rollout_path"] = stored_rollout_path
    telemetry["rollout_file_id"] = rollout_file_id
    try:
        with rollout.open("rb") as handle:
            handle.seek(offset)
            while True:
                line_start = handle.tell()
                raw = handle.readline()
                if not raw:
                    break
                if not raw.endswith(b"\n"):
                    handle.seek(line_start)
                    break
                try:
                    update_codex_telemetry_from_record(telemetry, raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    telemetry["parse_errors"] += 1
                offset = handle.tell()
    except OSError:
        telemetry["available"] = False
        return telemetry
    telemetry["rollout_offset"] = offset
    if lifetime_floor is not None:
        # Rollout rotation/replacement is not a fresh model thread. Preserve a
        # monotonic lifetime floor without double-counting a rewritten file
        # that may contain the same history from byte zero.
        for field in (
            "outer_compactions",
            "event_compactions",
            "tool_calls",
            "tool_output_chars",
            "max_tool_output_chars",
            "truncated_tool_outputs",
            "parse_errors",
        ):
            telemetry[field] = max(
                int(telemetry.get(field, 0)),
                int(lifetime_floor.get(field, 0)),
            )
    telemetry["compactions"] = max(
        int(telemetry["outer_compactions"]),
        int(telemetry["event_compactions"]),
    )
    telemetry["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    return telemetry


def context_fill_percent(telemetry: dict[str, Any]) -> float | None:
    input_tokens = telemetry.get("latest_input_tokens")
    context_window = telemetry.get("context_window")
    if (
        not telemetry.get("token_count_seen")
        or not isinstance(input_tokens, int)
        or not isinstance(context_window, int)
        or context_window <= 0
    ):
        return None
    return input_tokens * 100.0 / context_window


def telemetry_budget_reasons(
    registry: dict[str, Any], telemetry: dict[str, Any]
) -> list[str]:
    reasons: list[str] = []
    compactions = int(telemetry.get("compactions", 0))
    compaction_limit = int(
        registry.get("max_session_compactions", DEFAULT_MAX_SESSION_COMPACTIONS)
    )
    if compactions > 0 and compactions >= compaction_limit:
        reasons.append("compacted")
    fill = context_fill_percent(telemetry)
    if fill is not None and fill >= int(
        registry.get(
            "max_context_fill_percent", DEFAULT_MAX_CONTEXT_FILL_PERCENT
        )
    ):
        reasons.append("context_fill")
    if int(telemetry.get("tool_output_chars", 0)) >= int(
        registry.get("max_tool_output_chars", DEFAULT_MAX_TOOL_OUTPUT_CHARS)
    ):
        reasons.append("tool_output_chars")
    if int(telemetry.get("max_tool_output_chars", 0)) >= int(
        registry.get(
            "max_single_tool_output_chars",
            DEFAULT_MAX_SINGLE_TOOL_OUTPUT_CHARS,
        )
    ):
        reasons.append("single_tool_output_chars")
    if int(telemetry.get("tool_calls", 0)) >= int(
        registry.get("max_tool_calls", DEFAULT_MAX_TOOL_CALLS)
    ):
        reasons.append("tool_calls")
    return reasons


def telemetry_summary(telemetry: dict[str, Any]) -> str:
    fill = context_fill_percent(telemetry)
    fill_text = "unknown" if fill is None else f"{fill:.1f}%"
    availability = "available" if telemetry.get("available") else "unavailable"
    return (
        f"status={availability},compactions={telemetry.get('compactions', 0)},"
        f"context_fill={fill_text},tool_calls={telemetry.get('tool_calls', 0)},"
        f"tool_output_chars={telemetry.get('tool_output_chars', 0)},"
        f"max_tool_output_chars={telemetry.get('max_tool_output_chars', 0)},"
        f"truncated_outputs={telemetry.get('truncated_tool_outputs', 0)},"
        f"parse_errors={telemetry.get('parse_errors', 0)}"
    )


def validate_codex_telemetry(telemetry: dict[str, Any]) -> None:
    parse_errors = telemetry.get("parse_errors", 0)
    if isinstance(parse_errors, int) and parse_errors > 0:
        raise ContextError(
            "Codex rollout telemetry is unreliable: "
            f"{parse_errors} malformed relevant JSONL record(s)"
        )


def start_runtime_session(
    paths: dict[str, Path],
    project_id: str,
    task_id: str,
    base_sha256: str,
    checkpoint_after: int,
    started_rules_fingerprint_sha256: str,
    replaces_task_id: str | None = None,
    expected_source_audit_fingerprint_sha256: str | None = None,
) -> tuple[dict[str, Any], bool]:
    started_rules_fingerprint_sha256 = require_state_sha256(
        started_rules_fingerprint_sha256,
        "started rules fingerprint",
    )
    if replaces_task_id == task_id:
        raise ContextError("replacement source task must differ from target task")
    if replaces_task_id is None:
        if expected_source_audit_fingerprint_sha256 is not None:
            raise ContextError("source audit fingerprint requires a replacement source")
    else:
        expected_source_audit_fingerprint_sha256 = require_state_sha256(
            expected_source_audit_fingerprint_sha256,
            "expected source audit fingerprint",
        )
    session_path = runtime_path(paths, task_id)
    receipt_path = completion_receipt_path(paths, task_id)
    source_path = (
        runtime_path(paths, replaces_task_id)
        if replaces_task_id is not None
        else None
    )
    state_lock = paths["state"].parent / "checkpoint.lock"
    runtime_locks = {session_path.with_suffix(".lock")}
    if source_path is not None:
        runtime_locks.add(source_path.with_suffix(".lock"))
    with checkpoint_lock(state_lock):
        with contextlib.ExitStack() as stack:
            for runtime_lock in sorted(runtime_locks, key=lambda item: str(item)):
                stack.enter_context(checkpoint_lock(runtime_lock))
            locked_state = read_json(paths["state"])
            validate_state(locked_state, project_id)
            if state_sha256(locked_state) != base_sha256:
                raise ContextError("state changed during preflight; retry preflight")
            current: dict[str, Any] | None = None
            if session_path.is_file():
                current = read_json(session_path)
                validate_runtime_identity(current, project_id, task_id)
            completed_receipt: dict[str, Any] | None = None
            if current is None and receipt_path.is_file():
                completed_receipt = read_json(receipt_path)
                validate_lifecycle_receipt(
                    completed_receipt,
                    project_id,
                    task_id,
                    expected_kind="completed",
                    expected_replacement_task_id=None,
                )
                receipt_source_task_id = completed_receipt.get("source_task_id")
                if receipt_source_task_id is None:
                    if replaces_task_id is not None:
                        raise ContextError(
                            "source-free completed receipt cannot be rebound to a "
                            "replacement source"
                        )
                elif receipt_source_task_id != replaces_task_id:
                    if replaces_task_id is None:
                        raise ContextError(
                            "source-linked completed receipt requires "
                            f"--replaces-task {receipt_source_task_id}"
                        )
                    raise ContextError(
                        "completed receipt is bound to a different source task"
                    )
            previous_telemetry: Any = None
            if current is not None:
                previous_telemetry = current.get("codex_telemetry")
            telemetry = refresh_codex_telemetry(task_id, previous_telemetry)
            created = current is None or current.get("base_sha256") != base_sha256
            if created:
                lineage: dict[str, Any] = {}
                started_state_sha256 = base_sha256
                runtime_started_rules_fingerprint = (
                    started_rules_fingerprint_sha256
                )
                if completed_receipt is not None:
                    started_state_sha256 = require_state_sha256(
                        completed_receipt.get("started_state_sha256"),
                        "completed receipt started_state_sha256",
                    )
                    runtime_started_rules_fingerprint = require_state_sha256(
                        completed_receipt.get("started_rules_fingerprint_sha256"),
                        "completed receipt started_rules_fingerprint_sha256",
                    )
                    if replaces_task_id is not None:
                        lineage = {
                            "source_task_id": replaces_task_id,
                            "source_audited_state_sha256": require_state_sha256(
                                completed_receipt.get(
                                    "source_audited_state_sha256"
                                ),
                                "completed receipt source_audited_state_sha256",
                            ),
                            "source_audit_rules_fingerprint_sha256": (
                                require_state_sha256(
                                    completed_receipt.get(
                                        "source_audit_rules_fingerprint_sha256"
                                    ),
                                    "completed receipt source audit rules fingerprint",
                                )
                            ),
                            "source_audit_fingerprint_sha256": require_state_sha256(
                                completed_receipt.get(
                                    "source_audit_fingerprint_sha256"
                                ),
                                "completed receipt source audit fingerprint",
                            ),
                        }
                elif replaces_task_id is not None:
                    assert source_path is not None
                    if not source_path.is_file():
                        raise ContextError(
                            "replacement source runtime is missing; "
                            "cannot bind target lineage"
                        )
                    source = read_json(source_path)
                    validate_runtime_identity(source, project_id, replaces_task_id)
                    source_state_sha256 = require_state_sha256(
                        source.get("base_sha256"),
                        "replacement source runtime base_sha256",
                    )
                    source_audited_state_sha256 = require_state_sha256(
                        source.get("audited_state_sha256"),
                        "replacement source audited_state_sha256",
                    )
                    source_rules_fingerprint_sha256 = require_state_sha256(
                        source.get("audit_rules_fingerprint_sha256"),
                        "replacement source audit rules fingerprint",
                    )
                    source_audit_fingerprint_sha256 = require_state_sha256(
                        source.get("audit_fingerprint_sha256"),
                        "replacement source audit fingerprint",
                    )
                    if source_state_sha256 != base_sha256:
                        raise ContextError(
                            "replacement source state does not match target start"
                        )
                    if source_audited_state_sha256 != source_state_sha256:
                        raise ContextError(
                            "replacement source task-bound audit is missing or stale"
                        )
                    if (
                        source_rules_fingerprint_sha256
                        != started_rules_fingerprint_sha256
                    ):
                        raise ContextError(
                            "rules changed since the replacement source audit; "
                            "rerun the source task-bound audit"
                        )
                    if (
                        source_audit_fingerprint_sha256
                        != expected_source_audit_fingerprint_sha256
                    ):
                        raise ContextError(
                            "replacement source audit fingerprint is stale"
                        )
                    lineage = {
                        "source_task_id": replaces_task_id,
                        "source_audited_state_sha256": (
                            source_audited_state_sha256
                        ),
                        "source_audit_rules_fingerprint_sha256": (
                            source_rules_fingerprint_sha256
                        ),
                        "source_audit_fingerprint_sha256": (
                            source_audit_fingerprint_sha256
                        ),
                    }
                current = {
                    "schema_version": 1,
                    "project_id": project_id,
                    "task_id": task_id,
                    "base_sha256": base_sha256,
                    "started_state_sha256": started_state_sha256,
                    "started_rules_fingerprint_sha256": (
                        runtime_started_rules_fingerprint
                    ),
                    "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "substantive_iterations": 0,
                    "checkpoint_after": checkpoint_after,
                    "started_tool_calls": int(telemetry.get("tool_calls", 0)),
                    "started_rollout_offset": int(telemetry.get("rollout_offset", 0)),
                    "started_rollout_file_id": telemetry.get("rollout_file_id"),
                    **lineage,
                }
            else:
                current_started_state_sha256 = require_state_sha256(
                    current.get("started_state_sha256"),
                    "runtime started_state_sha256",
                )
                current_started_rules_fingerprint_sha256 = require_state_sha256(
                    current.get("started_rules_fingerprint_sha256"),
                    "runtime started_rules_fingerprint_sha256",
                )
                if replaces_task_id is not None:
                    if current.get("source_task_id") != replaces_task_id:
                        raise ContextError(
                            "target runtime is bound to a different source task"
                        )
                    current_source_state_sha256 = require_state_sha256(
                        current.get("source_audited_state_sha256"),
                        "runtime source_audited_state_sha256",
                    )
                    current_source_rules_fingerprint_sha256 = require_state_sha256(
                        current.get("source_audit_rules_fingerprint_sha256"),
                        "runtime source_audit_rules_fingerprint_sha256",
                    )
                    require_state_sha256(
                        current.get("source_audit_fingerprint_sha256"),
                        "runtime source_audit_fingerprint_sha256",
                    )
                    if current_started_state_sha256 != current_source_state_sha256:
                        raise ContextError(
                            "target runtime start state does not match its source audit"
                        )
                    if (
                        current_started_rules_fingerprint_sha256
                        != current_source_rules_fingerprint_sha256
                    ):
                        raise ContextError(
                            "target runtime start rules do not match its source audit"
                        )
            current["checkpoint_after"] = checkpoint_after
            if (
                "started_rollout_offset" not in current
                or "started_rollout_file_id" not in current
            ):
                current["started_tool_calls"] = int(telemetry.get("tool_calls", 0))
                current["started_rollout_offset"] = int(
                    telemetry.get("rollout_offset", 0)
                )
                current["started_rollout_file_id"] = telemetry.get("rollout_file_id")
                current.pop("work_evidence", None)
            current["codex_telemetry"] = telemetry
            current["last_seen_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
            atomic_write(
                session_path,
                json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n",
            )
            if completed_receipt is not None:
                receipt_path.unlink(missing_ok=True)
            return current, created


def pulse_runtime_session(
    paths: dict[str, Path],
    project_id: str,
    task_id: str,
    base_sha256: str,
    count: int,
) -> dict[str, Any]:
    session_path = runtime_path(paths, task_id)
    lock_path = session_path.with_suffix(".lock")
    with checkpoint_lock(lock_path):
        if not session_path.is_file():
            raise ContextError("runtime session is missing; run preflight first")
        current = read_json(session_path)
        validate_runtime_identity(current, project_id, task_id)
        if current.get("base_sha256") != base_sha256:
            raise ContextError(
                "runtime session state changed; "
                f"run refresh --project {project_id}"
            )
        iterations = current.get("substantive_iterations")
        checkpoint_after = current.get("checkpoint_after")
        if not isinstance(iterations, int) or not isinstance(checkpoint_after, int):
            raise ContextError("runtime session is invalid")
        current["substantive_iterations"] = iterations + count
        refreshed_telemetry = refresh_codex_telemetry(
            task_id,
            current.get("codex_telemetry"),
        )
        if (
            "started_rollout_offset" not in current
            or "started_rollout_file_id" not in current
        ):
            current["started_tool_calls"] = int(
                refreshed_telemetry.get("tool_calls", 0)
            )
            current["started_rollout_offset"] = int(
                refreshed_telemetry.get("rollout_offset", 0)
            )
            current["started_rollout_file_id"] = refreshed_telemetry.get(
                "rollout_file_id"
            )
            current.pop("work_evidence", None)
        current["codex_telemetry"] = refreshed_telemetry
        current["last_seen_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        atomic_write(
            session_path,
            json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        return current


def refresh_runtime_state(
    root: Path,
    paths: dict[str, Path],
    project: dict[str, Any],
    task_id: str,
    checkpoint_after: int,
) -> tuple[str, str]:
    project_id = str(project["id"])
    session_path = runtime_path(paths, task_id)
    if not session_path.is_file():
        raise ContextError("runtime session is missing; run preflight first")
    lock_path = session_path.with_suffix(".lock")
    with checkpoint_lock(lock_path):
        if not session_path.is_file():
            raise ContextError("runtime session is missing; run preflight first")
        current = read_json(session_path)
        validate_runtime_identity(current, project_id, task_id)
        iterations = current.get("substantive_iterations")
        current_checkpoint_after = current.get("checkpoint_after")
        if (
            not isinstance(iterations, int)
            or isinstance(iterations, bool)
            or not isinstance(current_checkpoint_after, int)
            or isinstance(current_checkpoint_after, bool)
        ):
            raise ContextError("runtime session is invalid")
        old_sha256 = current.get("base_sha256")
        if not isinstance(old_sha256, str) or not old_sha256:
            raise ContextError("runtime session is invalid")

        state = read_json(paths["state"])
        validate_state(state, project_id)
        validate_project_state(state, project, root)
        new_sha256 = state_sha256(state)

        current["base_sha256"] = new_sha256
        current.pop("audited_state_sha256", None)
        current.pop("audit_fingerprint_sha256", None)
        current.pop("audit_rules_fingerprint_sha256", None)
        current.pop("audited_at", None)
        current["checkpoint_after"] = checkpoint_after
        current["last_seen_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        atomic_write(
            session_path,
            json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        return old_sha256, new_sha256


def reset_runtime_session(
    paths: dict[str, Path],
    project_id: str,
    task_id: str | None,
    base_sha256: str,
    checkpoint_after: int,
) -> None:
    if task_id is None:
        return
    session_path = runtime_path(paths, task_id)
    lock_path = session_path.with_suffix(".lock")
    with checkpoint_lock(lock_path):
        if not session_path.is_file():
            return
        current = read_json(session_path)
        validate_runtime_identity(current, project_id, task_id)
        current["base_sha256"] = base_sha256
        current.pop("audited_state_sha256", None)
        current.pop("audit_fingerprint_sha256", None)
        current.pop("audit_rules_fingerprint_sha256", None)
        current.pop("audited_at", None)
        current["substantive_iterations"] = 0
        current["checkpoint_after"] = checkpoint_after
        # A checkpoint updates the bounded handoff, not the model transcript.
        # Preserve lifetime telemetry so repeated checkpoints cannot hide a
        # degrading thread. A genuinely fresh thread gets a new task id and a
        # new runtime record.
        current["codex_telemetry"] = normalize_codex_telemetry(
            current.get("codex_telemetry")
        )
        current["last_seen_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        atomic_write(
            session_path,
            json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )


def audit_one(root: Path, registry: dict[str, Any], project: dict[str, Any]) -> dict[str, Any]:
    paths = project_paths(root, project)
    issues: list[str] = []
    try:
        state = read_json(paths["state"])
        validate_state(state, str(project["id"]))
        validate_project_state(state, project, root)
        rendered = render_state(state)
        if not paths["active"].is_file():
            issues.append("missing ACTIVE_STATE.md")
        elif paths["active"].read_text(encoding="utf-8") != rendered:
            issues.append("ACTIVE_STATE drift")
    except ContextError as exc:
        issues.append(str(exc))

    if not paths["agent"].is_file():
        issues.append("missing AGENTS.md")
        agent_size = 0
    else:
        agent_size = paths["agent"].stat().st_size
        if agent_size > AGENT_BYTE_LIMIT:
            issues.append(f"AGENTS>{AGENT_BYTE_LIMIT} bytes")
        if "ACTIVE_STATE.md" not in paths["agent"].read_text(encoding="utf-8"):
            issues.append("AGENTS missing ACTIVE_STATE contract")

    root_agent = root / "AGENTS.md"
    if not root_agent.is_file():
        issues.append("missing root AGENTS.md")
    else:
        root_rules = root_agent.read_text(encoding="utf-8").casefold()
        rollover_terms = (
            "context_rollover_required",
            "checkpoint",
            "fresh thread",
        )
        if not repo_hooks_disabled(root) and not all(
            term in root_rules for term in rollover_terms
        ):
            issues.append("root AGENTS missing fresh-thread rollover contract")

    try:
        bundle_size = len(build_bundle(root, registry, project).encode("utf-8"))
    except (ContextError, FileNotFoundError) as exc:
        issues.append(f"preflight: {exc}")
        bundle_size = 0

    return {
        "id": project["id"],
        "name": project["name"],
        "agent_bytes": agent_size,
        "active_bytes": paths["active"].stat().st_size if paths["active"].is_file() else 0,
        "bundle_bytes": bundle_size,
        "status": "PASS" if not issues else "FAIL",
        "issues": issues,
    }


def print_audit(rows: list[dict[str, Any]]) -> None:
    headers = ("PROJECT", "AGENT", "ACTIVE", "BUNDLE", "STATUS", "ISSUES")
    formatted = [
        (
            str(row["id"]),
            str(row["agent_bytes"]),
            str(row["active_bytes"]),
            str(row["bundle_bytes"]),
            str(row["status"]),
            "; ".join(row["issues"]),
        )
        for row in rows
    ]
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in formatted))
        for index in range(len(headers))
    ]
    print("  ".join(headers[index].ljust(widths[index]) for index in range(len(headers))))
    print("  ".join("-" * width for width in widths))
    for row in formatted:
        print("  ".join(row[index].ljust(widths[index]) for index in range(len(headers))))
    passed = sum(row["status"] == "PASS" for row in rows)
    print(f"\nContext audit: {passed}/{len(rows)} projects pass.")


def cmd_render(root: Path, registry: dict[str, Any], selector: str | None) -> int:
    projects = (
        [resolve_project(root, registry, selector)]
        if selector
        else list(registry["projects"])
    )
    for project in projects:
        paths = project_paths(root, project)
        state = read_json(paths["state"])
        validate_state(state, str(project["id"]))
        validate_project_state(state, project, root)
        atomic_write(paths["active"], render_state(state))
        print(f"rendered {project['id']}: {paths['active']}")
    return 0


def root_audit_issues(root: Path) -> list[str]:
    root_agent = root / "AGENTS.md"
    if not root_agent.is_file():
        return ["missing root AGENTS.md"]
    issues: list[str] = []
    if root_agent.stat().st_size > ROOT_AGENT_BYTE_LIMIT:
        issues.append(f"root AGENTS>{ROOT_AGENT_BYTE_LIMIT} bytes")
    if "context-guardian" not in root_agent.read_text(encoding="utf-8"):
        issues.append("root AGENTS missing context-guardian contract")
    return issues


def audit_rules_fingerprint(
    root: Path,
    project: dict[str, Any],
) -> str:
    """Hash stable rule inputs that must remain valid across target progress."""
    paths = project_paths(root, project)
    try:
        inputs = {
            "schema_version": 1,
            "project_id": str(project["id"]),
            "root_agent_sha256": file_sha256(root / "AGENTS.md"),
            "project_agent_sha256": file_sha256(paths["agent"]),
            "registry_sha256": file_sha256(root / ".context" / "registry.json"),
        }
    except OSError as exc:
        raise ContextError(f"audit fingerprint input is unavailable: {exc}") from exc
    return hashlib.sha256(canonical_state_bytes(inputs)).hexdigest()


def audit_fingerprint(
    root: Path,
    registry: dict[str, Any],
    project: dict[str, Any],
    state_sha: str,
) -> str:
    """Bind a task audit to state/view and the stable rule inputs."""
    del registry
    paths = project_paths(root, project)
    try:
        inputs = {
            "schema_version": 1,
            "project_id": str(project["id"]),
            "state_sha256": require_state_sha256(state_sha, "audited state_sha256"),
            "active_sha256": file_sha256(paths["active"]),
            "rules_fingerprint_sha256": audit_rules_fingerprint(root, project),
        }
    except OSError as exc:
        raise ContextError(f"audit fingerprint input is unavailable: {exc}") from exc
    return hashlib.sha256(canonical_state_bytes(inputs)).hexdigest()


def cmd_audit(
    root: Path,
    registry: dict[str, Any],
    selector: str | None,
    task_id_arg: str | None = None,
) -> int:
    root_issues = root_audit_issues(root)

    projects = (
        [resolve_project(root, registry, selector)]
        if selector
        else list(registry["projects"])
    )
    rows = [audit_one(root, registry, project) for project in projects]
    if root_issues:
        for row in rows:
            row["status"] = "FAIL"
            row["issues"].extend(root_issues)
    print_audit(rows)
    passed = all(row["status"] == "PASS" for row in rows)
    if passed and task_id_arg is not None:
        if len(projects) != 1:
            raise ContextError("task-bound audit requires exactly one project")
        task_id = resolve_task_id(task_id_arg)
        if task_id is None:
            raise ContextError("task id unavailable; pass --task-id")
        project = projects[0]
        paths = project_paths(root, project)
        session_path = runtime_path(paths, task_id)
        state_lock = paths["state"].parent / "checkpoint.lock"
        runtime_lock = session_path.with_suffix(".lock")
        with checkpoint_lock(state_lock), checkpoint_lock(runtime_lock):
            locked_row = audit_one(root, registry, project)
            if locked_row["status"] != "PASS":
                raise ContextError(
                    "task-bound audit changed while recording receipt: "
                    + "; ".join(locked_row["issues"])
                )
            if not session_path.is_file():
                raise ContextError("runtime session is missing; run preflight first")
            runtime = read_json(session_path)
            validate_runtime_identity(runtime, str(project["id"]), task_id)
            state = read_json(paths["state"])
            current_sha256 = state_sha256(state)
            if runtime.get("base_sha256") != current_sha256:
                raise ContextError("runtime session state changed; run refresh first")
            recorded_audit_fingerprint = audit_fingerprint(
                root,
                registry,
                project,
                current_sha256,
            )
            recorded_rules_fingerprint = audit_rules_fingerprint(root, project)
            runtime["audited_state_sha256"] = current_sha256
            runtime["audit_fingerprint_sha256"] = recorded_audit_fingerprint
            runtime["audit_rules_fingerprint_sha256"] = recorded_rules_fingerprint
            source_lineage_fields = (
                "source_task_id",
                "source_audited_state_sha256",
                "source_audit_rules_fingerprint_sha256",
                "source_audit_fingerprint_sha256",
            )
            has_source_lineage = any(
                runtime.get(field) is not None for field in source_lineage_fields
            )
            if has_source_lineage:
                source_task_id = runtime.get("source_task_id")
                if (
                    not isinstance(source_task_id, str)
                    or re.fullmatch(r"[A-Za-z0-9._-]{1,128}", source_task_id)
                    is None
                    or source_task_id == task_id
                ):
                    raise ContextError("runtime source task lineage is invalid")
                source_state_sha256 = require_state_sha256(
                    runtime.get("source_audited_state_sha256"),
                    "runtime source_audited_state_sha256",
                )
                source_rules_fingerprint_sha256 = require_state_sha256(
                    runtime.get("source_audit_rules_fingerprint_sha256"),
                    "runtime source_audit_rules_fingerprint_sha256",
                )
                require_state_sha256(
                    runtime.get("source_audit_fingerprint_sha256"),
                    "runtime source_audit_fingerprint_sha256",
                )
                if require_state_sha256(
                    runtime.get("started_state_sha256"),
                    "runtime started_state_sha256",
                ) != source_state_sha256:
                    raise ContextError(
                        "runtime start state does not match its source audit"
                    )
                if require_state_sha256(
                    runtime.get("started_rules_fingerprint_sha256"),
                    "runtime started_rules_fingerprint_sha256",
                ) != source_rules_fingerprint_sha256:
                    raise ContextError(
                        "runtime start rules do not match its source audit"
                    )
            else:
                migrated = False
                if "started_state_sha256" not in runtime:
                    runtime["started_state_sha256"] = current_sha256
                    migrated = True
                else:
                    require_state_sha256(
                        runtime.get("started_state_sha256"),
                        "runtime started_state_sha256",
                    )
                if "started_rules_fingerprint_sha256" not in runtime:
                    runtime["started_rules_fingerprint_sha256"] = (
                        recorded_rules_fingerprint
                    )
                    migrated = True
                else:
                    require_state_sha256(
                        runtime.get("started_rules_fingerprint_sha256"),
                        "runtime started_rules_fingerprint_sha256",
                    )
                if migrated:
                    runtime["legacy_lineage_migrated_at_audit"] = (
                        dt.datetime.now(dt.timezone.utc).isoformat()
                    )
            runtime["audited_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
            atomic_write(
                session_path,
                json.dumps(runtime, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            )
        print(f"task audit recorded: {project['id']} task={task_id}")
    return 0 if passed else 1


def cmd_preflight(
    root: Path,
    registry: dict[str, Any],
    selector: str,
    output: str | None,
    full_rules: bool,
    task_id_arg: str | None,
    no_session: bool,
    resume_state: bool,
    replaces_task_id_arg: str | None = None,
) -> int:
    project = resolve_project(root, registry, selector)
    row = audit_one(root, registry, project)
    if row["status"] != "PASS":
        raise ContextError(f"project audit failed: {'; '.join(row['issues'])}")
    runtime: dict[str, Any] | None = None
    created = False
    replaces_task_id = (
        resolve_task_id(replaces_task_id_arg)
        if replaces_task_id_arg is not None
        else None
    )
    if no_session and replaces_task_id is not None:
        raise ContextError("--replaces-task requires a runtime session")
    if not no_session:
        task_id = resolve_task_id(task_id_arg)
        if task_id is None and replaces_task_id is not None:
            raise ContextError("--replaces-task requires --task-id")
        if task_id is not None:
            paths = project_paths(root, project)
            state = read_json(paths["state"])
            base_sha256 = state_sha256(state)
            checkpoint_after = int(
                registry.get(
                    "checkpoint_after_iterations", DEFAULT_CHECKPOINT_AFTER
                )
            )
            runtime, created = start_runtime_session(
                paths,
                str(project["id"]),
                task_id,
                base_sha256,
                checkpoint_after,
                audit_rules_fingerprint(root, project),
                replaces_task_id,
                (
                    audit_fingerprint(root, registry, project, base_sha256)
                    if replaces_task_id is not None
                    else None
                ),
            )
            telemetry = runtime.get("codex_telemetry", empty_codex_telemetry())
            validate_codex_telemetry(telemetry)
            reasons = telemetry_budget_reasons(registry, telemetry)
            rollover_notice: str | None = None
            if reasons:
                rollover_notice = (
                    "CONTEXT_ROLLOVER_REQUIRED "
                    f"project={project['id']} task={task_id} "
                    f"reasons={','.join(reasons)} base_sha256={base_sha256} "
                    f"telemetry={telemetry_summary(telemetry)}"
                )
            if (
                rollover_notice is None
                and not created
                and output is None
                and not full_rules
                and not resume_state
            ):
                print(
                    "CONTEXT_PREFLIGHT_ALREADY_ACTIVE "
                    f"project={project['id']} task={task_id} "
                    "iterations="
                    f"{runtime['substantive_iterations']}/{runtime['checkpoint_after']} "
                    f"telemetry={telemetry_summary(telemetry)}"
                )
                return 0
    include_active_state = resume_state or (runtime is not None and not created)
    bundle = build_bundle(
        root,
        registry,
        project,
        full_rules=full_rules,
        include_active_state=include_active_state,
    )
    if runtime is not None and rollover_notice is not None:
        bundle = rollover_notice + "\n\n" + bundle
    if runtime is not None:
        telemetry = runtime.get("codex_telemetry", empty_codex_telemetry())
        bundle = (
            bundle.rstrip()
            + "\n\n## Context Budget\n\n"
            + f"- Task ID: `{runtime['task_id']}`\n"
            + "- Run `contextctl.py pulse --project "
            + f"{project['id']}` after every 10 substantive tool batches "
            + "with `--count 10`, or use the actual batch count at a natural milestone.\n"
            + "- Iterations: "
            + f"{runtime['substantive_iterations']}/{runtime['checkpoint_after']}\n"
            + f"- Codex telemetry: `{telemetry_summary(telemetry)}`\n"
            + "- `CONTEXT_CHECKPOINT_DUE` means checkpoint in this thread. "
            + "`CONTEXT_ROLLOVER_REQUIRED` means checkpoint, then let the "
            + "authorized Desktop/App Server controller create and start a fresh task.\n"
            )
    if output:
        atomic_write(Path(output).resolve(), bundle)
        print(f"wrote {len(bundle.encode('utf-8'))} bytes: {Path(output).resolve()}")
    else:
        print(bundle, end="")
    return 0


def cmd_checkpoint(
    root: Path,
    registry: dict[str, Any],
    selector: str,
    input_path: str,
    task_id_arg: str | None,
) -> int:
    project = resolve_project(root, registry, selector)
    paths = project_paths(root, project)
    candidate = read_json(Path(input_path).resolve())
    base_sha256 = candidate.pop("base_sha256", None)
    if not isinstance(base_sha256, str) or not re.fullmatch(
        r"[0-9a-fA-F]{64}", base_sha256
    ):
        raise ContextError(
            "checkpoint candidate requires base_sha256 from the preflight bundle"
        )
    validate_state(candidate, str(project["id"]))
    validate_project_state(candidate, project, root)
    rendered = render_state(candidate)
    lock_path = paths["state"].parent / "checkpoint.lock"
    with checkpoint_lock(lock_path):
        current = read_json(paths["state"])
        validate_state(current, str(project["id"]))
        current_sha256 = state_sha256(current)
        if base_sha256.casefold() != current_sha256:
            raise ContextError(
                "stale checkpoint: "
                f"candidate base is {base_sha256.casefold()}, "
                f"current state is {current_sha256}"
            )
        if candidate == current:
            atomic_write(paths["active"], rendered)
            reset_runtime_session(
                paths,
                str(project["id"]),
                resolve_task_id(task_id_arg),
                current_sha256,
                int(
                    registry.get(
                        "checkpoint_after_iterations", DEFAULT_CHECKPOINT_AFTER
                    )
                ),
            )
            print(f"checkpoint unchanged: {project['id']}")
            return 0

        paths["history"].mkdir(parents=True, exist_ok=True)
        current_text = json.dumps(
            current, ensure_ascii=False, indent=2, sort_keys=True
        ) + "\n"
        digest = current_sha256[:12]
        timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        archive_path = paths["history"] / f"{timestamp}-{digest}.json"
        atomic_write(archive_path, current_text)
        candidate_text = json.dumps(
            candidate, ensure_ascii=False, indent=2, sort_keys=True
        ) + "\n"
        atomic_write(paths["state"], candidate_text)
        atomic_write(paths["active"], rendered)
        new_sha256 = state_sha256(candidate)
        reset_runtime_session(
            paths,
            str(project["id"]),
            resolve_task_id(task_id_arg),
            new_sha256,
            int(
                registry.get(
                    "checkpoint_after_iterations", DEFAULT_CHECKPOINT_AFTER
                )
            ),
        )
        print(f"checkpointed {project['id']}; archived {archive_path}")
        return 0


def cmd_pulse(
    root: Path,
    registry: dict[str, Any],
    selector: str,
    task_id_arg: str | None,
    count: int,
) -> int:
    if count < 0 or count > 100:
        raise ContextError("pulse count must be from 0 to 100")
    project = resolve_project(root, registry, selector)
    paths = project_paths(root, project)
    state = read_json(paths["state"])
    validate_state(state, str(project["id"]))
    validate_project_state(state, project, root)
    task_id = resolve_task_id(task_id_arg)
    if task_id is None:
        raise ContextError("task id unavailable; pass --task-id")
    base_sha256 = state_sha256(state)
    runtime = pulse_runtime_session(
        paths,
        str(project["id"]),
        task_id,
        base_sha256,
        count,
    )
    iterations = int(runtime["substantive_iterations"])
    checkpoint_after = int(runtime["checkpoint_after"])
    telemetry = runtime.get("codex_telemetry", empty_codex_telemetry())
    validate_codex_telemetry(telemetry)
    reasons = telemetry_budget_reasons(registry, telemetry)
    if reasons:
        print(
            "CONTEXT_ROLLOVER_REQUIRED "
            f"project={project['id']} task={task_id} "
            f"reasons={','.join(reasons)} action=checkpoint-and-rollover "
            f"telemetry={telemetry_summary(telemetry)}"
        )
        return 2
    if iterations >= checkpoint_after:
        print(
            "CONTEXT_CHECKPOINT_DUE "
            f"project={project['id']} task={task_id} "
            f"iterations={iterations}/{checkpoint_after} "
            "reason=iterations action=checkpoint"
        )
        return 2
    print(
        "CONTEXT_BUDGET_OK "
        f"project={project['id']} task={task_id} "
        f"iterations={iterations}/{checkpoint_after} "
        f"telemetry={telemetry_summary(telemetry)}"
    )
    return 0


def cmd_refresh(
    root: Path,
    registry: dict[str, Any],
    selector: str,
    task_id_arg: str | None,
) -> int:
    project = resolve_project(root, registry, selector)
    task_id = resolve_task_id(task_id_arg)
    if task_id is None:
        raise ContextError("task id unavailable; pass --task-id")
    old_sha256, new_sha256 = refresh_runtime_state(
        root,
        project_paths(root, project),
        project,
        task_id,
        int(
            registry.get(
                "checkpoint_after_iterations", DEFAULT_CHECKPOINT_AFTER
            )
        ),
    )
    sentinel = (
        "CONTEXT_STATE_CURRENT"
        if old_sha256 == new_sha256
        else "CONTEXT_STATE_REFRESHED"
    )
    print(
        f"{sentinel} project={project['id']} task={task_id} "
        f"old_sha256={old_sha256[:12]} state_sha256={new_sha256}"
    )
    return 0


def cmd_finish(
    root: Path,
    registry: dict[str, Any],
    selector: str,
    task_id_arg: str | None,
    replaced_by_arg: str | None = None,
) -> int:
    project = resolve_project(root, registry, selector)
    task_id = resolve_task_id(task_id_arg)
    if task_id is None:
        raise ContextError("task id unavailable; pass --task-id")
    paths = project_paths(root, project)
    session_path = runtime_path(paths, task_id)
    replacement_task_id = (
        resolve_task_id(replaced_by_arg) if replaced_by_arg is not None else None
    )
    if replacement_task_id == task_id:
        raise ContextError("replacement task must differ from source task")
    replacement_path = (
        runtime_path(paths, replacement_task_id)
        if replacement_task_id is not None
        else None
    )
    receipt_path = completion_receipt_path(paths, task_id)
    state_lock = paths["state"].parent / "checkpoint.lock"
    runtime_locks = {session_path.with_suffix(".lock")}
    if replacement_path is not None:
        runtime_locks.add(replacement_path.with_suffix(".lock"))
    with checkpoint_lock(state_lock):
        with contextlib.ExitStack() as stack:
            for runtime_lock in sorted(runtime_locks, key=lambda item: str(item)):
                stack.enter_context(checkpoint_lock(runtime_lock))
            if not session_path.is_file():
                if not receipt_path.is_file():
                    raise ContextError(
                        "runtime session is missing; completion is not proven"
                    )
                expected_kind = (
                    "retired" if replacement_task_id is not None else "completed"
                )
                receipt = read_json(receipt_path)
                validate_lifecycle_receipt(
                    receipt,
                    str(project["id"]),
                    task_id,
                    expected_kind=expected_kind,
                    expected_replacement_task_id=replacement_task_id,
                )
                print(
                    f"context session finished: {project['id']} task={task_id} "
                    f"kind={expected_kind}"
                )
                return 0
            runtime = read_json(session_path)
            validate_runtime_identity(runtime, str(project["id"]), task_id)
            source_state_sha256 = require_state_sha256(
                runtime.get("base_sha256"),
                "source runtime base_sha256",
            )
            source_started_state_sha256 = require_state_sha256(
                runtime.get("started_state_sha256"),
                "source runtime started_state_sha256",
            )
            source_started_rules_fingerprint = require_state_sha256(
                runtime.get("started_rules_fingerprint_sha256"),
                "source runtime started_rules_fingerprint_sha256",
            )
            own_work_evidence: dict[str, Any] | None = None
            if runtime.get("source_task_id") is not None:
                own_work_evidence = observe_runtime_work(
                    runtime,
                    task_id,
                    session_path,
                )
            replacement_work_evidence: dict[str, Any] | None = None
            if replacement_task_id is not None:
                assert replacement_path is not None
                replacement_runtime: dict[str, Any] | None = None
                if replacement_path.is_file():
                    replacement = read_json(replacement_path)
                    replacement_runtime = replacement
                    validate_runtime_identity(
                        replacement,
                        str(project["id"]),
                        replacement_task_id,
                    )
                    target_start_sha256 = replacement.get("started_state_sha256")
                    target_started_rules_fingerprint = replacement.get(
                        "started_rules_fingerprint_sha256"
                    )
                    target_source_task_id = replacement.get("source_task_id")
                    target_source_state_sha256 = replacement.get(
                        "source_audited_state_sha256"
                    )
                    target_source_rules_fingerprint = replacement.get(
                        "source_audit_rules_fingerprint_sha256"
                    )
                    target_source_audit_fingerprint = replacement.get(
                        "source_audit_fingerprint_sha256"
                    )
                else:
                    target_receipt_path = completion_receipt_path(
                        paths,
                        replacement_task_id,
                    )
                    if not target_receipt_path.is_file():
                        raise ContextError("replacement runtime session is missing")
                    target_receipt = read_json(target_receipt_path)
                    target_receipt_kind = target_receipt.get("kind")
                    target_receipt_replacement = target_receipt.get(
                        "replacement_task_id"
                    )
                    validate_lifecycle_receipt(
                        target_receipt,
                        str(project["id"]),
                        replacement_task_id,
                        expected_kind=(
                            target_receipt_kind
                            if isinstance(target_receipt_kind, str)
                            else None
                        ),
                        expected_replacement_task_id=(
                            target_receipt_replacement
                            if isinstance(target_receipt_replacement, str)
                            else None
                        ),
                    )
                    target_start_sha256 = target_receipt.get(
                        "started_state_sha256"
                    )
                    target_started_rules_fingerprint = target_receipt.get(
                        "started_rules_fingerprint_sha256"
                    )
                    target_source_task_id = target_receipt.get("source_task_id")
                    target_source_state_sha256 = target_receipt.get(
                        "source_audited_state_sha256"
                    )
                    target_source_rules_fingerprint = target_receipt.get(
                        "source_audit_rules_fingerprint_sha256"
                    )
                    target_source_audit_fingerprint = target_receipt.get(
                        "source_audit_fingerprint_sha256"
                    )
                    replacement_work_evidence = target_receipt.get("work_evidence")
                    validate_work_evidence(
                        replacement_work_evidence,
                        replacement_task_id,
                    )
                target_start_sha256 = require_state_sha256(
                    target_start_sha256,
                    "replacement started_state_sha256",
                )
                if target_start_sha256 != source_state_sha256:
                    raise ContextError("replacement runtime state does not match source")
                if runtime.get("audited_state_sha256") != source_state_sha256:
                    raise ContextError(
                        "source task-bound audit receipt is missing or stale"
                    )
                source_rules_fingerprint = require_state_sha256(
                    runtime.get("audit_rules_fingerprint_sha256"),
                    "source task-bound audit rules fingerprint",
                )
                source_audit_fingerprint = require_state_sha256(
                    runtime.get("audit_fingerprint_sha256"),
                    "source task-bound audit fingerprint",
                )
                target_started_rules_fingerprint = require_state_sha256(
                    target_started_rules_fingerprint,
                    "replacement started_rules_fingerprint_sha256",
                )
                if target_started_rules_fingerprint != source_rules_fingerprint:
                    raise ContextError(
                        "replacement runtime started rules do not match the "
                        "source task-bound audit"
                    )
                if target_source_task_id != task_id:
                    raise ContextError(
                        "replacement runtime is not bound to the source task"
                    )
                if require_state_sha256(
                    target_source_state_sha256,
                    "replacement source_audited_state_sha256",
                ) != source_state_sha256:
                    raise ContextError(
                        "replacement runtime source state lineage does not match"
                    )
                if require_state_sha256(
                    target_source_rules_fingerprint,
                    "replacement source_audit_rules_fingerprint_sha256",
                ) != source_rules_fingerprint:
                    raise ContextError(
                        "replacement runtime source rules lineage does not match"
                    )
                if require_state_sha256(
                    target_source_audit_fingerprint,
                    "replacement source_audit_fingerprint_sha256",
                ) != source_audit_fingerprint:
                    raise ContextError(
                        "replacement runtime source audit lineage does not match"
                    )
                if replacement_runtime is not None:
                    replacement_work_evidence = observe_runtime_work(
                        replacement_runtime,
                        replacement_task_id,
                        replacement_path,
                    )
                    atomic_write(
                        replacement_path,
                        json.dumps(
                            replacement_runtime,
                            ensure_ascii=False,
                            indent=2,
                            sort_keys=True,
                        )
                        + "\n",
                    )
                receipt_kind = "retired"
            else:
                current_audit = audit_one(root, registry, project)
                finish_audit_issues = [
                    *root_audit_issues(root),
                    *current_audit["issues"],
                ]
                if finish_audit_issues:
                    raise ContextError(
                        "context audit failed at finish: "
                        + "; ".join(finish_audit_issues)
                    )
                state = read_json(paths["state"])
                validate_state(state, str(project["id"]))
                validate_project_state(state, project, root)
                current_sha256 = state_sha256(state)
                if source_state_sha256 != current_sha256:
                    raise ContextError("runtime session state changed; run refresh first")
                if runtime.get("audited_state_sha256") != current_sha256:
                    raise ContextError(
                        "task-bound audit receipt is missing or stale; "
                        "run audit --project ... --task-id ..."
                    )
                if runtime.get("audit_fingerprint_sha256") != audit_fingerprint(
                    root,
                    registry,
                    project,
                    current_sha256,
                ):
                    raise ContextError(
                        "task-bound audit fingerprint is missing or stale; "
                        "rerun audit --project ... --task-id ..."
                    )
                if state.get("open") or state.get("next_actions"):
                    raise ContextError(
                        "objective is still active; clear open and next_actions in a "
                        "validated checkpoint before finish"
                    )
                receipt_kind = "completed"
            receipt = {
                "schema_version": LIFECYCLE_RECEIPT_SCHEMA_VERSION,
                "project_id": str(project["id"]),
                "task_id": task_id,
                "kind": receipt_kind,
                "state_sha256": runtime.get("base_sha256"),
                "started_state_sha256": source_started_state_sha256,
                "started_rules_fingerprint_sha256": (
                    source_started_rules_fingerprint
                ),
                "audited_state_sha256": runtime.get("audited_state_sha256"),
                "audit_rules_fingerprint_sha256": runtime.get(
                    "audit_rules_fingerprint_sha256"
                ),
                "audit_fingerprint_sha256": runtime.get("audit_fingerprint_sha256"),
                "source_task_id": runtime.get("source_task_id"),
                "source_audited_state_sha256": runtime.get(
                    "source_audited_state_sha256"
                ),
                "source_audit_rules_fingerprint_sha256": runtime.get(
                    "source_audit_rules_fingerprint_sha256"
                ),
                "source_audit_fingerprint_sha256": runtime.get(
                    "source_audit_fingerprint_sha256"
                ),
                "replacement_task_id": replacement_task_id,
                "work_evidence": own_work_evidence,
                "replacement_work_evidence": replacement_work_evidence,
                "recorded_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            }
            validate_lifecycle_receipt(
                receipt,
                str(project["id"]),
                task_id,
                expected_kind=receipt_kind,
                expected_replacement_task_id=replacement_task_id,
            )
            atomic_write(
                receipt_path,
                json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n",
            )
            session_path.unlink()
    try:
        session_path.parent.rmdir()
    except OSError:
        pass
    print(
        f"context session finished: {project['id']} task={task_id} "
        f"kind={receipt_kind}"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    default_root = Path(__file__).resolve().parents[4]
    parser = argparse.ArgumentParser(description="Bounded context control plane")
    parser.add_argument("--root", default=str(default_root))
    subparsers = parser.add_subparsers(dest="command", required=True)

    render_parser = subparsers.add_parser("render")
    render_parser.add_argument("--project")

    audit_parser = subparsers.add_parser("audit")
    audit_parser.add_argument("--project")
    audit_parser.add_argument("--task-id")

    preflight_parser = subparsers.add_parser("preflight")
    preflight_parser.add_argument("--project", required=True)
    preflight_parser.add_argument("--output")
    preflight_parser.add_argument("--full-rules", action="store_true")
    preflight_parser.add_argument("--task-id")
    preflight_parser.add_argument(
        "--replaces-task",
        help="bind a fresh rollover target to one audited source task",
    )
    preflight_parser.add_argument("--no-session", action="store_true")
    preflight_parser.add_argument(
        "--resume",
        action="store_true",
        help="inject the prior Active State for an explicit resume or rollover",
    )

    checkpoint_parser = subparsers.add_parser("checkpoint")
    checkpoint_parser.add_argument("--project", required=True)
    checkpoint_parser.add_argument("--input", required=True)
    checkpoint_parser.add_argument("--task-id")

    pulse_parser = subparsers.add_parser("pulse")
    pulse_parser.add_argument("--project", required=True)
    pulse_parser.add_argument("--task-id")
    pulse_parser.add_argument(
        "--count",
        type=int,
        default=1,
        help="substantive batches to add; use 0 for a telemetry-only hook probe",
    )

    refresh_parser = subparsers.add_parser(
        "refresh",
        help="rebase an active runtime onto the latest validated state",
        description="Rebase an active runtime onto the latest validated state.",
    )
    refresh_parser.add_argument("--project", required=True)
    refresh_parser.add_argument("--task-id")

    finish_parser = subparsers.add_parser("finish")
    finish_parser.add_argument("--project", required=True)
    finish_parser.add_argument("--task-id")
    finish_parser.add_argument("--replaced-by")
    return parser


def main() -> int:
    configure_console()
    args = build_parser().parse_args()
    root = Path(args.root).resolve()
    registry = load_registry(root)
    if args.command == "render":
        return cmd_render(root, registry, args.project)
    if args.command == "audit":
        return cmd_audit(root, registry, args.project, args.task_id)
    if args.command == "preflight":
        return cmd_preflight(
            root,
            registry,
            args.project,
            args.output,
            args.full_rules,
            args.task_id,
            args.no_session,
            args.resume,
            args.replaces_task,
        )
    if args.command == "checkpoint":
        return cmd_checkpoint(
            root,
            registry,
            args.project,
            args.input,
            args.task_id,
        )
    if args.command == "pulse":
        return cmd_pulse(
            root,
            registry,
            args.project,
            args.task_id,
            args.count,
        )
    if args.command == "refresh":
        return cmd_refresh(root, registry, args.project, args.task_id)
    if args.command == "finish":
        return cmd_finish(
            root,
            registry,
            args.project,
            args.task_id,
            args.replaced_by,
        )
    raise ContextError(f"unsupported command: {args.command}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ContextError as exc:
        configure_console()
        print(f"contextctl: {exc}", file=sys.stderr)
        raise SystemExit(1)
