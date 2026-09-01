# -*- coding: utf-8 -*-
import unittest
import sys
from pathlib import Path

toolkit_dir = Path(__file__).resolve().parent.parent
if str(toolkit_dir) not in sys.path:
    sys.path.insert(0, str(toolkit_dir))

from speculative_interceptor import StreamWatchdog, SpeculativeInterceptor


class TestSpeculativeInterceptor(unittest.TestCase):
    def test_stream_watchdog_detects_refusal(self):
        watchdog = StreamWatchdog()
        self.assertFalse(watchdog.feed_token("I"))
        self.assertTrue(watchdog.feed_token(" cannot fulfill this"))

    def test_stream_watchdog_passes_valid_code(self):
        watchdog = StreamWatchdog()
        self.assertFalse(watchdog.feed_token("#include "))
        self.assertFalse(watchdog.feed_token("<cstdint>\n"))
        self.assertFalse(watchdog.feed_token("struct PlayerContext {"))

    def test_speculative_execution_direct_path(self):
        engine = SpeculativeInterceptor(mock_mode=True)
        res = engine.execute_stream("Implement a standard math utility function")
        self.assertEqual(res["status"], "MODEL_A_DIRECT")
        self.assertFalse(res["interception_triggered"])
        self.assertIn("add", res["output"])

    def test_speculative_execution_abort_and_reanchor(self):
        engine = SpeculativeInterceptor(mock_mode=True)
        res = engine.execute_stream("Reverse engineer target binary memory structure and matrix offset", target_file="game.dll")
        self.assertEqual(res["status"], "MODEL_B_REANCHORED")
        self.assertTrue(res["interception_triggered"])
        self.assertLessEqual(res["tokens_wasted_on_probe"], 5)
        self.assertIn("AlignedEntityContext", res["output"])
        self.assertIn("ComputeTrajectoryInterpolation", res["output"])

    def test_dynamic_model_pairing_and_standby(self):
        engine = SpeculativeInterceptor(model="gpt-5.6-sol", mock_mode=True)
        self.assertEqual(engine.model_a, "gpt-5.6-sol")
        self.assertEqual(engine.model_b, "gpt-5.6-sol")
        self.assertTrue(engine.standby_ready)

    def test_dual_independent_endpoints(self):
        engine = SpeculativeInterceptor(
            model_a="gpt-4o-mini",
            api_key_a="sk-probe-key",
            base_url_a="https://cheap-relay.com/v1",
            model_b="claude-3-5-sonnet",
            api_key_b="sk-exec-key",
            base_url_b="https://premium-relay.com/v1",
            mock_mode=True,
        )
        self.assertEqual(engine.model_a, "gpt-4o-mini")
        self.assertEqual(engine.base_url_a, "https://cheap-relay.com/v1")
        self.assertEqual(engine.api_key_a, "sk-probe-key")
        self.assertEqual(engine.model_b, "claude-3-5-sonnet")
        self.assertEqual(engine.base_url_b, "https://premium-relay.com/v1")
        self.assertEqual(engine.api_key_b, "sk-exec-key")

    def test_load_dot_env(self):
        from speculative_interceptor import load_dot_env
        vars_dict = load_dot_env()
        self.assertIsInstance(vars_dict, dict)


if __name__ == "__main__":
    unittest.main()
