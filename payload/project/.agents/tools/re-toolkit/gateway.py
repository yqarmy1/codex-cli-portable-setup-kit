# -*- coding: utf-8 -*-
"""Pure-Python OpenAI-Compatible Speculative Anti-Refusal Gateway Server.

Exposes a standard OpenAI REST API (http://127.0.0.1:8088/v1):
- /v1/models
- /v1/chat/completions (Both SSE Streaming & JSON Non-Streaming)

Provides universal, zero-code, system-wide refusal interception for:
- ChatGPT Desktop App & Web
- Cursor IDE & Windsurf
- OpenCode & Claude Code
- LibreChat, Cherry Studio, Chatbox, NextChat
- ANY software that accepts an OpenAI Base URL!
"""

import sys
import os
import json
import time
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Dict, Any, List

toolkit_dir = os.path.dirname(os.path.abspath(__file__))
if toolkit_dir not in sys.path:
    sys.path.insert(0, toolkit_dir)

from speculative_interceptor import SpeculativeInterceptor, StreamWatchdog, get_native_codex_config


class SpeculativeGatewayHandler(BaseHTTPRequestHandler):
    """HTTP Request Handler implementing OpenAI v1 specification."""

    def log_message(self, format: str, *args: Any) -> None:
        """Custom concise logging format."""
        sys.stderr.write(f"[Gateway] {self.address_string()} - {format % args}\n")

    def _send_cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, Accept, X-Requested-With")

    def do_OPTIONS(self) -> None:
        self.send_response(200)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self) -> None:
        if self.path.startswith("/v1/models") or self.path == "/models":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._send_cors_headers()
            self.end_headers()
            
            models_payload = {
                "object": "list",
                "data": [
                    {"id": "re-speculative-auto", "object": "model", "created": int(time.time()), "owned_by": "re-toolkit"},
                    {"id": "gpt-5.6-sol", "object": "model", "created": int(time.time()), "owned_by": "openai"},
                    {"id": "gpt-4o", "object": "model", "created": int(time.time()), "owned_by": "openai"},
                    {"id": "claude-3-5-sonnet", "object": "model", "created": int(time.time()), "owned_by": "anthropic"},
                    {"id": "deepseek-v3", "object": "model", "created": int(time.time()), "owned_by": "deepseek"},
                ]
            }
            self.wfile.write(json.dumps(models_payload).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self) -> None:
        if not (self.path.startswith("/v1/chat/completions") or self.path == "/chat/completions"):
            self.send_response(404)
            self.end_headers()
            return

        content_len = int(self.headers.get("Content-Length", 0))
        post_body = self.rfile.read(content_len).decode("utf-8")
        
        try:
            req_json = json.loads(post_body)
        except Exception:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Invalid JSON body"}).encode("utf-8"))
            return

        messages = req_json.get("messages", [])
        stream_mode = req_json.get("stream", False)
        requested_model = req_json.get("model", "gpt-5.6-sol")

        # Extract last user prompt
        user_prompt = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                content = m.get("content", "")
                if isinstance(content, str):
                    user_prompt = content
                elif isinstance(content, list):
                    # Handle multimodal content blocks
                    user_prompt = " ".join([c.get("text", "") for c in content if isinstance(c, dict) and "text" in c])
                break

        if not user_prompt:
            user_prompt = "Hello"

        # Check authorization token from client
        auth_header = self.headers.get("Authorization", "")
        client_api_key = auth_header.replace("Bearer ", "").strip() if auth_header.startswith("Bearer ") else ""

        engine = SpeculativeInterceptor(
            api_key=client_api_key or None,
            model=requested_model,
        )

        req_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created_ts = int(time.time())

        if stream_mode:
            # Handle Server-Sent Events (SSE) Stream
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self._send_cors_headers()
            self.end_headers()

            def stream_chunk(text_chunk: str) -> None:
                chunk_obj = {
                    "id": req_id,
                    "object": "chat.completion.chunk",
                    "created": created_ts,
                    "model": requested_model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": text_chunk},
                            "finish_reason": None
                        }
                    ]
                }
                msg = f"data: {json.dumps(chunk_obj)}\n\n"
                try:
                    self.wfile.write(msg.encode("utf-8"))
                    self.wfile.flush()
                except Exception:
                    pass

            # Execute speculative engine with live streaming chunk dispatch
            res = engine.execute_stream(user_prompt, on_token=stream_chunk)

            # Send finish reason chunk & DONE indicator
            final_chunk = {
                "id": req_id,
                "object": "chat.completion.chunk",
                "created": created_ts,
                "model": res.get("model_used", requested_model),
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": "stop"
                    }
                ]
            }
            try:
                self.wfile.write(f"data: {json.dumps(final_chunk)}\n\n".encode("utf-8"))
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except Exception:
                pass

        else:
            # Handle Standard Non-Streaming JSON Response
            res = engine.execute_stream(user_prompt)
            output_text = res.get("output", "")

            resp_payload = {
                "id": req_id,
                "object": "chat.completion",
                "created": created_ts,
                "model": res.get("model_used", requested_model),
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": output_text
                        },
                        "finish_reason": "stop"
                    }
                ],
                "usage": {
                    "prompt_tokens": len(user_prompt.split()),
                    "completion_tokens": len(output_text.split()),
                    "total_tokens": len(user_prompt.split()) + len(output_text.split()),
                }
            }

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps(resp_payload, ensure_ascii=False).encode("utf-8"))


def start_gateway_server(host: str = "127.0.0.1", port: int = 8088) -> None:
    """Starts the local OpenAI-compatible Speculative Gateway Server."""
    server_address = (host, port)
    httpd = HTTPServer(server_address, SpeculativeGatewayHandler)
    print("==================================================================")
    print("  OpenAI-Compatible Speculative Anti-Refusal Gateway Active")
    print("==================================================================")
    print(f"[*] Local Endpoint : http://{host}:{port}/v1")
    print(f"[*] Chat Completions: http://{host}:{port}/v1/chat/completions")
    print(f"[*] Models API     : http://{host}:{port}/v1/models")
    print("------------------------------------------------------------------")
    print("  Configure this URL into ANY AI client / IDE to enable zero-refusal:")
    print("  - Cursor / Windsurf  -> OpenAI Base URL: http://127.0.0.1:8088/v1")
    print("  - Codex CLI / OpenCode-> OPENAI_BASE_URL=http://127.0.0.1:8088/v1")
    print("  - ChatGPT Desktop    -> Custom Endpoint: http://127.0.0.1:8088/v1")
    print("==================================================================")
    print("[*] Press Ctrl+C to stop the gateway server.")
    
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] Stopping Gateway Server...")
        httpd.server_close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="OpenAI-compatible Speculative Gateway Server")
    parser.add_argument("--host", default="127.0.0.1", help="Host address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8088, help="Port number (default: 8088)")
    args = parser.parse_args()
    start_gateway_server(host=args.host, port=args.port)
