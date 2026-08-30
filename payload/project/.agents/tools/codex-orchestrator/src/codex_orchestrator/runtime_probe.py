"""Executable version probe shared by bootstrap and offline verification."""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import sys
from importlib.metadata import PackageNotFoundError, version


EXPECTED_PYTHON = (3, 11)
EXPECTED_DISTRIBUTIONS = {
    "temporalio": "1.30.0",
    "openai-codex": "0.144.4",
    "cryptography": "49.0.0",
}


def collect() -> dict[str, object]:
    actual: dict[str, str] = {}
    for distribution in EXPECTED_DISTRIBUTIONS:
        try:
            actual[distribution] = version(distribution)
        except PackageNotFoundError:
            actual[distribution] = "missing"
    return {
        "ok": sys.version_info[:2] == EXPECTED_PYTHON
        and actual == EXPECTED_DISTRIBUTIONS,
        "python": platform.python_version(),
        "python_executable": sys.executable,
        "python_base_executable": getattr(sys, "_base_executable", sys.executable),
        "temporalio": actual["temporalio"],
        "openai-codex": actual["openai-codex"],
        "cryptography": actual["cryptography"],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex-orchestrator-runtime-probe")
    parser.add_argument("--payload-key-file")
    parser.add_argument("--payload-key-id")
    parser.add_argument("--payload-key-sha256")
    return parser


async def _probe_payload_encryption(
    *, key_file: str, key_id: str, key_sha256: str
) -> None:
    from .payload_security import encrypted_data_converter

    marker = "bounded-payload-encryption-smoke"
    value = {"probe": marker}
    converter = encrypted_data_converter(
        key_id=key_id,
        key_path=key_file,
        expected_key_sha256=key_sha256,
    )
    encoded = await converter.encode([value])
    wire = b"".join(
        payload.SerializeToString(deterministic=True) for payload in encoded
    )
    if marker.encode("utf-8") in wire:
        raise RuntimeError("payload encryption smoke retained plaintext")
    (decoded,) = await converter.decode(encoded, [dict])
    if decoded != value:
        raise RuntimeError("payload encryption smoke round trip failed")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = collect()
    key_options = (
        args.payload_key_file,
        args.payload_key_id,
        args.payload_key_sha256,
    )
    if any(value is not None for value in key_options):
        if any(value is None for value in key_options):
            result["ok"] = False
            result["payload_encryption"] = "incomplete_configuration"
        elif result["ok"]:
            try:
                asyncio.run(
                    _probe_payload_encryption(
                        key_file=args.payload_key_file,
                        key_id=args.payload_key_id,
                        key_sha256=args.payload_key_sha256,
                    )
                )
            except Exception as exc:
                result["ok"] = False
                result["payload_encryption"] = "failed"
                result["payload_error"] = type(exc).__name__
                result["payload_detail"] = str(exc)
            else:
                result["payload_encryption"] = "ok"
    else:
        result["payload_encryption"] = "not_checked"
    stream = sys.stdout if result["ok"] else sys.stderr
    print(json.dumps(result, sort_keys=True), file=stream)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
