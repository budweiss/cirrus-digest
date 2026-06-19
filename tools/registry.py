#!/usr/bin/env python3
"""
CIRRUS Tool Registry
Defines the tool schemas (for Claude API tool-use) and dispatches calls
to the actual implementations in system_tools.py, web_tools.py, etc.

Also provides ask_with_tools() — a multi-turn Claude API loop that lets
Claude reason about a question, call tools, and synthesize a final answer.
"""

import json
import sys
import logging
import requests
from datetime import datetime
from pathlib import Path

# Load credentials for Claude API key
_CREDS_PATH = Path.home() / "projects/cirrus-digest/config/credentials.json"
try:
    with open(_CREDS_PATH) as f:
        _CREDS = json.load(f)
except Exception:
    _CREDS = {}

CLAUDE_API_KEY   = _CREDS.get("anthropic_api_key", "")
# Use Haiku for tool loops — fast and cheap; reasoning quality comes from the loop
CLAUDE_MODEL     = _CREDS.get("claude_tool_model", "claude-haiku-4-5-20251001")
CLAUDE_API_URL   = "https://api.anthropic.com/v1/messages"

# Tool call log — writes to same dir as bot.log for easy inspection
_LOG_PATH = Path.home() / "projects/cirrus-digest/tool_calls.log"

def _log_tool_call(name: str, args: dict, result: str, ok: bool) -> None:
    """Append one line to tool_calls.log so errors are visible without SSH."""
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        status = "OK" if ok else "ERR"
        preview = result[:120].replace("\n", " ") if result else ""
        with open(_LOG_PATH, "a") as f:
            f.write(f"[{ts}] [{status}] {name}({json.dumps(args)}) → {preview}\n")
    except Exception:
        pass

# ── Tool Schemas (Claude API format) ─────────────────────────────────────────

TOOL_SCHEMAS = [
    {
        "name": "check_network_speed",
        "description": (
            "Run a network speed test on CIRRUS and return download speed, "
            "upload speed, and latency. Use when asked about internet speed, "
            "network performance, bandwidth, or connection quality."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "check_disk_space",
        "description": (
            "Check disk usage and available space on CIRRUS. Use when asked "
            "about storage, free space, disk usage, or how full the drive is."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "check_system_health",
        "description": (
            "Check CIRRUS system health: CPU load average, memory usage. "
            "Use when asked about CPU, RAM, memory, system load, or performance."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "list_ollama_models",
        "description": (
            "List all AI models currently installed in Ollama on CIRRUS. "
            "Use when asked what models are available, installed, or downloaded."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "check_tool_errors",
        "description": (
            "Read recent tool call results from CIRRUS's tool_calls.log. "
            "Use when asked if there are any errors, what failed recently, "
            "why a tool returned wrong data, or to self-diagnose a problem."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "lines": {
                    "type": "integer",
                    "description": "Number of recent log lines to return (default 20)."
                }
            },
            "required": []
        }
    },
    {
        "name": "check_service_status",
        "description": (
            "Check whether a launchd service is running on CIRRUS. "
            "Omit service_name to list all com.cirrus.* services. "
            "Use when asked if the bot, offer app, or any service is running."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "service_name": {
                    "type": "string",
                    "description": (
                        "The launchd job label, e.g. 'com.cirrus.bot', "
                        "'com.cirrus.daily', 'com.cirrus.offer'. "
                        "Omit to list all cirrus services."
                    )
                }
            },
            "required": []
        }
    },
]

# ── Tool Dispatch ─────────────────────────────────────────────────────────────

def _load_tool_functions():
    """Lazily load tool implementations to keep imports fast."""
    tools_dir = Path(__file__).parent
    if str(tools_dir) not in sys.path:
        sys.path.insert(0, str(tools_dir))
    from system_tools import TOOL_FUNCTIONS
    return TOOL_FUNCTIONS


def call_tool(name: str, args: dict) -> str:
    """Dispatch a tool call by name, log result, and return as a string."""
    try:
        fns = _load_tool_functions()
        if name not in fns:
            msg = f"Unknown tool: {name}"
            _log_tool_call(name, args, msg, ok=False)
            return msg
        result = fns[name](**args)
        # Flag if the tool returned "unknown" values — log as ERR for visibility
        has_unknown = isinstance(result, str) and "unknown" in result.lower()
        _log_tool_call(name, args, result, ok=not has_unknown)
        return result
    except Exception as e:
        msg = f"Tool '{name}' error: {e}"
        _log_tool_call(name, args, msg, ok=False)
        return msg


# ── Claude API Tool Loop ───────────────────────────────────────────────────────

def ask_with_tools(question: str, context: str = "") -> tuple[str, str]:
    """
    Ask a question using Claude API with tool-calling support.
    Returns (answer, model_name) where model_name indicates what handled it.

    Claude reasons about the question, calls tools if needed, then synthesizes
    a final answer. Supports up to 5 tool-call rounds before forcing a response.

    context: optional RAG text to include as background knowledge.
    """
    if not CLAUDE_API_KEY:
        return "", ""

    system_prompt = (
        "You are CIRRUS, an AI assistant running on a Mac Mini home server. "
        "You have access to tools that let you check live system state. "
        "When a question requires current data (speed, disk, services, models), "
        "use the appropriate tool — don't guess or make up numbers. "
        "Be concise and direct in your final answer."
    )
    if context:
        system_prompt += f"\n\nBackground knowledge from past digests:\n{context}"

    messages = [{"role": "user", "content": question}]
    headers = {
        "x-api-key": CLAUDE_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    for _round in range(5):  # max 5 tool-call rounds
        payload = {
            "model": CLAUDE_MODEL,
            "max_tokens": 1024,
            "system": system_prompt,
            "tools": TOOL_SCHEMAS,
            "messages": messages,
        }

        try:
            resp = requests.post(CLAUDE_API_URL, headers=headers,
                                 json=payload, timeout=60)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            return f"Claude API error: {e}", CLAUDE_MODEL

        stop_reason = data.get("stop_reason", "")
        content     = data.get("content", [])

        if stop_reason == "end_turn":
            # Extract the final text answer
            for block in content:
                if block.get("type") == "text":
                    return block["text"].strip(), CLAUDE_MODEL
            return "No response text received.", CLAUDE_MODEL

        if stop_reason == "tool_use":
            # Add Claude's response (with tool_use blocks) to history
            messages.append({"role": "assistant", "content": content})

            # Execute each tool call and collect results
            tool_results = []
            for block in content:
                if block.get("type") == "tool_use":
                    tool_name = block["name"]
                    tool_args = block.get("input", {})
                    tool_id   = block["id"]

                    result = call_tool(tool_name, tool_args)

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": result,
                    })

            # Add tool results back as a user message
            messages.append({"role": "user", "content": tool_results})
            continue  # next round — Claude synthesizes

        # Unexpected stop reason
        break

    return "Could not complete the request after tool calls.", CLAUDE_MODEL
