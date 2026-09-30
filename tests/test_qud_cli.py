"""Exercise the actual plan CLI against scripted websocket replies, not Qud."""
from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def cli(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("qudgym_plan_cli", ROOT / "scripts/qud.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    control = tmp_path / "control.txt"
    control.write_text("ws://127.0.0.1:8765/rpc\n" + "t" * 32 + "\n")
    monkeypatch.setattr(module, "CONTROL", control)
    monkeypatch.setattr(module, "PRESET", tmp_path / "preset.txt")
    monkeypatch.setattr(module, "LOG", tmp_path / "diagnostic.txt")
    # Yield to asyncio without making every status test sleep for a second.
    real_sleep = asyncio.sleep

    async def yield_now(_):
        await real_sleep(0)

    monkeypatch.setattr(module.asyncio, "sleep", yield_now)
    return module


class Socket:
    def __init__(self, status=None, *, observe_error=False, reply_change=None):
        self.status = status if status is not None else {"running": False, "trace": "finished", "steps_taken": 3}
        self.observe_error = observe_error
        self.reply_change = reply_change
        self.sent = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.closed = True

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def recv(self):
        request = self.sent[-1]
        op = request["op"]
        observation = {"episode_id": "test", "turn": 1, "phase": "command", "player": {}}
        result = {
            "reset": {"observation": observation},
            "plan": {"plan_actions": "wait", "plan_steps": 1},
            "plan_status": self.status,
            "observe": observation,
        }[op]
        reply = {"protocol_version": "0.1", "request_id": request["request_id"], "result": result}
        if op == "observe" and self.observe_error:
            reply.pop("result")
            reply["error"] = {"code": "stale_decision", "message": "no fresh boundary"}
        if op == "plan_status" and "_error" in self.status:
            reply.pop("result")
            reply["error"] = self.status["_error"]
        if self.reply_change is not None:
            reply = self.reply_change(reply)
        return json.dumps(reply)


def run(cli, monkeypatch, socket, *, seconds=1, watch=False):
    monkeypatch.setattr(cli.websockets, "connect", lambda *a, **kw: socket)
    return asyncio.run(cli.play("wait", seconds=seconds, watch=watch))


@pytest.mark.parametrize("trace", ["unsupported zzz", "unavailable target", "exhausted", "", None])
def test_failed_or_unclassified_plan_is_not_success(cli, monkeypatch, trace):
    socket = Socket({"running": False, "trace": trace, "steps_taken": 0})
    assert run(cli, monkeypatch, socket) == 1
    assert socket.closed


def test_rpc_error_is_not_success(cli, monkeypatch):
    socket = Socket({"_error": {"code": "internal_error", "message": "failed"}})
    assert run(cli, monkeypatch, socket) == 1


def test_missing_status_is_not_completion(cli, monkeypatch):
    assert run(cli, monkeypatch, Socket({})) == 1


def test_failed_final_observation_is_not_success(cli, monkeypatch):
    assert run(cli, monkeypatch, Socket(observe_error=True)) == 1


def test_time_budget_is_failure_without_a_followup_observe(cli, monkeypatch):
    socket = Socket({"running": True, "trace": "stepped wait", "steps_taken": 1})
    assert run(cli, monkeypatch, socket, seconds=0.01) == 124
    assert "observe" not in [p["op"] for p in socket.sent]
    assert socket.closed


def test_finished_plan_and_final_observation_succeed(cli, monkeypatch):
    socket = Socket()
    assert run(cli, monkeypatch, socket) == 0
    assert [p["op"] for p in socket.sent] == ["reset", "plan", "plan_status", "observe"]


def test_watch_does_not_require_an_existing_log(cli, monkeypatch):
    assert run(cli, monkeypatch, Socket(), watch=True) == 0


@pytest.mark.parametrize("change", [
    lambda r: {**r, "request_id": "another-request"},
    lambda r: {k: v for k, v in r.items() if k != "request_id"},
    lambda r: {**r, "protocol_version": "not-0.1"},
    lambda r: {k: v for k, v in r.items() if k != "result"},
    lambda r: {**r, "error": {"code": "conflicting", "message": "also a result"}},
])
def test_unusable_reply_stops_the_connection_without_resending(cli, change):
    socket = Socket(reply_change=change)
    game = cli.Game(socket)

    async def scenario():
        with pytest.raises(RuntimeError):
            await game.call("plan", program="wait")
        with pytest.raises(RuntimeError):
            await game.call("plan", program="wait")

    asyncio.run(scenario())
    assert len(socket.sent) == 1


def test_lost_reply_blocks_new_requests_on_that_connection(cli):
    class LostReply(Socket):
        async def recv(self):
            raise TimeoutError("simulated lost reply")

    socket = LostReply()
    game = cli.Game(socket)

    async def scenario():
        with pytest.raises(TimeoutError):
            await game.call("plan", program="wait")
        with pytest.raises(RuntimeError):
            await game.call("plan", program="wait")

    asyncio.run(scenario())
    assert len(socket.sent) == 1


@pytest.mark.parametrize("seconds", [0, -1, float("nan"), float("inf")])
def test_invalid_budget_rejected_before_any_request(cli, monkeypatch, seconds):
    socket = Socket()
    with pytest.raises(ValueError):
        run(cli, monkeypatch, socket, seconds=seconds)
    assert socket.sent == []
