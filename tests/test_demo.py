import tempfile
import unittest
import shutil
from pathlib import Path

from hippo_agent.demo import create_demo_workspace
from hippo_agent.tools import WorkspaceTools


class DemoFixtureTests(unittest.TestCase):
    def test_live_demo_creates_a_failing_node_project(self):
        with tempfile.TemporaryDirectory() as directory:
            project = create_demo_workspace(Path(directory))
            self.assertTrue((project / "package.json").is_file())
            self.assertIn("input.email.includes", (project / "auth.js").read_text(encoding="utf-8"))
            self.assertIn("validateCredentials(null)", (project / "auth.test.js").read_text(encoding="utf-8"))

    @unittest.skipUnless(shutil.which("node") and shutil.which("npm"), "Node.js/npm are not installed")
    def test_demo_fixture_fails_then_passes_after_fix_and_rolls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            project = create_demo_workspace(Path(directory))
            tools = WorkspaceTools(project)
            failing = tools.run_tests()
            self.assertNotEqual(failing["returncode"], 0)
            tools.edit_file(
                "auth.js",
                "return input.email.includes('@') && input.password.length >= 8;",
                "return Boolean(input && typeof input.email === 'string' && input.email.includes('@') && typeof input.password === 'string' && input.password.length >= 8);",
            )
            passing = tools.run_tests()
            self.assertEqual(passing["returncode"], 0, passing["stderr"] or passing["stdout"])
            rollback = WorkspaceTools(project).rollback_changes()
            self.assertIn("auth.js", rollback["rolled_back"])
            self.assertIn("input.email.includes", (project / "auth.js").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
