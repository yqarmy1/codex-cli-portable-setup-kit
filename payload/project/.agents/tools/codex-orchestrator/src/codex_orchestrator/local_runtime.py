"""Fail-closed configuration for the local Temporal candidate runtime.

This module is deliberately dependency-free.  Bootstrap and verification can
exercise the path, version, address, and PID-record contracts before importing
the Temporal or Codex SDKs.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Sequence


TEMPORAL_CLI_VERSION = "1.8.2"
TEMPORAL_CLI_WINDOWS_AMD64_ARCHIVE = (
    "temporal_cli_1.8.2_windows_amd64.zip"
)
TEMPORAL_CLI_WINDOWS_AMD64_SHA256 = (
    "72e02498fa7849657c369377f7de69a8709b3d2183b6f2749f6c8bd54a984501"
)
TEMPORAL_CLI_DOWNLOAD_URL = (
    "https://github.com/temporalio/cli/releases/download/v1.8.2/"
    "temporal_cli_1.8.2_windows_amd64.zip"
)
TEMPORAL_PYTHON_SDK_VERSION = "1.30.0"
OPENAI_CODEX_SDK_VERSION = "0.144.4"

TEMPORAL_ADDRESS = "127.0.0.1:7233"
TEMPORAL_UI_ADDRESS = "127.0.0.1:8233"
TEMPORAL_HOST = "127.0.0.1"
TEMPORAL_PORT = 7233
TEMPORAL_UI_PORT = 8233
TEMPORAL_NAMESPACE = "default"
TASK_QUEUE_PREFIX = "codex-orchestrator-v2"
PROCESS_RECORD_SCHEMA_VERSION = 1
WORKFLOW_ID_SCHEMA_VERSION = 2
RUNTIME_MANIFEST_SCHEMA_VERSION = 2
PAYLOAD_ENCRYPTION_ALGORITHM = "aes-256-gcm"
PAYLOAD_KEY_FILENAME = "temporal-payload-aes256.key"

_PAYLOAD_KEY_ID_PATTERN = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_SHA256_PATTERN = re.compile(r"\A[0-9a-f]{64}\Z")

ProcessRole = Literal["temporal-server", "worker"]


def _resolved(path: str | os.PathLike[str], *, strict: bool = False) -> Path:
    return Path(path).expanduser().resolve(strict=strict)


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def validate_temporal_address(address: str) -> str:
    """Accept only the pinned IPv4 loopback endpoint.

    ``localhost`` is intentionally not accepted: name resolution and an IPv6
    fallback would make the actual bind/connect target less explicit.
    """

    if address != TEMPORAL_ADDRESS:
        raise ValueError(
            f"Temporal address must be exactly {TEMPORAL_ADDRESS}; got {address!r}"
        )
    return address


def _canonical_project_path(project_root: str | os.PathLike[str]) -> str:
    resolved = _resolved(project_root, strict=True)
    if not resolved.is_dir():
        raise ValueError(f"project root is not a directory: {resolved}")
    return os.path.normcase(str(resolved)).replace("\\", "/")


def project_key_for_project(project_root: str | os.PathLike[str]) -> str:
    canonical = _canonical_project_path(project_root)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


def task_queue_for_project(project_root: str | os.PathLike[str]) -> str:
    return f"{TASK_QUEUE_PREFIX}-{project_key_for_project(project_root)}"


def validate_task_queue(
    task_queue: str, project_root: str | os.PathLike[str]
) -> str:
    expected = task_queue_for_project(project_root)
    if task_queue != expected:
        raise ValueError(f"task queue must be exactly {expected}; got {task_queue!r}")
    return task_queue


@dataclass(frozen=True)
class RuntimePaths:
    """One workspace server plus project-isolated Worker/command paths."""

    workspace_root: Path
    project_root: Path
    project_key: str
    runtime_root: Path
    venv_root: Path
    venv_python: Path
    temporal_exe: Path
    temporal_db: Path
    secrets_dir: Path
    payload_key_file: Path
    logs_dir: Path
    pids_dir: Path
    locks_dir: Path
    command_lock: Path
    temporal_pid_record: Path
    worker_pid_record: Path
    manifest_file: Path

    @classmethod
    def from_project_root(
        cls,
        project_root: str | os.PathLike[str],
        *,
        workspace_root: str | os.PathLike[str],
        require_project_exists: bool = True,
    ) -> "RuntimePaths":
        workspace = _resolved(workspace_root, strict=True)
        project = _resolved(project_root, strict=require_project_exists)
        if not workspace.is_dir():
            raise ValueError(f"workspace root is not a directory: {workspace}")
        if require_project_exists and not project.is_dir():
            raise ValueError(f"project root is not a directory: {project}")
        if project != workspace and not _is_within(project, workspace):
            raise ValueError("project root must equal or be below the workspace root")
        project_key = project_key_for_project(project)
        runtime = _resolved(
            workspace / ".workspace" / "tools" / "codex-orchestrator"
        )
        paths = cls(
            workspace_root=workspace,
            project_root=project,
            project_key=project_key,
            runtime_root=runtime,
            venv_root=runtime / ".venv",
            venv_python=runtime / ".venv" / "Scripts" / "python.exe",
            temporal_exe=runtime / "bin" / "temporal.exe",
            # v1 state.db contains pre-encryption histories and must never be
            # replayed by the encrypted v2 Worker.
            temporal_db=runtime / "temporal" / "state-v2.db",
            secrets_dir=runtime / "secrets",
            payload_key_file=runtime / "secrets" / PAYLOAD_KEY_FILENAME,
            logs_dir=runtime / "logs",
            pids_dir=runtime / "pids",
            locks_dir=runtime / "locks",
            command_lock=runtime / "locks" / f"commands-{project_key}.lock",
            temporal_pid_record=runtime / "pids" / "temporal-server.json",
            worker_pid_record=runtime / "pids" / f"worker-{project_key}.json",
            manifest_file=runtime / "runtime-manifest.json",
        )
        paths.validate()
        return paths

    def validate(self) -> None:
        workspace = _resolved(self.workspace_root)
        project = _resolved(self.project_root)
        runtime = _resolved(self.runtime_root)
        if project != workspace and not _is_within(project, workspace):
            raise ValueError("project root escapes the workspace root")
        if runtime != _resolved(
            workspace / ".workspace" / "tools" / "codex-orchestrator"
        ):
            raise ValueError("runtime root is not the pinned workspace runtime")
        if self.project_key != project_key_for_project(project):
            raise ValueError("project key does not match the project root")
        for name, value in asdict(self).items():
            if name in {"workspace_root", "project_root", "project_key"}:
                continue
            candidate = _resolved(value)
            if candidate != runtime and not _is_within(candidate, runtime):
                raise ValueError(f"{name} escapes the runtime root: {candidate}")
        if self.temporal_db.name != "state-v2.db":
            raise ValueError("encrypted Temporal DB must be named state-v2.db")
        if self.payload_key_file != self.secrets_dir / PAYLOAD_KEY_FILENAME:
            raise ValueError("payload key path is not the pinned runtime secret")

    @property
    def payload_key_id(self) -> str:
        """Return the validated nonsecret rotation ID from the manifest."""

        return load_payload_encryption_config(self).key_id

    @property
    def payload_key_sha256(self) -> str:
        """Return the validated key fingerprint without reading key bytes."""

        return load_payload_encryption_config(self).key_sha256

    def create_directories(self) -> None:
        self.validate()
        for directory in (
            self.runtime_root,
            self.temporal_exe.parent,
            self.temporal_db.parent,
            self.secrets_dir,
            self.logs_dir,
            self.pids_dir,
            self.locks_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def pid_record_for(self, role: ProcessRole) -> Path:
        if role == "temporal-server":
            return self.temporal_pid_record
        if role == "worker":
            return self.worker_pid_record
        raise ValueError(f"unsupported process role: {role!r}")


@dataclass(frozen=True)
class PayloadEncryptionConfig:
    """Nonsecret manifest data plus the pinned runtime-only key path."""

    key_id: str
    key_sha256: str
    key_path: Path = field(repr=False)
    algorithm: str = PAYLOAD_ENCRYPTION_ALGORITHM


def load_payload_encryption_config(paths: RuntimePaths) -> PayloadEncryptionConfig:
    """Read and validate only the bounded nonsecret encryption manifest."""

    paths.validate()
    try:
        if paths.manifest_file.stat().st_size > 128 * 1024:
            raise ValueError("runtime manifest is unexpectedly large")
        manifest = json.loads(paths.manifest_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("runtime manifest could not be read") from None
    if not isinstance(manifest, dict):
        raise ValueError("runtime manifest must be an object")
    if manifest.get("schema_version") != RUNTIME_MANIFEST_SCHEMA_VERSION:
        raise ValueError("runtime manifest schema is not encrypted v2")
    if manifest.get("workspace_root") != str(paths.workspace_root):
        raise ValueError("runtime manifest workspace does not match")
    if manifest.get("runtime_root") != str(paths.runtime_root):
        raise ValueError("runtime manifest root does not match")
    if manifest.get("temporal_db") != str(paths.temporal_db):
        raise ValueError("runtime manifest Temporal DB does not match encrypted v2")
    if manifest.get("task_queue_prefix") != TASK_QUEUE_PREFIX:
        raise ValueError("runtime manifest task queue prefix does not match v2")

    encryption = manifest.get("payload_encryption")
    if not isinstance(encryption, dict) or set(encryption) != {
        "algorithm",
        "key_id",
        "key_sha256",
    }:
        raise ValueError("runtime payload encryption manifest is invalid")
    if encryption.get("algorithm") != PAYLOAD_ENCRYPTION_ALGORITHM:
        raise ValueError("runtime payload encryption algorithm is unsupported")
    key_id = encryption.get("key_id")
    key_sha256 = encryption.get("key_sha256")
    if not isinstance(key_id, str) or not _PAYLOAD_KEY_ID_PATTERN.fullmatch(key_id):
        raise ValueError("runtime payload key ID is invalid")
    if (
        not isinstance(key_sha256, str)
        or not _SHA256_PATTERN.fullmatch(key_sha256)
    ):
        raise ValueError("runtime payload key hash is invalid")
    return PayloadEncryptionConfig(
        key_id=key_id,
        key_sha256=key_sha256,
        key_path=paths.payload_key_file,
    )


def workflow_id_for_project(project_root: str | os.PathLike[str]) -> str:
    """Derive a stable, non-path-revealing Workflow ID for a project."""

    canonical = _canonical_project_path(project_root)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
    return f"codex-supervisor-v{WORKFLOW_ID_SCHEMA_VERSION}-{digest}"


def temporal_server_arguments(paths: RuntimePaths) -> tuple[str, ...]:
    """Return the only supported local server command line.

    Persistence and both bind addresses are explicit.  Callers must use the
    exact returned sequence and must not append user-controlled server flags.
    """

    paths.validate()
    return (
        "server",
        "start-dev",
        "--ip",
        TEMPORAL_HOST,
        "--port",
        str(TEMPORAL_PORT),
        "--ui-ip",
        TEMPORAL_HOST,
        "--ui-port",
        str(TEMPORAL_UI_PORT),
        "--db-filename",
        str(paths.temporal_db),
    )


@dataclass(frozen=True)
class ProcessRecord:
    schema_version: int
    role: ProcessRole
    pid: int
    executable: str
    arguments: tuple[str, ...]
    workspace_root: str
    project_root: str
    started_utc: str

    @classmethod
    def create(
        cls,
        *,
        role: ProcessRole,
        pid: int,
        executable: str | os.PathLike[str],
        arguments: Sequence[str],
        paths: RuntimePaths,
    ) -> "ProcessRecord":
        record = cls(
            schema_version=PROCESS_RECORD_SCHEMA_VERSION,
            role=role,
            pid=pid,
            executable=str(_resolved(executable)),
            arguments=tuple(str(value) for value in arguments),
            workspace_root=str(paths.workspace_root),
            project_root=str(
                paths.workspace_root if role == "temporal-server" else paths.project_root
            ),
            started_utc=datetime.now(timezone.utc).isoformat(),
        )
        record.validate(paths)
        return record

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ProcessRecord":
        expected = {
            "schema_version",
            "role",
            "pid",
            "executable",
            "arguments",
            "workspace_root",
            "project_root",
            "started_utc",
        }
        if set(value) != expected:
            raise ValueError("process record has missing or unexpected fields")
        arguments = value["arguments"]
        if not isinstance(arguments, list) or not all(
            isinstance(item, str) for item in arguments
        ):
            raise ValueError("process record arguments must be a string array")
        record = cls(
            schema_version=value["schema_version"],
            role=value["role"],
            pid=value["pid"],
            executable=value["executable"],
            arguments=tuple(arguments),
            workspace_root=value["workspace_root"],
            project_root=value["project_root"],
            started_utc=value["started_utc"],
        )
        return record

    def validate(self, paths: RuntimePaths) -> None:
        paths.validate()
        if self.schema_version != PROCESS_RECORD_SCHEMA_VERSION:
            raise ValueError("unsupported process record schema")
        if self.role not in {"temporal-server", "worker"}:
            raise ValueError(f"unsupported process role: {self.role!r}")
        if type(self.pid) is not int or self.pid <= 0:
            raise ValueError("PID must be a positive integer")
        executable = _resolved(self.executable)
        expected_executable = (
            paths.temporal_exe if self.role == "temporal-server" else paths.venv_python
        )
        if executable != _resolved(expected_executable):
            raise ValueError(
                f"unexpected executable for {self.role}: {self.executable!r}"
            )
        if _resolved(self.workspace_root) != paths.workspace_root:
            raise ValueError("process record belongs to a different workspace")
        expected_project = (
            paths.workspace_root if self.role == "temporal-server" else paths.project_root
        )
        if _resolved(self.project_root) != expected_project:
            raise ValueError("process record belongs to a different project")
        try:
            parsed = datetime.fromisoformat(self.started_utc)
        except (TypeError, ValueError) as exc:
            raise ValueError("started_utc is not ISO-8601") from exc
        if parsed.tzinfo is None:
            raise ValueError("started_utc must include a timezone")
        if not self.arguments or not all(isinstance(arg, str) for arg in self.arguments):
            raise ValueError("process arguments must be a non-empty string sequence")
        if self.role == "temporal-server":
            if self.arguments != temporal_server_arguments(paths):
                raise ValueError("Temporal server arguments do not match the pinned plan")
        else:
            expected_arguments = (
                "-m",
                "codex_orchestrator.worker",
                "--workspace-root",
                str(paths.workspace_root),
                "--project-root",
                str(paths.project_root),
                "--temporal-address",
                TEMPORAL_ADDRESS,
                "--task-queue",
                task_queue_for_project(paths.project_root),
            )
            if self.arguments != expected_arguments:
                raise ValueError("worker arguments do not match the project-isolated plan")

    def as_json_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["arguments"] = list(self.arguments)
        return value


def load_process_record(path: Path, paths: RuntimePaths) -> ProcessRecord:
    record_path = _resolved(path)
    expected_paths = {
        _resolved(paths.temporal_pid_record),
        _resolved(paths.worker_pid_record),
    }
    if record_path not in expected_paths:
        raise ValueError("refusing to read a PID record outside the pinned paths")
    if path.stat().st_size > 32 * 1024:
        raise ValueError("PID record is unexpectedly large")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("PID record must be a JSON object")
    record = ProcessRecord.from_dict(value)
    record.validate(paths)
    if _resolved(paths.pid_record_for(record.role)) != record_path:
        raise ValueError("PID record role does not match its filename")
    return record


def write_process_record(path: Path, record: ProcessRecord, paths: RuntimePaths) -> None:
    """Atomically create a PID record without clobbering an existing owner."""

    record.validate(paths)
    if path != paths.pid_record_for(record.role):
        raise ValueError("PID record path does not match its role")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{record.pid}.tmp")
    payload = json.dumps(record.as_json_dict(), ensure_ascii=True, sort_keys=True)
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.write("\n")
        # A same-directory hard link publishes the complete record atomically
        # and fails if a competing owner already created the destination.
        # Stale-record handling is an explicit operator action.
        os.link(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def remove_process_record_if_owned(
    path: Path,
    *,
    role: ProcessRole,
    pid: int,
    paths: RuntimePaths,
) -> bool:
    """Remove only the exact record still owned by this process."""

    try:
        record = load_process_record(path, paths)
    except FileNotFoundError:
        return False
    if record.role != role or record.pid != pid:
        return False
    path.unlink()
    return True


class CommandLockBusy(TimeoutError):
    """Another local command owns the atomic query/update section."""


@contextmanager
def command_lock(paths: RuntimePaths, *, timeout_seconds: float):
    """Acquire the project-scoped interprocess command lock with a hard limit.

    The lock file is never deleted: deleting a locked inode/file can allow two
    generations of callers to believe they each own a different lock.
    """

    if not 0 < timeout_seconds <= 10:
        raise ValueError("command lock timeout must be between 0 and 10 seconds")
    paths.validate()
    paths.locks_dir.mkdir(parents=True, exist_ok=True)
    handle = paths.command_lock.open("a+b", buffering=0)
    acquired = False
    deadline = time.monotonic() + timeout_seconds
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError as exc:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CommandLockBusy(
                        "another local command still owns the query/update lock"
                    ) from exc
                time.sleep(min(0.05, remaining))
        yield
    finally:
        if acquired:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
