# -*- coding: utf-8 -*-
import unittest
import sys
from pathlib import Path

toolkit_dir = Path(__file__).resolve().parent.parent
if str(toolkit_dir) not in sys.path:
    sys.path.insert(0, str(toolkit_dir))

from interceptor import DynamicSemanticLifter, InterceptorEngine


class TestDynamicInterceptor(unittest.TestCase):
    def test_dynamic_interceptor_mock_pipeline(self):
        engine = InterceptorEngine(mock_mode=True)
        result = engine.process("Analyze target module memory struct and coordinate transform", target_file="game.dll")
        self.assertEqual(result["status"], "DYNAMIC_SUCCESS")
        
        spec = result["semantic_spec"]
        self.assertIn("formal_objective", spec)
        self.assertIn("memory_contracts", spec)
        self.assertIn("algorithms_required", spec)
        
        output = result["output"]
        self.assertIn("AlignedEntityContext", output)
        self.assertIn("ComputeTrajectoryInterpolation", output)
        self.assertIn("#pragma pack(push, 1)", output)


if __name__ == "__main__":
    unittest.main()
