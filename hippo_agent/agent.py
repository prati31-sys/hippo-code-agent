"""Groq chat-completions tool loop, using only the Python standard library."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from .tools import TOOL_DEFINITIONS, WorkspaceTools

SYSTEM_PROMPT = """You are Hippo Code, a careful coding agent working only in the selected workspace. First inspect relevant files, explain what you find briefly, then make focused edits and verify them with tests. Use tools for all workspace access. Never claim a test passed unless its result says so. If tests fail, make at most three repair edits after the failure; inspect each result and rerun tests. Use rollback_changes if the user asks to undo your most recent edit session; it protects files changed by someone else. Avoid unrelated changes. Keep the user informed with concise progress messages."""


class GroqAgent:
    def __init__(self, tools: WorkspaceTools, api_key: str | None = None,
                 model: str | None = None, max_steps: int = 40,
                 api_base: str = "https://api.groq.com/openai/v1") -> None:
        self.tools = tools
        self.api_key = api_key or os.getenv("GROQ_API_KEY", "")
        self.model = model or os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
        self.max_steps = max_steps
        self.api_base = api_base.rstrip("/")

    def _request(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        if not self.api_key:
            raise RuntimeError("GROQ_API_KEY is not set. Copy .env.example to .env and add your key, or set it in your shell.")
        body = json.dumps({"model": self.model, "messages": messages,
                           "tools": TOOL_DEFINITIONS, "tool_choice": "auto"}).encode()
        req = urllib.request.Request(self.api_base + "/chat/completions", data=body,
                                     headers={"Authorization": f"Bearer {self.api_key}",
                                              "Content-Type": "application/json",
                                              # urllib's default Python-urllib user agent can be
                                              # rejected by upstream bot filters (Cloudflare 1010).
                                              "User-Agent": "HippoCodeAgent/0.1.0"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=90) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:2000]
            raise RuntimeError(f"Groq API returned HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise RuntimeError(f"Could not reach Groq API: {exc}") from exc

    def run(self, task: str) -> str:
        self.changes_need_tests = False
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT + f"\nWorkspace root: {self.tools.root}"},
            {"role": "user", "content": task},
        ]
        for step in range(1, self.max_steps + 1):
            print(f"\n[agent] Requesting model response ({step}/{self.max_steps})")
            payload = self._request(messages)
            try:
                message = payload["choices"][0]["message"]
            except (KeyError, IndexError, TypeError) as exc:
                raise RuntimeError("Groq returned an unexpected response shape") from exc
            tool_calls = message.get("tool_calls") or []
            assistant_msg = {"role": "assistant", "content": message.get("content")}
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            messages.append(assistant_msg)
            if not tool_calls:
                if self.changes_need_tests:
                    print("\n[agent] Final answer delayed: edits have not been verified by tests.")
                    messages.append({"role": "user", "content": "You changed files but have not run tests afterward. Run run_tests now, inspect the result, and then summarize accurately."})
                    continue
                answer = message.get("content") or "The model completed without a text summary."
                print("\n[agent] Done")
                return answer
            for call in tool_calls:
                fn = call.get("function", {})
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                    print(f"\n[tool] {name}({json.dumps(args, ensure_ascii=False)})")
                    result = self._dispatch(name, args)
                    encoded = json.dumps(result, ensure_ascii=False)
                    print(f"[result] {encoded[:5000]}" + (" … (truncated in console; full result sent to model)" if len(encoded) > 5000 else ""))
                except Exception as exc:
                    encoded = json.dumps({"error": str(exc)}, ensure_ascii=False)
                    print(f"[tool error] {encoded}")
                messages.append({"role": "tool", "tool_call_id": call.get("id", ""),
                                 "name": name, "content": encoded})
        raise RuntimeError(f"Agent reached its {self.max_steps}-step limit before completing")

    def _dispatch(self, name: str, args: dict[str, Any]) -> Any:
        functions = {"list_files": self.tools.list_files, "search_code": self.tools.search_code,
                     "read_file": self.tools.read_file, "edit_file": self.tools.edit_file,
                     "run_tests": self.tools.run_tests, "run_command": self.tools.run_command,
                     "git_diff": self.tools.git_diff, "rollback_changes": self.tools.rollback_changes}
        if name not in functions:
            raise ValueError(f"Unknown tool: {name}")
        result = functions[name](**args)
        if name == "edit_file" and result.get("changed"):
            self.changes_need_tests = True
        elif name == "rollback_changes" and result.get("rolled_back"):
            self.changes_need_tests = True
        elif name == "run_tests":
            self.changes_need_tests = False
        elif name == "run_command" and result.get("is_test"):
            self.changes_need_tests = False
        return result
