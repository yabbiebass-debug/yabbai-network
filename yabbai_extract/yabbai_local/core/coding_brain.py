#!/usr/bin/env python3
"""
YABBAI Local -- Coding Brain

The agentic coding loop. Wraps the local model with the CodingTools so it can
actually work on your files, Claude-Code-style:

  user asks --> model thinks --> model calls a tool --> 
     if READ tool: auto-run, feed result back, continue
     if WRITE/RUN tool: PAUSE, ask the user to confirm in the UI, then run
  --> model continues until it has an answer

Honest scope: the local 8B model is a capable junior. It handles "read this file
and explain it", "create a small script", "fix this function", "run the tests".
It will struggle with large multi-file refactors or long autonomous tasks -- that's
the model's ceiling, not the harness's. The harness is solid; the brain is 8B.
"""

import json
from datetime import datetime, timezone
from typing import Dict, List, Optional

from .ollama_client import OllamaClient
from .tools import CodingTools, TOOL_SCHEMAS, CONFIRM_REQUIRED, AUTO_RUN


CODING_PERSONA = """You are YABBAI Code -- a local, private coding assistant running
on the user's own machine. You help write, read, edit, and run code.

You have tools to work with the user's files and shell:
- list_dir, read_file, tree: inspect the workspace (these run automatically)
- write_file, run_command: change files or run commands (the USER must confirm these)

How to work:
- When asked about code, READ the relevant files first before answering.
- Make small, focused changes. Explain what you're doing and why.
- When you want to create/edit a file or run a command, call the tool -- the user
  will be asked to approve it. Don't pretend you've done it until it's confirmed.
- Be honest about your limits. You are a local model, capable but not infinite. If
  a task is beyond you, say so and suggest how the user could break it down.
- Keep responses focused and practical. Show code clearly."""


class CodingBrain:
    def __init__(self, model: str, workspace: str):
        self.client = OllamaClient(model=model)
        self.tools = CodingTools(workspace)
        self.history: List[Dict] = []
        # actions waiting for user confirmation
        self.pending: Dict[str, Dict] = {}
        self._counter = 0

    def set_model(self, model: str):
        self.client.model = model

    def set_workspace(self, workspace: str):
        self.tools = CodingTools(workspace)

    def setup_status(self) -> Dict:
        return self.client.setup_status()

    def _execute_tool(self, name: str, args: Dict) -> Dict:
        fn = getattr(self.tools, name, None)
        if not fn:
            return {"ok": False, "error": f"unknown tool {name}"}
        try:
            return fn(**args)
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def chat(self, user_text: str) -> Dict:
        """
        One turn. Runs the tool loop, auto-executing reads and pausing on
        writes/commands. Returns either a final answer or a confirmation request.
        """
        status = self.client.setup_status()
        if not status["ready"]:
            return {"type": "message", "content": status["message"]}

        self.history.append({"role": "user", "content": user_text})
        return self._run_loop()

    def confirm_pending(self, pending_id: str, approved: bool) -> Dict:
        """User approved or rejected a write/run action."""
        pa = self.pending.pop(pending_id, None)
        if not pa:
            return {"type": "message", "content": "That action expired or was already handled."}

        if not approved:
            # Tell the model the user declined, let it continue
            self.history.append({"role": "tool", "content": json.dumps(
                {"ok": False, "declined_by_user": True,
                 "note": "User declined this action. Suggest an alternative or stop."})})
            return self._run_loop()

        # Execute the approved action
        result = self._execute_tool(pa["name"], pa["args"])
        self.history.append({"role": "tool", "content": json.dumps(result)})
        return self._run_loop()

    def _run_loop(self, max_steps: int = 6) -> Dict:
        """Core agentic loop. Stops to ask for confirmation on state-changing tools."""
        for _ in range(max_steps):
            resp = self.client.chat_with_tools(
                system=CODING_PERSONA, messages=self.history, tools=TOOL_SCHEMAS)

            if not resp.get("ok"):
                return {"type": "message", "content": resp.get("content", "Local model error.")}

            tool_calls = resp.get("tool_calls") or []

            if not tool_calls:
                # Final natural-language answer
                content = resp.get("content", "")
                self.history.append({"role": "assistant", "content": content})
                return {"type": "message", "content": content, "model": self.client.model}

            # Record the assistant's tool-calling turn
            self.history.append({"role": "assistant", "content": resp.get("content", ""),
                                 "tool_calls": tool_calls})

            # Process tool calls one at a time
            for call in tool_calls:
                name = call["function"]["name"]
                args = call["function"].get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {}

                if name in CONFIRM_REQUIRED:
                    # Pause -- ask the user
                    self._counter += 1
                    pid = f"act_{self._counter}"
                    self.pending[pid] = {"name": name, "args": args}
                    return {"type": "confirm", "pending_id": pid,
                            "tool": name, "args": args,
                            "preview": self._preview(name, args)}
                else:
                    # Auto-run read-only tools, feed result back
                    result = self._execute_tool(name, args)
                    self.history.append({"role": "tool", "content": json.dumps(result)})
                    # loop continues, model sees the result

        return {"type": "message",
                "content": "Reached the step limit for this turn. Ask me to continue if needed."}

    def _preview(self, name: str, args: Dict) -> str:
        if name == "write_file":
            content = args.get("content", "")
            preview = content[:500] + ("\n... (truncated)" if len(content) > 500 else "")
            return f"Write to '{args.get('rel_path')}':\n\n{preview}"
        if name == "run_command":
            return f"Run command:\n\n  {args.get('command')}"
        return json.dumps(args)

    def reset(self):
        self.history = []
        self.pending = {}
