# Hippo Code Agent
![Tests](https://github.com/prati31-sys/hippo-code-agent/actions/workflows/tests.yml/badge.svg)

A small, runnable AI coding agent that works inside one selected project folder. It uses Python for orchestration and Groq for model inference. It can inspect and search source files, apply exact edits, run tests, show Git diffs, and report every tool call and result.

## Requirements

- Python 3.10 or newer
- A Groq API key for live AI tasks
- Node.js only when working on JavaScript projects

The agent itself uses only Python's standard library. It does not need the Groq SDK or any third-party runtime package.

## Setup

From this project folder, create and activate a virtual environment, then install the project:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
Copy-Item .env.example .env
```

Add your Groq key to `.env` in the directory where you will launch `hippo` (or set it in PowerShell for the current session). Keep the key out of source control. The CLI loads simple `KEY=value` entries from that file without overwriting existing environment values:

```powershell
$env:GROQ_API_KEY = "your-key"
```

The default model is `openai/gpt-oss-120b`, which supports local tool calling. Override it with `GROQ_MODEL` or `--model` if your account uses another compatible model.

## Run

From the installed agent folder, point it at the project you want to work on:

```powershell
hippo --workspace "C:\path\to\your-project" "Inspect the project, find the login bug, fix it, and run the tests"
```

Or use the module entry point without installing the console script:

```powershell
python -m hippo_agent --workspace "C:\path\to\your-project" "Add input validation to the signup flow"
```

The selected workspace is the boundary for listing, searching, reading, and editing files. Always pass the project folder explicitly. `.env` is read from the launch directory, which can be the installed agent folder.

## What it can do

| Tool | Purpose |
| --- | --- |
| `list_files` | Explore the project tree (skips `.git`, virtual environments, and dependency folders) |
| `search_code` | Find text in readable source files |
| `read_file` | Read UTF-8 text files with line numbers |
| `edit_file` | Create a file or replace one exact, unique text block; prints a unified diff |
| `run_tests` | Detect and run Python, Node.js, Go, Rust, .NET, Maven, or Gradle tests |
| `run_command` | Run permitted Python, Node.js, Go, Rust, .NET, Maven, or Gradle test commands and read-only Git commands |
| `git_diff` | Review tracked working-tree changes |
| `rollback_changes` | Safely revert the latest Hippo edit session |

The model sees a maximum of 40 rounds by default; change this with `--max-steps`. After a failed test run, no more than three subsequent edits are allowed. The agent will not accept a final answer after a file change until it has run tests. Test commands and model calls have time limits, and command output is capped before it is sent back to the model.

Hippo records the original bytes and last agent-written version of edited files in a local temporary journal. Roll back the most recent active edit session with:

```powershell
hippo --workspace "C:\path\to\your-project" --rollback
```

Rollback restores only files that still match Hippo's last written version. If you or another process changed a file afterward, Hippo reports a conflict and leaves that file untouched. The journal is stored outside the project folder under the operating system's temporary directory.

## Command safety

Commands run without a shell from the selected workspace. Shell chaining, redirection, pipes, and arbitrary command execution are rejected. Allowed commands include Python `unittest`/`pytest`, Node `--test`/`--check`, selected npm/pnpm/yarn scripts, `go test`, `cargo test`, `dotnet test`, Maven `test`, Gradle wrapper `test`, and read-only `git diff`/`status`/`log`. Node projects choose npm, pnpm, or Yarn from their lockfile. Do not use this as a sandbox for untrusted repositories; an allowed test/build script can itself execute project code.

File reads and edits are limited to 500 KB per file. Edits require the old text to occur exactly once, preventing ambiguous broad replacements. The agent cannot resolve paths outside the selected workspace.

## Verification

Run Hippo's offline self-test suite (no Groq key needed):

```powershell
hippo --self-test
```

Or run the suite directly from the source checkout with `python -m unittest discover -v`.

For a live end-to-end demo, set `GROQ_API_KEY`, install Node.js/npm, and run:

```powershell
hippo --demo
```

Hippo creates a demo project with a failing credential-validation test, asks Groq to inspect and fix it, reruns the tests, and retains the demo folder for review. Use the printed `--rollback` command to revert its changes.

The tests exercise the Groq request format using a mocked HTTP response, tool-call orchestration, enforced testing after edits, workspace path containment, file search/read/edit behavior, command restrictions, and the three-edit repair limit. A live Groq task requires a valid API key and network access; without those, the core tools and tests remain usable, and the CLI reports the API setup problem clearly.

## Limitations

- The codebase has a CLI rather than a graphical editor UI.
- `--self-test` runs from a source checkout; it reports when the test files are not installed alongside the package.
- `--demo` is a live Groq run and needs a valid API key plus Node.js/npm.
- `run_tests` detects common Python, Node.js, Go, Rust, .NET, Maven, and Gradle project markers. Use `run_command` for a project's other permitted scripts or test setup.
- The CLI reads `GROQ_API_KEY` and `GROQ_MODEL` from the process environment, with optional simple `KEY=value` entries from `.env` in the launch directory.
- This is a local coding helper, not an operating-system security sandbox. Keep the allowed project scripts trustworthy.
