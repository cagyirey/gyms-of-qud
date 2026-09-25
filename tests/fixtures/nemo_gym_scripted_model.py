"""Test-only deterministic OpenAI Responses/Chat model for native Gym CI.

This fixture is not a QudGym runtime component, model gateway, or training
service. It only exercises NeMo Gym's public model-server contract.
"""
from __future__ import annotations

import itertools
import json
import time
from typing import Any

from fastapi import FastAPI, Request

app = FastAPI()
_response_ids = itertools.count(1)


def _text(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict) and item.get("type") in {"input_text", "output_text", "text"}:
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts) or None
    return None


def _select_action(body: dict[str, Any]) -> str:
    items = body.get("input")
    if isinstance(items, str):
        items = [{"role": "user", "content": items}]
    if not isinstance(items, list):
        items = []
    for item in reversed(items):
        if not isinstance(item, dict) or item.get("role") != "user":
            continue
        text = _text(item.get("content"))
        if not isinstance(text, str):
            continue
        try:
            observation = json.loads(text)
        except json.JSONDecodeError:
            continue
        if not isinstance(observation, dict):
            continue
        decision_id = observation.get("decision_id")
        actions = observation.get("actions")
        if not isinstance(decision_id, str) or not isinstance(actions, list):
            continue
        ids = {
            action.get("id")
            for action in actions
            if isinstance(action, dict) and isinstance(action.get("id"), str)
        }
        wanted = "answer:open" if observation.get("prompt") else "move:E"
        action_id = wanted if wanted in ids else ("wait" if "wait" in ids else sorted(ids)[0])
        return json.dumps({"action_id": action_id, "decision_id": decision_id}, separators=(",", ":"))
    return json.dumps({"action_id": "wait", "decision_id": "missing"}, separators=(",", ":"))


@app.get("/health")
async def health() -> dict[str, bool]:
    return {"ok": True}


@app.get("/v1/models")
async def models() -> dict[str, Any]:
    return {"object": "list", "data": [{"id": "qudgym-scripted-smoke", "object": "model"}]}


@app.post("/v1/responses")
async def responses(request: Request) -> dict[str, Any]:
    body = await request.json()
    response_number = next(_response_ids)
    output = _select_action(body)
    return {
        "id": f"resp_qudgym_scripted_{response_number}",
        "created_at": time.time(),
        "model": body.get("model", "qudgym-scripted-smoke"),
        "object": "response",
        "output": [{
            "id": f"msg_qudgym_scripted_{response_number}",
            "content": [{"annotations": [], "text": output, "type": "output_text"}],
            "role": "assistant",
            "status": "completed",
            "type": "message",
        }],
        "parallel_tool_calls": False,
        "tool_choice": "auto",
        "tools": [],
        "usage": {
            "input_tokens": 16,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 8,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 24,
        },
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> dict[str, Any]:
    body = await request.json()
    output = _select_action({"input": body.get("messages", [])})
    return {
        "id": "chatcmpl_qudgym_scripted",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body.get("model", "qudgym-scripted-smoke"),
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": output},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 16, "completion_tokens": 8, "total_tokens": 24},
    }
