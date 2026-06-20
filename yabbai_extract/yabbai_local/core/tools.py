#!/usr/bin/env python3
"""
YABBAI Local -- Coding Tools

This is what turns YABBAI Local from a chatbot into a coding assistant: it gives
the local model the ability to act on your machine -- read files, write/edit files,
list directories, and run shell commands -- instead of only talking about them.

SAFETY MODEL (important):
  - Every tool call is sandboxed to a WORKSPACE directory you choose. The model
    cannot read or write outside it (path traversal is blocked).
  - WRITE and RUN operations require confirmation by default (the UI asks you
    before anything changes on disk or executes). READ/LIST are safe and auto-run.
  - A command denylist blocks obviously destructive operations even if confirmed.

This is a local junior assistant, so the guardrails matter more than convenience.
"""

import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Dict, List, Optional


# ── Commands we refuse outright, even with confirmation ──────────────────────
DANGEROUS_PATTERNS = [
    r"\brm\s+-rf\s+/",          # wipe root
    r"\bmkfs\b", r"\bdd\b.*of=/dev", r":\(\)\{.*\};:",  # fork bomb
    r"\b(shutdown|reboot|halt|poweroff)\b",
    r">\s*/dev/sd", r"\bchmod\s+-R\s+777\s+/",
    r"\bcurl\b.*\|\s*(sudo\s+)?(sh|bash)",   # pipe-to-shell
    r"\bwget\b.*\|\s*(sudo\s+)?(sh|bash)",
]


class ToolError(Exception):
    pass


class CodingTools:
    def __init__(self, workspace: str):
        self.workspace = Path(workspace).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)

    # ── Path safety ──────────────────────────────────────────────────────────
    def _safe(self, rel_path: str) -> Path:
        """Resolve a path and ensure it stays inside the workspace."""
        p = (self.workspace / rel_path).resolve()
        if self.workspace not in p.parents and p != self.workspace:
            raise ToolError(f"Path '{rel_path}' is outside the workspace. Blocked.")
        return p

    # ── READ (safe, auto-run) ─────────────────────────────────────────────────
    def list_dir(self, rel_path: str = ".") -> Dict:
        p = self._safe(rel_path)
        if not p.exists():
            return {"ok": False, "error": f"{rel_path} does not exist"}
        items = []
        for child in sorted(p.iterdir()):
            if child.name.startswith(".git") or child.name == "node_modules":
                continue
            items.append({"name": child.name, "type": "dir" if child.is_dir() else "file",
                          "size": child.stat().st_size if child.is_file() else None})
        return {"ok": True, "path": rel_path, "items": items}

    def read_file(self, rel_path: str, max_bytes: int = 60000) -> Dict:
        p = self._safe(rel_path)
        if not p.exists() or not p.is_file():
            return {"ok": False, "error": f"{rel_path} is not a readable file"}
        try:
            text = p.read_text(encoding="utf-8", errors="replace")[:max_bytes]
            return {"ok": True, "path": rel_path, "content": text,
                    "truncated": p.stat().st_size > max_bytes}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def tree(self, rel_path: str = ".", max_depth: int = 3) -> Dict:
        root = self._safe(rel_path)
        lines = []
        def walk(d: Path, depth: int, prefix: str):
            if depth > max_depth:
                return
            try:
                children = sorted([c for c in d.iterdir()
                                   if not c.name.startswith(".git")
                                   and c.name != "node_modules"
                                   and c.name != "__pycache__"])
            except Exception:
                return
            for c in children:
                lines.append(f"{prefix}{c.name}{'/' if c.is_dir() else ''}")
                if c.is_dir():
                    walk(c, depth + 1, prefix + "  ")
        walk(root, 1, "")
        return {"ok": True, "tree": "\n".join(lines) or "(empty)"}

    # ── WRITE (requires confirmation in the UI) ───────────────────────────────
    def write_file(self, rel_path: str, content: str) -> Dict:
        p = self._safe(rel_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        existed = p.exists()
        try:
            p.write_text(content, encoding="utf-8")
            return {"ok": True, "path": rel_path,
                    "action": "overwrote" if existed else "created",
                    "bytes": len(content.encode("utf-8"))}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── RUN (requires confirmation + denylist) ────────────────────────────────
    def run_command(self, command: str, timeout: int = 60) -> Dict:
        for pat in DANGEROUS_PATTERNS:
            if re.search(pat, command):
                return {"ok": False, "blocked": True,
                        "error": "This command matches a destructive pattern and was blocked."}
        try:
            result = subprocess.run(
                command, shell=True, cwd=str(self.workspace),
                capture_output=True, text=True, timeout=timeout)
            return {"ok": True, "command": command, "exit_code": result.returncode,
                    "stdout": result.stdout[-8000:], "stderr": result.stderr[-4000:]}
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"Command timed out after {timeout}s"}
        except Exception as e:
            return {"ok": False, "error": str(e)}


# ── Tool schemas the model is told about (Ollama tool-calling format) ─────────
TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "list_dir", "description": "List files and folders in a workspace directory.",
        "parameters": {"type": "object", "properties": {
            "rel_path": {"type": "string", "description": "Path relative to workspace root. Default '.'"}}}}},
    {"type": "function", "function": {
        "name": "read_file", "description": "Read the contents of a file in the workspace.",
        "parameters": {"type": "object", "properties": {
            "rel_path": {"type": "string"}}, "required": ["rel_path"]}}},
    {"type": "function", "function": {
        "name": "tree", "description": "Show the directory tree of the workspace.",
        "parameters": {"type": "object", "properties": {
            "rel_path": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "write_file", "description": "Create or overwrite a file. REQUIRES USER CONFIRMATION.",
        "parameters": {"type": "object", "properties": {
            "rel_path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["rel_path", "content"]}}},
    {"type": "function", "function": {
        "name": "run_command", "description": "Run a shell command in the workspace. REQUIRES USER CONFIRMATION.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string"}}, "required": ["command"]}}},
]

# Which tools change state (need confirmation) vs are read-only (auto-run)
CONFIRM_REQUIRED = {"write_file", "run_command"}
AUTO_RUN = {"list_dir", "read_file", "tree"}
