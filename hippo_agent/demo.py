"""Create a small intentionally broken Node project for a live agent demo."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path


TASK = (
    "Inspect this small Node.js project. Fix validateCredentials so it returns false "
    "for missing or invalid email/password values, preserves the stated requirements "
    "in the tests, and passes the complete test suite. Read the failing test output, "
    "make the smallest focused fix, and run the tests again before summarizing."
)


def create_demo_workspace(parent: Path | None = None) -> Path:
    root = Path(tempfile.mkdtemp(prefix="hippo-code-demo-", dir=parent)).resolve()
    (root / "package.json").write_text(json.dumps({
        "name": "hippo-agent-live-demo", "private": True, "type": "module",
        "scripts": {"test": "node --test"},
    }, indent=2) + "\n", encoding="utf-8")
    (root / "auth.js").write_text(
        "export function validateCredentials(input) {\n"
        "  return input.email.includes('@') && input.password.length >= 8;\n"
        "}\n", encoding="utf-8")
    (root / "auth.test.js").write_text(
        "import test from 'node:test';\n"
        "import assert from 'node:assert/strict';\n"
        "import { validateCredentials } from './auth.js';\n\n"
        "test('accepts valid credentials', () => {\n"
        "  assert.equal(validateCredentials({ email: 'dev@example.com', password: 'secure123' }), true);\n"
        "});\n\n"
        "test('rejects missing and malformed credentials without throwing', () => {\n"
        "  assert.equal(validateCredentials(null), false);\n"
        "  assert.equal(validateCredentials({ email: 'bad-email', password: 'secure123' }), false);\n"
        "  assert.equal(validateCredentials({ email: 'dev@example.com' }), false);\n"
        "  assert.equal(validateCredentials({ email: 'dev@example.com', password: 'short' }), false);\n"
        "});\n", encoding="utf-8")
    return root
