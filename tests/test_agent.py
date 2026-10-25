import io
import json
import tempfile
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout
from pathlib import Path

from hippo_agent.agent import GroqAgent
from hippo_agent.tools import WorkspaceTools


class ScriptedAgent(GroqAgent):
    def __init__(self, tools):
        super().__init__(tools, api_key="test-key", max_steps=3)
        self.responses = [
            {"choices": [{"message": {"content": None, "tool_calls": [{
                "id": "call-1", "type": "function", "function": {
                    "name": "list_files", "arguments": "{}"}}]}}]},
            {"choices": [{"message": {"content": "I inspected the workspace."}}]},
        ]
        self.tool_results = []

    def _request(self, messages):
        self.tool_results = [m["content"] for m in messages if m.get("role") == "tool"]
        return self.responses.pop(0)


class VerificationGateAgent(GroqAgent):
    def __init__(self, tools):
        super().__init__(tools, api_key="test-key", max_steps=5)
        self.responses = [
            {"choices": [{"message": {"content": None, "tool_calls": [{
                "id": "edit-1", "type": "function", "function": {
                    "name": "edit_file", "arguments": json.dumps({"path": "new.txt", "old_text": "", "new_text": "created"})}}]}}]},
            {"choices": [{"message": {"content": "Done; no tests needed."}}]},
            {"choices": [{"message": {"content": None, "tool_calls": [{
                "id": "test-1", "type": "function", "function": {
                    "name": "run_tests", "arguments": "{}"}}]}}]},
            {"choices": [{"message": {"content": "Done; tests passed."}}]},
        ]

    def _request(self, messages):
        return self.responses.pop(0)


class AgentLoopTests(unittest.TestCase):
    def test_tool_call_round_trip_and_final_answer(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "hello.txt").write_text("hello", encoding="utf-8")
            agent = ScriptedAgent(WorkspaceTools(directory))
            output = io.StringIO()
            with redirect_stdout(output):
                answer = agent.run("List files")
            self.assertEqual(answer, "I inspected the workspace.")
            self.assertIn("hello.txt", json.loads(agent.tool_results[0])["files"])
            self.assertIn("[tool] list_files", output.getvalue())

    def test_agent_requires_tests_after_a_file_change(self):
        with tempfile.TemporaryDirectory() as directory:
            tools = WorkspaceTools(directory)
            tests_run = []
            tools.run_tests = lambda: tests_run.append(True) or {"returncode": 0, "stdout": "OK", "stderr": ""}
            agent = VerificationGateAgent(tools)
            output = io.StringIO()
            with redirect_stdout(output):
                answer = agent.run("Create a file and verify it")
            self.assertEqual(answer, "Done; tests passed.")
            self.assertEqual(tests_run, [True])
            self.assertIn("Final answer delayed", output.getvalue())

    @patch("hippo_agent.agent.urllib.request.urlopen")
    def test_groq_request_uses_auth_chat_endpoint_and_tool_schemas(self, urlopen):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"choices":[{"message":{"content":"ready"}}]}'

        urlopen.return_value = FakeResponse()
        with tempfile.TemporaryDirectory() as directory:
            agent = GroqAgent(WorkspaceTools(directory), api_key="example-secret", model="test-model")
            result = agent._request([{"role": "user", "content": "hello"}])
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.groq.com/openai/v1/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer example-secret")
        self.assertEqual(request.get_header("User-agent"), "HippoCodeAgent/0.1.0")
        self.assertEqual(result["choices"][0]["message"]["content"], "ready")
        import json
        payload = json.loads(request.data)
        self.assertEqual(payload["model"], "test-model")
        self.assertEqual(payload["tools"][0]["function"]["name"], "list_files")


if __name__ == "__main__":
    unittest.main()
