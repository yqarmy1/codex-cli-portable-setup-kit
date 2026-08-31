#!/usr/bin/env python3
"""Unified CLI entrypoint for Reverse Engineering Toolkit."""

import argparse
import json
import os
import sys
from pathlib import Path

# Add parent directory to path so re_toolkit imports work when executed directly
toolkit_root = Path(__file__).resolve().parent.parent
if str(toolkit_root) not in sys.path:
    sys.path.insert(0, str(toolkit_root))

from pe_parser import PEParser, BinaryParseError
from disasm import Disassembler, pattern_scan, create_patch
from emulator import MicroEmulator
from protocol_parser import ProtobufDissector, TLVDissector, format_hexdump
from frida_bridge import FridaScriptGenerator
from pipeline import PipelineEngine


def load_input_bytes(input_val: str, offset: int = 0, length: int = 0) -> bytes:
    """Load bytes from a hex string or from a file path."""
    if os.path.exists(input_val):
        with open(input_val, "rb") as f:
            if offset > 0:
                f.seek(offset)
            if length > 0:
                return f.read(length)
            return f.read()
    else:
        # Treat as hex string
        clean_hex = "".join(input_val.split()).replace("0x", "").replace("0X", "")
        return bytes.fromhex(clean_hex)


def cmd_parse_pe(args: argparse.Namespace) -> None:
    data = load_input_bytes(args.file)
    parser = PEParser(data)
    if args.json:
        out = {
            "summary": parser.summary(),
            "sections": parser.sections,
            "imports": parser.imports,
            "exports": parser.exports,
        }
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        summary = parser.summary()
        print("=== PE Binary Summary ===")
        print(f"Format:       {summary['format']} ({summary['machine']})")
        print(f"64-bit:       {summary['is_64bit']}")
        print(f"EntryPoint:   {summary['entrypoint']}")
        print(f"ImageBase:    {summary['image_base']}")
        print(f"Sections:     {', '.join(summary['sections'])}")
        print(f"Imported DLLs: {', '.join(summary['imported_dlls'])}")
        print(f"Exports:      {summary['export_count']} exported symbols")


def cmd_disasm(args: argparse.Namespace) -> None:
    data = load_input_bytes(args.target, offset=args.offset, length=args.length)
    disasm = Disassembler(arch=args.arch)
    instructions = disasm.disassemble(data, base_address=args.base)
    if args.json:
        print(json.dumps([ins.to_dict() for ins in instructions], indent=2))
    else:
        for ins in instructions:
            print(ins)


def cmd_decode_protobuf(args: argparse.Namespace) -> None:
    data = load_input_bytes(args.target)
    tree = ProtobufDissector.dissect(data)
    print(json.dumps(tree, indent=2, ensure_ascii=False))


def cmd_decode_tlv(args: argparse.Namespace) -> None:
    data = load_input_bytes(args.target)
    tlv_list = TLVDissector.dissect(data, type_len=args.type_len, length_len=args.len_len)
    print(json.dumps(tlv_list, indent=2, ensure_ascii=False))


def cmd_emulate(args: argparse.Namespace) -> None:
    data = load_input_bytes(args.code)
    emu = MicroEmulator(arch=args.arch)
    emu.load_code(data, base_address=args.base)
    state = emu.run(max_steps=args.max_steps)
    print(json.dumps(state, indent=2, ensure_ascii=False))


def cmd_gen_hook(args: argparse.Namespace) -> None:
    script = FridaScriptGenerator.generate_function_hook(
        target_symbol=args.symbol,
        module_name=args.module,
        arg_count=args.args_count,
        log_backtrace=args.backtrace,
        replace_return=args.replace_ret,
    )
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(script)
        print(f"[+] Hook script saved to {args.output}")
    else:
        print(script)


def cmd_hexdump(args: argparse.Namespace) -> None:
    data = load_input_bytes(args.target, offset=args.offset, length=args.length)
    print(format_hexdump(data, base_address=args.base))


def cmd_pipeline(args: argparse.Namespace) -> None:
    engine = PipelineEngine(
        api_key=args.api_key,
        base_url=args.base_url,
        model=args.model,
        mock_mode=args.mock,
    )
    result = engine.execute(args.prompt)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(result["rendered_text"])


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="re-toolkit",
        description="Unified Reverse Engineering, Protocol Dissection, and Emulation Toolkit",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # parse-pe
    p_pe = subparsers.add_parser("parse-pe", help="Parse PE32/PE32+ binary structure")
    p_pe.add_argument("file", help="Path to EXE/DLL/SYS file")
    p_pe.add_argument("--json", action="store_true", help="Output full JSON structure")
    p_pe.set_defaults(func=cmd_parse_pe)

    # disasm
    p_dis = subparsers.add_parser("disasm", help="Disassemble binary code or hex stream")
    p_dis.add_argument("target", help="Hex string or file path")
    p_dis.add_argument("--offset", type=lambda x: int(x, 0), default=0, help="File byte offset")
    p_dis.add_argument("--length", type=lambda x: int(x, 0), default=64, help="Byte length")
    p_dis.add_argument("--base", type=lambda x: int(x, 0), default=0x1000, help="Base address")
    p_dis.add_argument("--arch", default="x86_64", help="Architecture (x86, x86_64, arm, arm64)")
    p_dis.add_argument("--json", action="store_true", help="Output JSON instruction list")
    p_dis.set_defaults(func=cmd_disasm)

    # decode-protobuf
    p_pb = subparsers.add_parser("decode-protobuf", help="Dissect raw Protobuf binary stream")
    p_pb.add_argument("target", help="Hex string or file path")
    p_pb.set_defaults(func=cmd_decode_protobuf)

    # decode-tlv
    p_tlv = subparsers.add_parser("decode-tlv", help="Dissect TLV binary packet")
    p_tlv.add_argument("target", help="Hex string or file path")
    p_tlv.add_argument("--type-len", type=int, default=1, help="Type field length in bytes")
    p_tlv.add_argument("--len-len", type=int, default=2, help="Length field length in bytes")
    p_tlv.set_defaults(func=cmd_decode_tlv)

    # emulate
    p_emu = subparsers.add_parser("emulate", help="Emulate instruction execution")
    p_emu.add_argument("--code", required=True, help="Hex byte string of instructions")
    p_emu.add_argument("--base", type=lambda x: int(x, 0), default=0x1000, help="Base address")
    p_emu.add_argument("--arch", default="x86_64", help="Architecture (x86, x86_64)")
    p_emu.add_argument("--max-steps", type=int, default=100, help="Maximum execution steps")
    p_emu.set_defaults(func=cmd_emulate)

    # gen-hook
    p_hook = subparsers.add_parser("gen-hook", help="Generate Frida hook script")
    p_hook.add_argument("--symbol", required=True, help="Target function symbol or hex address")
    p_hook.add_argument("--module", help="Target module name (e.g. ntdll.dll)")
    p_hook.add_argument("--args-count", type=int, default=4, help="Number of arguments to log")
    p_hook.add_argument("--backtrace", action="store_true", help="Log accurate backtrace on enter")
    p_hook.add_argument("--replace-ret", help="Value/pointer to replace return value with")
    p_hook.add_argument("--output", help="Save script to file")
    p_hook.set_defaults(func=cmd_gen_hook)

    # hexdump
    p_hex = subparsers.add_parser("hexdump", help="Display formatted hexadecimal dump")
    p_hex.add_argument("target", help="Hex string or file path")
    p_hex.add_argument("--offset", type=lambda x: int(x, 0), default=0, help="File byte offset")
    p_hex.add_argument("--length", type=lambda x: int(x, 0), default=128, help="Byte length")
    p_hex.add_argument("--base", type=lambda x: int(x, 0), default=0, help="Base address")
    p_hex.set_defaults(func=cmd_hexdump)

    # pipeline
    p_pipe = subparsers.add_parser("pipeline", help="Run dual-stage decoupled generation pipeline")
    p_pipe.add_argument("prompt", help="Scenario or prompt to render")
    p_pipe.add_argument("--model", default="gpt-4o", help="Model name")
    p_pipe.add_argument("--api-key", help="API key (defaults to OPENAI_API_KEY env)")
    p_pipe.add_argument("--base-url", help="API base URL (defaults to OPENAI_BASE_URL env)")
    p_pipe.add_argument("--mock", action="store_true", help="Force offline mock execution")
    p_pipe.add_argument("--json", action="store_true", help="Output full JSON containing blueprint and text")
    p_pipe.set_defaults(func=cmd_pipeline)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
