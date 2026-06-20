#!/usr/bin/env python3
"""
YABBAI Local -- Ollama Client

Talks to a locally-running Ollama server (default http://localhost:11434).
Everything stays on your machine. No external network calls, no API keys, nothing
leaves your computer.

Designed to fail gracefully: if Ollama isn't installed/running, or the model isn't
pulled yet, it returns clear, actionable guidance instead of crashing -- so the app
is usable for setup even before a model is ready.
"""

import json
from typing import Dict, List, Optional, Generator

import httpx

OLLAMA_HOST = "http://localhost:11434"


class OllamaClient:
    def __init__(self, host: str = OLLAMA_HOST, model: str = "llama3.1:8b"):
        self.host = host.rstrip("/")
        self.model = model

    # ── Status / setup helpers ──────────────────────────────────────────────
    def is_running(self) -> bool:
        try:
            r = httpx.get(f"{self.host}/api/tags", timeout=3)
            return r.status_code == 200
        except Exception:
            return False

    def list_models(self) -> List[str]:
        try:
            r = httpx.get(f"{self.host}/api/tags", timeout=5)
            r.raise_for_status()
            return [m["name"] for m in r.json().get("models", [])]
        except Exception:
            return []

    def model_ready(self, model: Optional[str] = None) -> bool:
        target = model or self.model
        installed = self.list_models()
        # Ollama tags may include ':latest'; match loosely
        return any(target.split(":")[0] in m for m in installed)

    def setup_status(self) -> Dict:
        """One call the UI uses to know what state setup is in."""
        if not self.is_running():
            return {
                "ready": False, "stage": "ollama_not_running",
                "message": "Ollama isn't running. Install it from ollama.com, then "
                           "run `ollama serve` (or just open the Ollama app).",
            }
        models = self.list_models()
        if not models:
            return {
                "ready": False, "stage": "no_models",
                "message": f"Ollama is running but no models are pulled. "
                           f"Run: `ollama pull {self.model}`",
                "installed": [],
            }
        if not self.model_ready():
            return {
                "ready": False, "stage": "model_not_pulled",
                "message": f"Model '{self.model}' not found. Either run "
                           f"`ollama pull {self.model}` or pick one you have.",
                "installed": models,
            }
        return {"ready": True, "stage": "ready", "model": self.model, "installed": models}

    # ── Chat ─────────────────────────────────────────────────────────────────
    def chat(self, system: str, messages: List[Dict], temperature: float = 0.7,
             max_tokens: int = 1024) -> Dict:
        """
        Non-streaming chat. messages = [{'role':'user'|'assistant','content':...}].
        Returns {'ok':bool, 'content':str, 'model':str, 'error':str?}.
        """
        status = self.setup_status()
        if not status["ready"]:
            return {"ok": False, "content": status["message"],
                    "model": self.model, "setup": status}

        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}] + messages,
            "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        try:
            r = httpx.post(f"{self.host}/api/chat", json=payload, timeout=180)
            r.raise_for_status()
            data = r.json()
            return {"ok": True,
                    "content": data.get("message", {}).get("content", ""),
                    "model": self.model,
                    "eval_count": data.get("eval_count"),
                    "total_duration_ms": round(data.get("total_duration", 0) / 1e6)}
        except httpx.TimeoutException:
            return {"ok": False, "model": self.model,
                    "content": "The model took too long to respond. On CPU or a large "
                               "model this can happen -- try a smaller model."}
        except Exception as e:
            return {"ok": False, "model": self.model, "content": f"Local model error: {e}"}

    def chat_with_tools(self, system: str, messages: List[Dict], tools: List[Dict],
                        temperature: float = 0.4) -> Dict:
        """
        Tool-calling chat. Ollama supports native tools for capable models
        (llama3.1, qwen2.5, mistral-nemo, etc.). Returns content + any tool_calls.
        """
        status = self.setup_status()
        if not status["ready"]:
            return {"ok": False, "content": status["message"]}

        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}] + messages,
            "stream": False,
            "tools": tools,
            "options": {"temperature": temperature},
        }
        try:
            r = httpx.post(f"{self.host}/api/chat", json=payload, timeout=300)
            r.raise_for_status()
            data = r.json()
            msg = data.get("message", {})
            return {"ok": True,
                    "content": msg.get("content", ""),
                    "tool_calls": msg.get("tool_calls", []),
                    "model": self.model}
        except httpx.TimeoutException:
            return {"ok": False, "content": "The model took too long. Try a smaller model or simpler task."}
        except Exception as e:
            return {"ok": False, "content": f"Local model error: {e}"}

    def chat_stream(self, system: str, messages: List[Dict],
                    temperature: float = 0.7) -> Generator[str, None, None]:
        """Streaming generator -- yields tokens as the local model produces them."""
        status = self.setup_status()
        if not status["ready"]:
            yield status["message"]
            return
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}] + messages,
            "stream": True,
            "options": {"temperature": temperature},
        }
        try:
            with httpx.stream("POST", f"{self.host}/api/chat", json=payload, timeout=180) as r:
                for line in r.iter_lines():
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                        token = chunk.get("message", {}).get("content", "")
                        if token:
                            yield token
                    except json.JSONDecodeError:
                        continue
        except Exception as e:
            yield f"\n[stream error: {e}]"
