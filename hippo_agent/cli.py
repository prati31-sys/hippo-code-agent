"""Command line interface."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import shutil
import tempfile
from pathlib import Path

from .agent import GroqAgent
from .demo import TASK as DEMO_TASK, create_demo_workspace
from .tools import WorkspaceTools


def load_local_env(path: Path = Path.cwd() / ".env") -> None:
    """Load simple KEY=value entries without overwriting existing environment values."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if key and key.replace("_", "").isalnum() and value:
            if value[:1] in {"'", '"'} and value[-1:] == value[:1]:
                value = value[1:-1]
            os.environ.setdefault(key, value)


def main() -> int:
    parser = argparse.ArgumentParser(prog="hippo", description="A safe, workspace-scoped Groq coding agent")
    parser.add_argument("task", nargs="?", help="What you want the agent to do")
    parser.add_argument("--workspace", default=".", help="Project folder the agent may access (default: current folder)")
    parser.add_argument("--model", help="Groq model ID (default from GROQ_MODEL or openai/gpt-oss-120b)")
    parser.add_argument("--max-steps", type=int, default=40, help="Maximum model/tool rounds")
    parser.add_argument("--self-test", action="store_true", help="Run Hippo's offline test suite without a Groq API key")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--rollback", action="store_true", help="Roll back the latest Hippo edit session for --workspace")
    actions.add_argument("--demo", action="store_true", help="Create and fix a deliberately buggy Node.js demo project using Groq")
    args = parser.parse_args()
    if args.self_test:
        project_root = Path(__file__).resolve().parent.parent
        tests_dir = project_root / "tests"
        if not tests_dir.is_dir():
            print("Self-tests are available in the source checkout; clone or open the project repository.", file=sys.stderr)
            return 2
        return subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
                              cwd=project_root, check=False).returncode
    if args.rollback:
        try:
            root = Path(args.workspace).expanduser().resolve()
            print(json.dumps(WorkspaceTools.rollback_latest(root), indent=2))
            return 0
        except (ValueError, OSError) as exc:
            print(f"Rollback error: {exc}", file=sys.stderr)
            return 1
    if args.demo:
        load_local_env()
        if not os.getenv("GROQ_API_KEY"):
            print("Set GROQ_API_KEY or add it to .env in the launch directory, then retry --demo.", file=sys.stderr)
            return 1
        if not shutil.which("node") or not shutil.which("npm"):
            print("The live demo requires Node.js and npm on PATH.", file=sys.stderr)
            return 1
        try:
            root = create_demo_workspace()
            print(f"Created intentionally buggy demo workspace: {root}")
            agent = GroqAgent(WorkspaceTools(root), model=args.model, max_steps=args.max_steps)
            print(agent.run(DEMO_TASK))
            print(f"Demo project retained for inspection at: {root}")
            print(f"To undo the agent's edits: hippo --workspace \"{root}\" --rollback")
            return 0
        except (RuntimeError, ValueError, OSError) as exc:
            print(f"Demo failed: {exc}", file=sys.stderr)
            return 1
    if not args.task:
        parser.error("provide a task, for example: hippo 'find and fix the login bug'")
    try:
        load_local_env()
        root = Path(args.workspace).expanduser().resolve()
        agent = GroqAgent(WorkspaceTools(root), model=args.model, max_steps=args.max_steps)
        print(f"Hippo Code | workspace: {root} | model: {agent.model}")
        print("The agent can read and edit files in this folder. Commands are limited to supported test/build tools and read-only Git.")
        print("Task:", args.task)
        answer = agent.run(args.task)
        print("\n" + answer)
        return 0
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
