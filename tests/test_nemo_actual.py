import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

pytest.importorskip("nemo_gym")
pytest.importorskip("resources_servers.gymnasium")

from nemo_gym.openai_utils import (
    NeMoGymResponse,
    NeMoGymResponseOutputMessage,
    NeMoGymResponseOutputText,
)
from nemo_gym.server_utils import ServerClient

ROOT = Path(__file__).resolve().parents[1]
APP_PATH = ROOT / "integrations/nemo_gym/qudgym/app.py"


def _load_app():
    name = "qudgym_nemo_actual_app"
    spec = importlib.util.spec_from_file_location(name, APP_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _server(app):
    config = app.QudGymConfig(host="127.0.0.1", port=0, entrypoint="app.py", name="qudgym")
    return app.QudGymServer(config=config, server_client=MagicMock(spec=ServerClient))


def _response(action_id, decision_id):
    return NeMoGymResponse(
        id="response",
        created_at=0.0,
        model="model",
        object="response",
        output=[NeMoGymResponseOutputMessage(
            id="message",
            content=[NeMoGymResponseOutputText(annotations=[], text=json.dumps({
                "action_id": action_id,
                "decision_id": decision_id,
            }), type="output_text")],
            role="assistant",
            status="completed",
            type="message",
        )],
        parallel_tool_calls=False,
        tool_choice="auto",
        tools=[],
    )


def test_actual_nemo_gym_adapter_completes_the_mock_episode():
    app = _load_app()

    async def scenario():
        server = _server(app)
        observation, info = await server.reset({"seed": 7, "max_decisions": 16}, "session")
        assert info["is_mock"] is True
        for index, action in enumerate(("move:E", "move:E", "answer:open", "move:E", "move:E"), 1):
            current = json.loads(observation)
            result = await server.step(
                _response(action, current["decision_id"]),
                {"_ng_step_request_id": f"step-{index}"},
                "session",
            )
            observation, reward, terminated, truncated, step_info = result
            if terminated or truncated:
                break
        assert reward == 1.0
        assert terminated is True
        assert truncated is False
        assert step_info["outcome"] == "success"
        assert step_info["turns_elapsed"] == 4
        assert step_info["decisions_elapsed"] == 5
        await server.close_session("session")

    asyncio.run(scenario())


def test_actual_nemo_gym_adapter_rejects_invalid_task_rows():
    app = _load_app()

    async def scenario():
        server = _server(app)
        with pytest.raises(HTTPException) as exc:
            await server.reset({"seed": -1}, "session")
        assert exc.value.status_code == 422

    asyncio.run(scenario())
