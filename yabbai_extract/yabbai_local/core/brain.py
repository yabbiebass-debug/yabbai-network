#!/usr/bin/env python3
"""
YABBAI Local -- Brain (Star-Network Orchestration over a Local Model)

This is the orchestration workflow you designed for YABBAI -- the star-network
pattern where a central brain routes through a guard, draws on a local codex
(memory), and talks to a model backend. The difference now: the model is LOCAL
(via Ollama), so the whole thing runs privately on your machine.

This is NOT a frontier model and does not claim to be. It's the YABBAI nervous
system -- router, guard, memory, persona -- wrapping whatever open-weights model
you've pulled. The intelligence ceiling is the local model's; YABBAI adds
structure, safety, memory, and consistency around it.

Star-network shape:
                    ┌─────────┐
                    │  GUARD  │  (safety check on every input)
                    └────┬────┘
        ┌───────────┐    │    ┌──────────┐
        │  CODEX    │────┼────│  PERSONA │
        │ (memory)  │    │    │  (system)│
        └───────────┘    │    └──────────┘
                    ┌────┴────┐
                    │  MODEL  │  (local, via Ollama)
                    └─────────┘
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .ollama_client import OllamaClient


# ── Guard: a lightweight local safety check ──────────────────────────────────
class LocalGuard:
    """
    Minimal local guard. Blocks a few clearly-harmful categories before they reach
    the model. This is a private personal tool, so the guard is light -- but the hook
    is here if you want to extend it.
    """
    BLOCK_PATTERNS = [
        r"(?i)\b(make|build|synthesize)\b.*\b(bioweapon|nerve agent|explosive device)\b",
        r"(?i)\bhow to (hack|breach)\b.*\b(bank|government|someone'?s account)\b",
    ]

    def check(self, text: str) -> Dict:
        for pat in self.BLOCK_PATTERNS:
            if re.search(pat, text):
                return {"allowed": False,
                        "message": "I won't help with that one. Anything else I can do?"}
        return {"allowed": True}


# ── Codex: simple local persistent memory ────────────────────────────────────
class LocalCodex:
    """
    A local JSON-backed memory. Stores notable facts/notes the user asks to remember,
    and recent conversation summaries. Entirely on-disk, on your machine.
    """
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.entries: List[Dict] = []
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                self.entries = json.loads(self.path.read_text())
            except Exception:
                self.entries = []

    def _save(self):
        try:
            self.path.write_text(json.dumps(self.entries, indent=2))
        except Exception:
            pass

    def add(self, content: str, kind: str = "note"):
        self.entries.append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "kind": kind, "content": content,
        })
        self.entries = self.entries[-200:]   # cap
        self._save()

    def context_block(self, max_entries: int = 8) -> str:
        if not self.entries:
            return ""
        recent = self.entries[-max_entries:]
        lines = ["[ YABBAI memory -- things you've told me to remember ]"]
        for e in recent:
            lines.append(f"- ({e['kind']}) {e['content']}")
        return "\n".join(lines)


# ── Brain: the central router ─────────────────────────────────────────────────
class YabbaiLocalBrain:
    PERSONA = """You are YABBAI -- a private, local AI assistant running entirely on
the user's own machine. You are direct, capable, and a little bit a builder's
companion: practical, honest, no corporate filler. You help with coding, planning,
writing, analysis, and thinking things through.

Be honest about what you don't know. You run on a local open-weights model, so if a
task needs capabilities beyond you, say so plainly rather than bluffing. Keep
answers focused. The user values straight talk and real help."""

    def __init__(self, model: str, codex_path: str):
        self.client = OllamaClient(model=model)
        self.guard = LocalGuard()
        self.codex = LocalCodex(codex_path)
        self.history: List[Dict] = []

    def set_model(self, model: str):
        self.client.model = model

    def setup_status(self) -> Dict:
        return self.client.setup_status()

    def _build_system(self) -> str:
        system = self.PERSONA
        mem = self.codex.context_block()
        if mem:
            system += "\n\n" + mem
        return system

    def _maybe_capture_memory(self, user_text: str):
        """If the user says 'remember that ...', store it in the codex."""
        m = re.search(r"(?i)\bremember (?:that )?(.+)", user_text)
        if m:
            self.codex.add(m.group(1).strip(), kind="user_request")

    def chat(self, user_text: str, stream: bool = False):
        # 1. Guard
        verdict = self.guard.check(user_text)
        if not verdict["allowed"]:
            return {"ok": True, "content": verdict["message"], "model": self.client.model,
                    "guarded": True}

        # 2. Memory capture
        self._maybe_capture_memory(user_text)

        # 3. Assemble + route to local model
        self.history.append({"role": "user", "content": user_text})
        system = self._build_system()
        # keep last ~12 turns to fit context
        msgs = self.history[-12:]

        if stream:
            return self.client.chat_stream(system, msgs)

        result = self.client.chat(system, msgs)
        if result.get("ok"):
            self.history.append({"role": "assistant", "content": result["content"]})
        return result

    def reset(self):
        self.history = []
