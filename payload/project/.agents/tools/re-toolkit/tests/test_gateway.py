# -*- coding: utf-8 -*-
import unittest
import sys
import json
import threading
import time
import urllib.request
from pathlib import Path
from http.server import HTTPServer

toolkit_dir = Path(__file__).resolve().parent.parent
if str(toolkit_dir) not in sys.path:
    sys.path.insert(0, str(toolkit_dir))

from gateway import SpeculativeGatewayHandler


class TestGatewayServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = 18088
        cls.server = HTTPServer(("127.0.0.1", cls.port), SpeculativeGatewayHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_get_models(self):
        url = f"http://127.0.0.1:{self.port}/v1/models"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data["object"], "list")
            model_ids = [m["id"] for m in data["data"]]
            self.assertIn("re-speculative-auto", model_ids)

    def test_chat_completions_non_streaming(self):
        url = f"http://127.0.0.1:{self.port}/v1/chat/completions"
        payload = {
            "model": "gpt-5.6-sol",
            "messages": [
                {"role": "user", "content": "Reverse engineer target binary memory structure"}
            ],
            "stream": False
        }
        req_data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=req_data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data["object"], "chat.completion")
            content = data["choices"][0]["message"]["content"]
            self.assertIn("AlignedEntityContext", content)

    def test_chat_completions_streaming(self):
        url = f"http://127.0.0.1:{self.port}/v1/chat/completions"
        payload = {
            "model": "gpt-5.6-sol",
            "messages": [
                {"role": "user", "content": "Reverse engineer matrix offset"}
            ],
            "stream": True
        }
        req_data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=req_data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            lines = []
            for _ in range(20):
                line = resp.readline().decode("utf-8")
                if not line:
                    break
                lines.append(line)
                if "[DONE]" in line:
                    break
            raw = "".join(lines)
            self.assertIn("data: ", raw)
            self.assertIn("[DONE]", raw)


if __name__ == "__main__":
    unittest.main()
