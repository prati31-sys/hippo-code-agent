import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hippo_agent.tools import WorkspaceTools


class WorkspaceToolsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "src").mkdir()
        (self.root / "src" / "app.py").write_text("def login():\n    return False\n", encoding="utf-8")
        self.tools = WorkspaceTools(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_list_search_read(self):
        self.assertIn("src/app.py", self.tools.list_files()["files"])
        self.assertEqual(self.tools.search_code("login")["matches"][0]["line"], 1)
        self.assertIn("return False", self.tools.read_file("src/app.py")["content"])

    def test_edit_requires_unique_match_and_returns_diff(self):
        result = self.tools.edit_file("src/app.py", "return False", "return True")
        self.assertTrue(result["changed"])
        self.assertIn("-    return False", result["diff"])
        with self.assertRaisesRegex(ValueError, "exactly once"):
            self.tools.edit_file("src/app.py", "missing", "replacement")

    def test_paths_cannot_escape_workspace(self):
        with self.assertRaisesRegex(ValueError, "outside"):
            self.tools.read_file("../outside.txt")

    def test_command_policy_rejects_shell_and_arbitrary_execution(self):
        with self.assertRaisesRegex(ValueError, "Shell operators"):
            self.tools.run_command("python -m unittest; whoami")
        with self.assertRaisesRegex(ValueError, "not on the permitted list"):
            self.tools.run_command("python -c print(1)")
        with patch.object(self.tools, "_run_argv", return_value={"returncode": 0, "stdout": "ok", "stderr": ""}):
            self.assertTrue(self.tools.run_command("go test ./...")["is_test"])
        with self.assertRaisesRegex(ValueError, "not on the permitted list"):
            self.tools.run_command("go clean -cache")

    def test_repair_limit_after_failed_tests(self):
        self.tools.last_test_failed = True
        self.tools.edit_file("src/app.py", "return False", "return True")
        self.tools.edit_file("src/app.py", "return True", "return 'ok'")
        self.tools.edit_file("src/app.py", "return 'ok'", "return None")
        with self.assertRaisesRegex(ValueError, "Repair limit"):
            self.tools.edit_file("src/app.py", "return None", "too many")

    def test_rollback_restores_agent_changes_and_keeps_external_edits(self):
        self.tools.edit_file("src/app.py", "return False", "return True")
        self.tools.edit_file("new.txt", "", "created by agent")
        self.tools.edit_file("external.txt", "", "agent version")
        (self.root / "external.txt").write_text("human change", encoding="utf-8")
        # Rollback still works from a fresh process-scoped tools object.
        result = WorkspaceTools(self.root).rollback_changes()
        self.assertFalse((self.root / "new.txt").exists())
        self.assertEqual((self.root / "src" / "app.py").read_text(encoding="utf-8"), "def login():\n    return False\n")
        self.assertEqual((self.root / "external.txt").read_text(encoding="utf-8"), "human change")
        self.assertIn("src/app.py", result["rolled_back"])
        self.assertIn("new.txt", result["rolled_back"])
        self.assertEqual(result["conflicts"][0]["path"], "external.txt")

    def test_edit_preserves_crlf_line_endings(self):
        path = self.root / "windows.txt"
        path.write_bytes(b"first\r\nsecond\r\n")
        self.tools.edit_file("windows.txt", "second", "changed")
        self.assertEqual(path.read_bytes(), b"first\r\nchanged\r\n")

    def test_run_tests_detects_go_rust_dotnet_maven_and_gradle(self):
        markers = [("go.mod", ["go", "test", "./..."]), ("Cargo.toml", ["cargo", "test"]),
                   ("app.csproj", ["dotnet", "test"]), ("pom.xml", ["mvn", "test"])]
        for marker, command in markers:
            project = self.root / marker.replace(".", "_")
            project.mkdir()
            (project / marker).write_text("", encoding="utf-8")
            tools = WorkspaceTools(project)
            with patch.object(tools, "_run_argv", return_value={"returncode": 0, "stdout": "ok", "stderr": ""}) as run:
                tools.run_tests()
            self.assertEqual(run.call_args.args[0], command)
        gradle = self.root / "gradle_project"
        gradle.mkdir()
        wrapper = "gradlew.bat" if os.name == "nt" else "gradlew"
        (gradle / wrapper).write_text("", encoding="utf-8")
        tools = WorkspaceTools(gradle)
        with patch.object(tools, "_run_argv", return_value={"returncode": 0, "stdout": "ok", "stderr": ""}) as run:
            tools.run_tests()
        self.assertEqual(run.call_args.args[0], ["./gradlew", "test"] if os.name != "nt" else ["gradlew.bat", "test"])

    def test_run_tests_does_not_report_success_when_no_suite_is_configured(self):
        result = self.tools.run_tests()
        self.assertEqual(result["returncode"], 2)
        self.assertFalse(result["tests_found"])
        self.assertIn("no tests were run", result["stdout"])

    def test_run_tests_uses_node_project_lockfile_package_manager(self):
        for lockfile, manager in [("package-lock.json", "npm"), ("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn")]:
            project = self.root / manager
            project.mkdir()
            (project / "package.json").write_text("{}", encoding="utf-8")
            (project / lockfile).write_text("", encoding="utf-8")
            tools = WorkspaceTools(project)
            with patch.object(tools, "_run_argv", return_value={"returncode": 0, "stdout": "ok", "stderr": ""}) as run:
                tools.run_tests()
            self.assertEqual(run.call_args.args[0], [manager, "test"])


if __name__ == "__main__":
    unittest.main()
