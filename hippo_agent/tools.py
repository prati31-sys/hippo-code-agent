"""Workspace-scoped tools exposed to the model."""
from __future__ import annotations

import difflib
import getpass
import hashlib
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

IGNORED = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache"}
MAX_FILE_BYTES = 500_000


def _journal_root_for(workspace: Path) -> Path:
    workspace_id = hashlib.sha256(str(workspace).encode("utf-8")).hexdigest()[:24]
    owner = str(os.getuid()) if hasattr(os, "getuid") else getpass.getuser()
    owner_id = hashlib.sha256(owner.encode("utf-8")).hexdigest()[:12]
    return Path(tempfile.gettempdir()) / "hippo-agent" / owner_id / workspace_id / "sessions"


def _private_directory(path: Path) -> None:
    if path.is_symlink():
        raise ValueError(f"Refusing to use a symbolic link for rollback data: {path}")
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        path.chmod(0o700)


class WorkspaceTools:
    def __init__(self, root: str | Path, command_timeout: int = 120) -> None:
        self.root = Path(root).expanduser().resolve()
        if not self.root.is_dir():
            raise ValueError(f"Workspace does not exist or is not a directory: {self.root}")
        self.command_timeout = command_timeout
        self.last_test_failed = False
        self.repairs_used = 0
        self._journal_root = _journal_root_for(self.root)
        self._session_dir = self._journal_root / uuid.uuid4().hex
        self._manifest_path = self._session_dir / "manifest.json"

    @staticmethod
    def _sha256(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def _record_edit(self, relative: str, original: bytes | None, updated: bytes) -> None:
        path = self._journal_root
        chain = list(reversed([path, *path.parents[:3]]))
        for directory in chain:
            _private_directory(directory)
        _private_directory(self._session_dir)
        if self._manifest_path.is_file():
            manifest = json.loads(self._manifest_path.read_text(encoding="utf-8"))
        else:
            manifest = {"workspace": str(self.root), "status": "active", "files": {}}
        files = manifest["files"]
        if relative not in files:
            backup_name = hashlib.sha256(relative.encode("utf-8")).hexdigest() + ".bin"
            backup_path = self._session_dir / backup_name
            if original is not None:
                backup_path.write_bytes(original)
                if os.name != "nt":
                    backup_path.chmod(0o600)
            files[relative] = {
                "original_exists": original is not None,
                "original_sha256": self._sha256(original) if original is not None else None,
                "backup": backup_name if original is not None else None,
            }
        files[relative]["after_sha256"] = self._sha256(updated)
        temporary_manifest = self._manifest_path.with_suffix(".tmp")
        temporary_manifest.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        if os.name != "nt":
            temporary_manifest.chmod(0o600)
        temporary_manifest.replace(self._manifest_path)

    @classmethod
    def rollback_latest(cls, root: str | Path) -> dict[str, Any]:
        workspace = Path(root).expanduser().resolve()
        if not workspace.is_dir():
            raise ValueError("Workspace does not exist or is not a directory")
        journal_root = _journal_root_for(workspace)
        if not journal_root.is_dir():
            return {"rolled_back": [], "unchanged": [], "conflicts": [], "message": "No Hippo edit history was found."}
        if journal_root.is_symlink():
            raise ValueError("Rollback journal path is a symbolic link")
        sessions = sorted((p for p in journal_root.iterdir() if not p.is_symlink() and p.is_dir()
                           and (p / "manifest.json").is_file() and not (p / "manifest.json").is_symlink()),
                          key=lambda p: p.stat().st_mtime, reverse=True)
        session = next((p for p in sessions if json.loads((p / "manifest.json").read_text(encoding="utf-8")).get("status") == "active"), None)
        if session is None:
            return {"rolled_back": [], "unchanged": [], "conflicts": [], "message": "No active Hippo edits can be rolled back."}
        manifest_path = session / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if Path(manifest.get("workspace", "")).resolve() != workspace:
            raise ValueError("Rollback journal workspace does not match")
        rolled_back, unchanged, conflicts = [], [], []
        for relative, entry in reversed(list(manifest["files"].items())):
            target = (workspace / relative).resolve()
            try:
                target.relative_to(workspace)
            except ValueError:
                conflicts.append({"path": relative, "reason": "path now resolves outside workspace"})
                continue
            current = target.read_bytes() if target.is_file() else None
            current_hash = cls._sha256(current) if current is not None else None
            if current_hash == entry.get("original_sha256") and entry.get("original_exists"):
                unchanged.append(relative)
                continue
            if current is None and not entry.get("original_exists"):
                unchanged.append(relative)
                continue
            if current_hash != entry.get("after_sha256"):
                conflicts.append({"path": relative, "reason": "file changed since Hippo edited it; left untouched"})
                continue
            if entry.get("original_exists"):
                backup = session / entry["backup"]
                try:
                    backup.resolve().relative_to(session.resolve())
                except ValueError:
                    conflicts.append({"path": relative, "reason": "rollback backup path is invalid"})
                    continue
                if backup.is_symlink() or not backup.is_file():
                    conflicts.append({"path": relative, "reason": "rollback backup is missing or unsafe"})
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(backup.read_bytes())
            else:
                target.unlink()
            rolled_back.append(relative)
        if not conflicts:
            manifest["status"] = "rolled_back"
        temporary_manifest = manifest_path.with_suffix(".tmp")
        temporary_manifest.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        if os.name != "nt":
            temporary_manifest.chmod(0o600)
        temporary_manifest.replace(manifest_path)
        return {"rolled_back": rolled_back, "unchanged": unchanged, "conflicts": conflicts,
                "message": "Rollback finished." if not conflicts else "Some files changed after the agent edit and were left untouched."}

    def _path(self, relative: str) -> Path:
        candidate = (self.root / relative).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("Path is outside the selected workspace") from exc
        return candidate

    def list_files(self, path: str = ".", limit: int = 200) -> dict[str, Any]:
        base = self._path(path)
        if not base.is_dir():
            raise ValueError("Requested path is not a directory")
        results = []
        for current, dirs, files in os.walk(base):
            dirs[:] = sorted(d for d in dirs if d not in IGNORED)
            for name in sorted(files):
                file_path = Path(current) / name
                results.append(file_path.relative_to(self.root).as_posix())
                if len(results) >= max(1, min(limit, 1000)):
                    return {"files": results, "truncated": True}
        return {"files": results, "truncated": False}

    def search_code(self, query: str, path: str = ".", limit: int = 50) -> dict[str, Any]:
        if not query:
            raise ValueError("Search query cannot be empty")
        base = self._path(path)
        hits = []
        for current, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in IGNORED]
            for name in files:
                file_path = Path(current) / name
                try:
                    # Do not follow file symlinks that point outside the workspace.
                    file_path.resolve().relative_to(self.root)
                    if file_path.stat().st_size > MAX_FILE_BYTES:
                        continue
                    lines = file_path.read_text(encoding="utf-8").splitlines()
                except (OSError, UnicodeError, ValueError):
                    continue
                for number, line in enumerate(lines, 1):
                    if query.casefold() in line.casefold():
                        hits.append({"file": file_path.relative_to(self.root).as_posix(), "line": number, "text": line[:500]})
                        if len(hits) >= max(1, min(limit, 500)):
                            return {"matches": hits, "truncated": True}
        return {"matches": hits, "truncated": False}

    def read_file(self, path: str, start_line: int = 1, end_line: int | None = None) -> dict[str, Any]:
        file_path = self._path(path)
        if not file_path.is_file():
            raise ValueError("File does not exist")
        if file_path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError("File exceeds the 500 KB read limit")
        lines = file_path.read_text(encoding="utf-8").splitlines()
        start = max(1, start_line)
        end = min(len(lines), end_line if end_line is not None else len(lines))
        return {"path": file_path.relative_to(self.root).as_posix(), "start_line": start, "end_line": end,
                "content": "\n".join(f"{i}: {lines[i-1]}" for i in range(start, end + 1))}

    def edit_file(self, path: str, old_text: str, new_text: str) -> dict[str, Any]:
        file_path = self._path(path)
        if self.last_test_failed and self.repairs_used >= 3:
            raise ValueError("Repair limit reached: at most 3 edit attempts after failed tests")
        if file_path.exists() and not file_path.is_file():
            raise ValueError("Target is not a file")
        if file_path.exists() and file_path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError("File exceeds the 500 KB edit limit")
        original_bytes = file_path.read_bytes() if file_path.exists() else None
        original = original_bytes.decode("utf-8") if original_bytes is not None else ""
        newline = "\r\n" if "\r\n" in original else "\n"
        original = original.replace("\r\n", "\n")
        if old_text:
            occurrences = original.count(old_text)
            if occurrences != 1:
                raise ValueError(f"Expected old_text exactly once; found {occurrences}")
            updated = original.replace(old_text, new_text, 1)
        else:
            if original:
                raise ValueError("For an existing file, provide exact old_text to replace")
            updated = new_text
        file_path.parent.mkdir(parents=True, exist_ok=True)
        updated = updated.replace("\r\n", "\n")
        if newline == "\r\n":
            updated = updated.replace("\n", "\r\n")
        updated_bytes = updated.encode("utf-8")
        self._record_edit(file_path.relative_to(self.root).as_posix(), original_bytes, updated_bytes)
        file_path.write_bytes(updated_bytes)
        if self.last_test_failed:
            self.repairs_used += 1
        diff_updated = updated.replace("\r\n", "\n")
        diff = "".join(difflib.unified_diff(original.splitlines(True), diff_updated.splitlines(True),
                                             fromfile=f"a/{path}", tofile=f"b/{path}"))
        return {"path": file_path.relative_to(self.root).as_posix(), "changed": original_bytes != updated_bytes,
                "repair_attempt": self.repairs_used if self.last_test_failed else None, "diff": diff[:12000]}

    def git_diff(self) -> dict[str, Any]:
        return self._run_argv(["git", "diff", "--"], expect_git=True)

    def rollback_changes(self) -> dict[str, Any]:
        return self.rollback_latest(self.root)

    def run_tests(self) -> dict[str, Any]:
        if (self.root / "package.json").exists():
            manager = "pnpm" if (self.root / "pnpm-lock.yaml").exists() else "yarn" if (self.root / "yarn.lock").exists() else "npm"
            result = self._run_argv([manager, "test"])
        elif (self.root / "pyproject.toml").exists():
            if importlib.util.find_spec("pytest"):
                result = self._run_argv([sys.executable, "-m", "pytest", "-q"])
            else:
                result = self._run_argv([sys.executable, "-m", "unittest", "discover", "-v"])
        elif (self.root / "go.mod").exists():
            result = self._run_argv(["go", "test", "./..."])
        elif (self.root / "Cargo.toml").exists():
            result = self._run_argv(["cargo", "test"])
        elif list(self.root.glob("*.sln")) or list(self.root.glob("*.csproj")):
            result = self._run_argv(["dotnet", "test"])
        elif (self.root / "pom.xml").exists():
            result = self._run_argv(["mvn", "test"])
        elif (self.root / "gradlew").exists() or (self.root / "gradlew.bat").exists():
            wrapper = "gradlew.bat" if os.name == "nt" else "./gradlew"
            result = self._run_argv([wrapper, "test"])
        elif (self.root / "tests").exists():
            if importlib.util.find_spec("pytest"):
                result = self._run_argv([sys.executable, "-m", "pytest", "-q"])
            else:
                result = self._run_argv([sys.executable, "-m", "unittest", "discover", "-v"])
        else:
            result = {"returncode": 2, "stdout": "No recognized test configuration found; no tests were run.", "stderr": "",
                      "command": "(none)", "tests_found": False}
        if result.get("returncode") == 0 and "Ran 0 tests" in (result.get("stdout", "") + result.get("stderr", "")):
            result["returncode"] = 2
            result["tests_found"] = False
            result["stderr"] = (result.get("stderr", "") + "\nNo tests were discovered.").strip()
        elif result.get("returncode") == 127:
            result["tests_found"] = False
        else:
            result.setdefault("tests_found", True)
        self.last_test_failed = result["returncode"] != 0
        return result

    def run_command(self, command: str) -> dict[str, Any]:
        """Allow only a small set of safe test/build and read-only git commands."""
        if re.search(r"[|;&<>`\n\r]", command) or "$(`" in command:
            raise ValueError("Shell operators and multi-command strings are not permitted")
        try:
            argv = shlex.split(command, posix=os.name != "nt")
        except ValueError as exc:
            raise ValueError(f"Could not parse command: {exc}") from exc
        if not argv:
            raise ValueError("Command cannot be empty")
        exe = Path(argv[0]).name.lower()
        allowed = ((exe in {"python", "python.exe", "py"} and len(argv) >= 3 and argv[1:3] in (["-m", "unittest"], ["-m", "pytest"]))
                   or (exe in {"node", "node.exe"} and len(argv) >= 2 and argv[1] in {"--test", "--check"})
                   or (exe in {"npm", "npm.cmd", "pnpm", "pnpm.cmd", "yarn", "yarn.cmd"} and len(argv) >= 2 and argv[1] in {"test", "run"} and (argv[1] != "run" or (len(argv) > 2 and argv[2] in {"test", "build", "lint", "typecheck"})))
                   or (exe == "go" and len(argv) >= 3 and argv[1:3] == ["test", "./..."])
                   or (exe == "cargo" and len(argv) >= 2 and argv[1] == "test")
                   or (exe == "dotnet" and len(argv) >= 2 and argv[1] == "test")
                   or (exe in {"mvn", "mvn.cmd"} and len(argv) >= 2 and argv[1] == "test")
                   or (exe in {"gradlew", "gradlew.bat"} and len(argv) >= 2 and argv[1] == "test")
                   or (exe in {"git", "git.exe"} and len(argv) >= 2 and argv[1] in {"diff", "status", "log"}))
        if not allowed:
            raise ValueError("Command is not on the permitted list (supported test/build tools and read-only Git)")
        if exe in {"python", "python.exe", "py"}:
            argv[0] = sys.executable
        result = self._run_argv(argv)
        result["is_test"] = self._is_test_argv(argv, exe)
        if result["is_test"]:
            self.last_test_failed = result["returncode"] != 0
        return result

    @staticmethod
    def _is_test_argv(argv: list[str], exe: str) -> bool:
        return (
            (exe in {"python", "python.exe", "py"} and len(argv) >= 3 and argv[1:3] in (["-m", "unittest"], ["-m", "pytest"]))
            or (exe in {"node", "node.exe"} and len(argv) >= 2 and argv[1] == "--test")
            or (exe in {"npm", "npm.cmd", "pnpm", "pnpm.cmd", "yarn", "yarn.cmd"} and len(argv) >= 2 and (argv[1] == "test" or (argv[1] == "run" and len(argv) > 2 and argv[2] == "test")))
            or (exe == "go" and len(argv) >= 2 and argv[1] == "test")
            or (exe == "cargo" and len(argv) >= 2 and argv[1] == "test")
            or (exe == "dotnet" and len(argv) >= 2 and argv[1] == "test")
            or (exe in {"mvn", "mvn.cmd", "gradlew", "gradlew.bat"} and len(argv) >= 2 and argv[1] == "test")
        )

    def _run_argv(self, argv: list[str], expect_git: bool = False) -> dict[str, Any]:
        try:
            # Windows package-manager wrappers are batch files, which CreateProcess
            # cannot launch directly with shell=False. Keep cmd.exe behind the allowlist.
            launch = argv
            if os.name == "nt" and Path(argv[0]).name.lower() in {"npm", "npm.cmd", "pnpm", "pnpm.cmd", "yarn", "yarn.cmd", "mvn", "mvn.cmd", "gradlew.bat"}:
                launch = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", subprocess.list2cmdline(argv)]
            proc = subprocess.run(launch, cwd=self.root, shell=False, capture_output=True, text=True,
                                  timeout=self.command_timeout, check=False)
            return {"command": subprocess.list2cmdline(argv), "returncode": proc.returncode,
                    "stdout": proc.stdout[-12000:], "stderr": proc.stderr[-12000:]}
        except FileNotFoundError:
            return {"command": subprocess.list2cmdline(argv), "returncode": 127, "stdout": "",
                    "stderr": f"Executable not found: {argv[0]}"}
        except subprocess.TimeoutExpired as exc:
            return {"command": subprocess.list2cmdline(argv), "returncode": 124,
                    "stdout": str(exc.stdout or "")[-12000:], "stderr": f"Timed out after {self.command_timeout} seconds"}


TOOL_DEFINITIONS = [
    {"type": "function", "function": {"name": "list_files", "description": "List workspace files, excluding dependency and git folders.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": []}}},
    {"type": "function", "function": {"name": "search_code", "description": "Find a case-insensitive text string in workspace source files.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "read_file", "description": "Read a UTF-8 text file with line numbers.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "start_line": {"type": "integer"}, "end_line": {"type": ["integer", "null"]}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "edit_file", "description": "Create a new file or replace one exact unique text block. For new files, old_text must be empty.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}}},
    {"type": "function", "function": {"name": "run_tests", "description": "Run the workspace's detected test suite.", "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {"name": "run_command", "description": "Run a permitted test/build or read-only git command in the workspace.", "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
    {"type": "function", "function": {"name": "git_diff", "description": "Show the workspace's tracked Git changes.", "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {"name": "rollback_changes", "description": "Roll back the most recent active Hippo edit session. Leaves any file changed by someone else since the edit untouched.", "parameters": {"type": "object", "properties": {}, "required": []}}},
]
