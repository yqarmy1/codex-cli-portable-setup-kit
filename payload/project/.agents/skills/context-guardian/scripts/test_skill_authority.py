from pathlib import Path
import unittest


SKILL_ROOT = Path(__file__).resolve().parents[1]


class SkillAuthorityBoundaryTests(unittest.TestCase):
    def test_guardian_cannot_create_or_drive_native_goal(self) -> None:
        skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("Guardian never creates, resumes, replaces, or completes", skill)
        self.assertNotIn("call `create_goal`", skill)
        self.assertNotIn("The native\n+Goal is the worker lease", skill)
        self.assertNotIn("must keep\n+dispatching concrete work", skill)

    def test_legacy_rollover_is_non_executable(self) -> None:
        legacy = (
            SKILL_ROOT / "references" / "desktop-rollover.md"
        ).read_text(encoding="utf-8")
        self.assertTrue(legacy.startswith("# Legacy Desktop rollover (disabled)"))
        self.assertIn("Do not execute the transaction below", legacy)

    def test_durable_controller_requires_explicit_typed_goal(self) -> None:
        contract = (
            SKILL_ROOT / "references" / "durable-orchestrator.md"
        ).read_text(encoding="utf-8")
        self.assertIn("explicit typed Goal command", contract)
        self.assertIn("Ordinary user", contract)
        self.assertIn("can never grant Goal", contract)
        self.assertIn("authority.", contract)
        self.assertIn("keep project launchers on native Codex", contract)


if __name__ == "__main__":
    unittest.main()
