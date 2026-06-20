#!/usr/bin/env python3
"""
YABBAI v9.5.0 — Specialized Agents

Five agents that do real, useful work FOR YOU. Each has a focused job, uses the
local model (escalating only when you allow), and surfaces results for you to act
on. They divide labor like a small team — not a "swarm extracting money," but
genuine assistants that make YABBAI more capable.

THE FIVE:
  1. ResearchAgent   — researches a topic using the model + (optional) web, returns
                       a structured brief with sources you can verify.
  2. CodeFixerAgent  — scans a file/codebase for bugs, smells, missing error handling;
                       proposes fixes as reviewable diffs (never auto-applies blind).
  3. ScamScoutAgent  — wraps the GoldScout scam-analysis: given a URL/opportunity,
                       returns a risk score + red flags. Protects you; never executes.
  4. SummarizerAgent — condenses long text/docs/conversations into clear summaries.
  5. SchedulerAgent  — organizes tasks/todos, suggests an order, tracks what's pending.
                       (Local list — it organizes, it doesn't act on your behalf.)

DESIGN: every agent RETURNS information or PROPOSES actions. None take irreversible
actions autonomously. They're a team of researchers and assistants, human-directed.
This is the honest, useful version of "agents working for you."
"""

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class AgentResult:
    agent: str
    ok: bool
    summary: str
    detail: Dict = field(default_factory=dict)
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ── 1. RESEARCH AGENT ────────────────────────────────────────────────────────
class ResearchAgent:
    """Researches a topic with the model; structured brief, sources flagged for you to verify."""
    name = "research"

    def __init__(self, model_pool, workhorse_id="local-8b"):
        self.pool = model_pool
        self.workhorse = workhorse_id

    def run(self, topic: str, depth: str = "brief") -> AgentResult:
        system = ("You are a research assistant. Produce a clear, structured brief on the "
                  "topic. Use headings: Overview, Key Points, Considerations, Open Questions. "
                  "Be honest about uncertainty. Do NOT invent sources or statistics — if you "
                  "don't know, say so and suggest what to verify.")
        prompt = f"Research topic: {topic}\nDepth: {depth}\nGive a structured brief."
        try:
            result = self.pool.call(self.workhorse, system, [{"role": "user", "content": prompt}])
            return AgentResult(self.name, result.ok,
                               f"Research brief on: {topic}",
                               {"brief": result.content, "model": result.model,
                                "note": "Verify any factual claims independently — the model "
                                        "can be confidently wrong. Treat as a starting point."})
        except Exception as e:
            return AgentResult(self.name, False, f"Research failed: {e}", {})


# ── 2. CODE FIXER AGENT ──────────────────────────────────────────────────────
class CodeFixerAgent:
    """Scans code for issues, proposes fixes as diffs. Never auto-applies."""
    name = "code_fixer"

    def __init__(self, model_pool, workhorse_id="local-8b"):
        self.pool = model_pool
        self.workhorse = workhorse_id

    def run(self, file_path: str, code: Optional[str] = None) -> AgentResult:
        if code is None:
            try:
                code = Path(file_path).read_text(encoding="utf-8")
            except Exception as e:
                return AgentResult(self.name, False, f"Couldn't read {file_path}: {e}", {})
        if len(code) > 8000:
            code = code[:8000] + "\n# ... (truncated for analysis)"

        system = ("You are a careful code reviewer. Identify real issues: bugs, missing error "
                  "handling, security concerns, unclear naming. For each, give: the problem, "
                  "why it matters, and a suggested fix. Be specific and conservative — don't "
                  "invent problems. If the code is fine, say so. Do NOT rewrite the whole file.")
        prompt = f"Review this code from {file_path}:\n\n```\n{code}\n```"
        try:
            result = self.pool.call(self.workhorse, system, [{"role": "user", "content": prompt}])
            return AgentResult(self.name, result.ok,
                               f"Code review: {file_path}",
                               {"review": result.content, "model": result.model,
                                "note": "These are SUGGESTIONS to review — apply them yourself "
                                        "after checking. Nothing was changed."})
        except Exception as e:
            return AgentResult(self.name, False, f"Review failed: {e}", {})


# ── 3. SCAM SCOUT AGENT (wraps GoldScout's real edge) ───────────────────────
class ScamScoutAgent:
    """Given a URL/opportunity, returns risk score + red flags. Protects; never executes."""
    name = "scam_scout"

    # Mirror of the GoldScout scam patterns (kept self-contained here)
    DRAINER_PATTERNS = [
        (r"(airdrop|claim)[-.]?(now|reward|gift|bonus)", "Urgency-claim pattern (common drainer)"),
        (r"(jupiter|uniswap|aave|metamask|phantom)[-.]?(airdrop|claim|gift)", "Impersonates a known protocol"),
        (r"\.(xyz|top|click|gift|live)$", "Cheap throwaway TLD"),
        (r"free-?(crypto|eth|sol|money)", "Advertises 'free crypto/money'"),
        (r"(wallet|seed)[-.]?(verify|sync|restore)", "Seed-phrase phishing pattern"),
    ]
    SCAM_TEXT = [
        (r"connect wallet to (claim|receive)", "Asks to connect wallet to claim — drainer", 30),
        (r"enter (seed|recovery|private key)", "Requests seed/private key — guaranteed theft", 100),
        (r"guaranteed (returns?|profit)", "Promises guaranteed returns — scam tell", 40),
        (r"send .{0,10}(to receive|to claim)", "Advance-fee scam (send to receive)", 100),
        (r"double your|2x your", "Double-your-crypto giveaway scam", 60),
    ]

    def run(self, url: str = "", text: str = "") -> AgentResult:
        score = 0
        flags = []
        domain = ""
        m = re.search(r"https?://([^/]+)", url)
        if m:
            domain = m.group(1).lower().replace("www.", "")
        hay = f"{url} {text}".lower()

        for pat, flag in self.DRAINER_PATTERNS:
            if domain and re.search(pat, domain):
                score += 35; flags.append(flag)
        for pat, flag, w in self.SCAM_TEXT:
            if re.search(pat, hay):
                score += w; flags.append(flag)
        if url.startswith("http://"):
            score += 20; flags.append("Not HTTPS")

        score = min(100, score)
        level = ("critical" if score >= 70 else "high" if score >= 40
                 else "medium" if score >= 20 else "low")
        verdict = {
            "critical": "Strong scam signals. Do NOT connect a wallet. Almost certainly a drainer.",
            "high": "Multiple red flags. Treat as unsafe; verify independently.",
            "medium": "Some concerns. Research the project independently first.",
            "low": "No obvious red flags — but absence of flags is NOT proof of safety. Verify.",
        }[level]
        return AgentResult(self.name, True,
                           f"Scam check: {level.upper()} ({score}/100)",
                           {"risk_score": score, "risk_level": level, "red_flags": flags,
                            "verdict": verdict, "note": "Research aid only. You act manually. "
                            "Never enter a seed phrase; verify URLs; use a burner wallet."})


# ── 4. SUMMARIZER AGENT ──────────────────────────────────────────────────────
class SummarizerAgent:
    """Condenses long text into a clear summary."""
    name = "summarizer"

    def __init__(self, model_pool, workhorse_id="local-8b"):
        self.pool = model_pool
        self.workhorse = workhorse_id

    def run(self, text: str, style: str = "bullets") -> AgentResult:
        if not text.strip():
            return AgentResult(self.name, False, "Nothing to summarize.", {})
        if len(text) > 12000:
            text = text[:12000] + "\n... (truncated)"
        system = (f"You are a summarizer. Condense the text into a clear {style} summary that "
                  "captures the key points faithfully. Don't add information that isn't there.")
        try:
            result = self.pool.call(self.workhorse, system,
                                    [{"role": "user", "content": f"Summarize:\n\n{text}"}])
            return AgentResult(self.name, result.ok, "Summary ready",
                               {"summary": result.content, "model": result.model})
        except Exception as e:
            return AgentResult(self.name, False, f"Summarize failed: {e}", {})


# ── 5. SCHEDULER AGENT ───────────────────────────────────────────────────────
class SchedulerAgent:
    """Organizes tasks/todos and suggests an order. Local list — organizes, doesn't act."""
    name = "scheduler"

    def __init__(self, data_path: str):
        self.path = Path(data_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.tasks = self._load()

    def _load(self) -> List[Dict]:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text())
            except Exception:
                return []
        return []

    def _save(self):
        try:
            self.path.write_text(json.dumps(self.tasks, indent=2))
        except Exception:
            pass

    def add(self, task: str, priority: str = "medium") -> AgentResult:
        self.tasks.append({"task": task, "priority": priority, "done": False,
                           "added": datetime.now(timezone.utc).isoformat()})
        self._save()
        return AgentResult(self.name, True, f"Added task: {task}", {"total": len(self.tasks)})

    def list_tasks(self) -> AgentResult:
        order = {"high": 0, "medium": 1, "low": 2}
        pending = [t for t in self.tasks if not t["done"]]
        pending.sort(key=lambda t: order.get(t["priority"], 1))
        return AgentResult(self.name, True, f"{len(pending)} pending tasks",
                           {"suggested_order": pending, "note": "Suggested by priority. "
                            "I organize the list — you decide and do the work."})

    def complete(self, task_substr: str) -> AgentResult:
        for t in self.tasks:
            if task_substr.lower() in t["task"].lower() and not t["done"]:
                t["done"] = True
                self._save()
                return AgentResult(self.name, True, f"Marked done: {t['task']}", {})
        return AgentResult(self.name, False, "No matching pending task.", {})


# ── The team coordinator ─────────────────────────────────────────────────────
class AgentTeam:
    """Holds the five agents and routes a request to the right one. Human-directed."""
    def __init__(self, model_pool, data_dir: str, workhorse_id="local-8b"):
        self.research = ResearchAgent(model_pool, workhorse_id)
        self.code_fixer = CodeFixerAgent(model_pool, workhorse_id)
        self.scam_scout = ScamScoutAgent()
        self.summarizer = SummarizerAgent(model_pool, workhorse_id)
        self.scheduler = SchedulerAgent(str(Path(data_dir) / "tasks.json"))

    def roster(self) -> List[Dict]:
        return [
            {"id": "research", "name": "Research Agent",
             "does": "Researches a topic, returns a structured brief (verify sources yourself)."},
            {"id": "code_fixer", "name": "Code Fixer Agent",
             "does": "Reviews code, proposes fixes as suggestions (never auto-applies)."},
            {"id": "scam_scout", "name": "Scam Scout Agent",
             "does": "Checks a URL/opportunity for scam red flags (protects you; never executes)."},
            {"id": "summarizer", "name": "Summarizer Agent",
             "does": "Condenses long text into a clear summary."},
            {"id": "scheduler", "name": "Scheduler Agent",
             "does": "Organizes your tasks and suggests an order (you do the work)."},
        ]
