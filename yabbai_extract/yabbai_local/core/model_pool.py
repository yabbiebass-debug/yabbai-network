#!/usr/bin/env python3
"""
YABBAI Local -- Multi-Provider Model Layer

One uniform interface over every model YABBAI can route to:
  - LOCAL  (Ollama): the 8B workhorse + any other pulled models. Free, private.
  - CLOUD  (API): Claude, OpenAI, Kimi, etc. Costs credits, leaves the machine.

Adding a new model later = one entry in the registry. The router (router.py)
picks among these; this file just knows how to *call* each one.

PRIVACY NOTE: local providers never leave the machine. Cloud providers send the
escalated request to that vendor. The registry marks each one `private: true/false`
so the UI can always tell the user honestly what just happened.

API keys are read from environment variables -- never hardcoded, never stored in
the repo. If a cloud provider's key is missing, it's simply marked unavailable.
"""

import os
import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import httpx

from .ollama_client import OllamaClient


@dataclass
class ProviderResult:
    ok: bool
    content: str
    provider: str
    model: str
    private: bool
    cost_hint: str = "free"        # 'free' | 'low' | 'medium' | 'high'
    error: Optional[str] = None
    tool_calls: list = field(default_factory=list)


# ── Registry: every model YABBAI knows about ────────────────────────────────
# tier: 'workhorse' (local default) | 'specialist' (escalation target)
# Adding a model = add a dict here. That's the whole extension point.
MODEL_REGISTRY = [
    # ---- LOCAL (Ollama) ----
    {"id": "local-8b", "provider": "ollama", "model": "llama3.1:8b",
     "label": "Llama 3.1 8B (local)", "tier": "workhorse",
     "private": True, "cost_hint": "free", "strengths": ["chat", "quick edits", "general"]},
    {"id": "local-qwen", "provider": "ollama", "model": "qwen2.5:7b",
     "label": "Qwen2.5 7B (local)", "tier": "workhorse",
     "private": True, "cost_hint": "free", "strengths": ["coding", "tool use"]},
    {"id": "local-coder", "provider": "ollama", "model": "deepseek-coder-v2:16b",
     "label": "DeepSeek Coder V2 (local)", "tier": "workhorse",
     "private": True, "cost_hint": "free", "strengths": ["coding"]},

    # ---- CLOUD (API) -- escalation specialists ----
    {"id": "claude", "provider": "anthropic", "model": "claude-sonnet-4-5",
     "label": "Claude Sonnet 4.5", "tier": "specialist",
     "private": False, "cost_hint": "medium", "strengths": ["hard reasoning", "refactors", "debugging"],
     "env_key": "ANTHROPIC_API_KEY"},
    {"id": "claude-opus", "provider": "anthropic", "model": "claude-opus-4-1",
     "label": "Claude Opus 4.1", "tier": "specialist",
     "private": False, "cost_hint": "high", "strengths": ["hardest reasoning"],
     "env_key": "ANTHROPIC_API_KEY"},
    {"id": "gpt", "provider": "openai", "model": "gpt-4o",
     "label": "GPT-4o", "tier": "specialist",
     "private": False, "cost_hint": "medium", "strengths": ["general", "vision"],
     "env_key": "OPENAI_API_KEY"},
    {"id": "kimi", "provider": "openai_compat", "model": "kimi-k2",
     "label": "Kimi K2", "tier": "specialist",
     "private": False, "cost_hint": "low", "strengths": ["long context", "coding"],
     "env_key": "MOONSHOT_API_KEY", "base_url": "https://api.moonshot.ai/v1"},
]


class ModelPool:
    def __init__(self, ollama_host: str = "http://localhost:11434"):
        self.ollama = OllamaClient(host=ollama_host)
        self.registry = {m["id"]: m for m in MODEL_REGISTRY}

    # ── Availability ─────────────────────────────────────────────────────────
    def available(self) -> List[Dict]:
        """List models that are actually usable right now (pulled / key present)."""
        out = []
        installed = self.ollama.list_models() if self.ollama.is_running() else []
        for m in self.registry.values():
            entry = dict(m)
            if m["provider"] == "ollama":
                entry["available"] = any(m["model"].split(":")[0] in i for i in installed)
                entry["reason"] = "" if entry["available"] else f"run: ollama pull {m['model']}"
            else:
                has_key = bool(os.getenv(m.get("env_key", "")))
                entry["available"] = has_key
                entry["reason"] = "" if has_key else f"set {m.get('env_key')} env var"
            out.append(entry)
        return out

    def get(self, model_id: str) -> Optional[Dict]:
        return self.registry.get(model_id)

    # ── Unified call ───────────────────────────────────────────────────────────
    def call(self, model_id: str, system: str, messages: List[Dict],
             tools: Optional[List[Dict]] = None, temperature: float = 0.5) -> ProviderResult:
        m = self.registry.get(model_id)
        if not m:
            return ProviderResult(False, f"Unknown model '{model_id}'", "?", "?", True,
                                  error="unknown_model")

        if m["provider"] == "ollama":
            return self._call_ollama(m, system, messages, tools)
        if m["provider"] == "anthropic":
            return self._call_anthropic(m, system, messages)
        if m["provider"] in ("openai", "openai_compat"):
            return self._call_openai(m, system, messages)
        return ProviderResult(False, "Unsupported provider", m["provider"], m["model"], False,
                              error="unsupported_provider")

    def _call_ollama(self, m, system, messages, tools) -> ProviderResult:
        if tools:
            r = self.ollama.chat_with_tools(system, messages, tools)
            self.ollama.model = m["model"]
            return ProviderResult(r.get("ok", False), r.get("content", ""),
                                  "ollama", m["model"], True, "free",
                                  error=None if r.get("ok") else r.get("content"),
                                  tool_calls=r.get("tool_calls", []))
        self.ollama.model = m["model"]
        r = self.ollama.chat(system, messages)
        return ProviderResult(r.get("ok", False), r.get("content", ""),
                              "ollama", m["model"], True, "free",
                              error=None if r.get("ok") else r.get("content"))

    def _call_anthropic(self, m, system, messages) -> ProviderResult:
        key = os.getenv(m.get("env_key", ""))
        if not key:
            return ProviderResult(False, f"No API key ({m.get('env_key')} not set).",
                                  "anthropic", m["model"], False, error="no_key")
        try:
            # Anthropic Messages API
            payload = {
                "model": m["model"], "max_tokens": 2048, "system": system,
                "messages": [mm for mm in messages if mm["role"] in ("user", "assistant")],
            }
            r = httpx.post("https://api.anthropic.com/v1/messages", json=payload, timeout=120,
                           headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                                    "content-type": "application/json"})
            r.raise_for_status()
            data = r.json()
            text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
            return ProviderResult(True, text, "anthropic", m["model"], False, m["cost_hint"])
        except Exception as e:
            return ProviderResult(False, f"Claude API error: {e}", "anthropic", m["model"], False,
                                  error=str(e))

    def _call_openai(self, m, system, messages) -> ProviderResult:
        key = os.getenv(m.get("env_key", ""))
        if not key:
            return ProviderResult(False, f"No API key ({m.get('env_key')} not set).",
                                  m["provider"], m["model"], False, error="no_key")
        base = m.get("base_url", "https://api.openai.com/v1")
        try:
            payload = {"model": m["model"],
                       "messages": [{"role": "system", "content": system}] + messages}
            r = httpx.post(f"{base}/chat/completions", json=payload, timeout=120,
                           headers={"Authorization": f"Bearer {key}",
                                    "Content-Type": "application/json"})
            r.raise_for_status()
            data = r.json()
            text = data["choices"][0]["message"]["content"]
            return ProviderResult(True, text, m["provider"], m["model"], False, m["cost_hint"])
        except Exception as e:
            return ProviderResult(False, f"{m['label']} API error: {e}", m["provider"], m["model"],
                                  False, error=str(e))
