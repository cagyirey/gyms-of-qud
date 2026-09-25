import importlib.util
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from qudgym import MockBackend, QudEnv

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_KEY_TOKENS = ("rng", "snapshot", "state_hash", "blueprint", "oracle", "save", "secret", "hidden")
INTEGRATION = ROOT / "integrations/nemo_gym/qudgym"


def _task_data_module():
    path = INTEGRATION / "task_data.py"
    spec = importlib.util.spec_from_file_location("qudgym_nemo_task_data", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _forbidden_keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key).lower()
            yield from _forbidden_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _forbidden_keys(child)


def _contains_forbidden_key(value):
    return any(
        token in key
        for key in _forbidden_keys(value)
        for token in FORBIDDEN_KEY_TOKENS
    )


def test_nemo_task_contract_is_typed_and_server_only():
    module = _task_data_module()
    row = module.TaskData.model_validate({
        "seed": 7,
        "max_decisions": 16,
        "representation": "native",
    })
    assert row.model_dump() == {
        "seed": 7,
        "max_decisions": 16,
        "representation": "native",
    }
    with pytest.raises(ValidationError):
        module.TaskData.model_validate({"seed": -1})
    with pytest.raises(ValidationError):
        module.TaskData.model_validate({"max_decisions": 0})


def test_nemo_example_rows_are_bounded_and_do_not_leak_server_state():
    module = _task_data_module()
    for path in sorted((INTEGRATION / "data").glob("*.jsonl")):
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
        assert rows
        for row in rows:
            module.TaskData.model_validate(row)
            assert row["max_decisions"] == 16
            assert row["responses_create_params"]["input"]
            assert not _contains_forbidden_key(row)


def test_model_visible_observations_have_no_hidden_backend_state_keys():
    with QudEnv(MockBackend()) as env:
        result = env.reset(seed=7)
        for action in ("move:E", "move:E", "answer:open", "move:E", "move:E"):
            assert not _contains_forbidden_key(result.observation.model_dump(mode="json"))
            result = env.step(action)
        assert not _contains_forbidden_key(result.observation.model_dump(mode="json"))


def test_nemo_configs_use_the_builtin_gymnasium_agent_and_keep_mock_unverified():
    for name in ("qudgym.yaml", "agent_eye.yaml"):
        text = (INTEGRATION / "configs" / name).read_text(encoding="utf-8")
        assert "allowed_agents: [gymnasium_agent]" in text
        assert "max_steps: 16" in text
        assert "num_workers: 1" in text
        assert "verified: false" in text
        assert "no live Qud yet" in text
        assert "/Users/" not in text


def test_native_wrapper_delegates_to_nemo_gym_without_a_second_policy_loop():
    text = (ROOT / "scripts/run_nemo_gym_mock.sh").read_text(encoding="utf-8")
    for required in (
        "env start",
        "eval run --no-serve",
        "eval profile",
        "gymnasium_agent",
        "model_call_capture_dir",
        "upload_rollouts=false",
        "wandb_project=null",
        "wandb_name=null",
        "wandb_api_key=null",
        "mlflow_tracking_uri=null",
        "mlflow_tracking_token=null",
        "mlflow_experiment_name=null",
        "mlflow_run_name=null",
        "NEMO_GYM_MLFLOW_ENABLED",
        "NEMO_GYM_MLFLOW_TRACKING_URI",
        "NEMO_GYM_MLFLOW_EXPERIMENT_NAME",
        "NEMO_GYM_MLFLOW_RUN_NAME",
        "NEMO_GYM_MLFLOW_TOKEN_ENV",
        "NEMO_GYM_MLFLOW_UPLOAD_ROLLOUTS",
        "expected_rollout_count",
        "reward_profile_completion_pct",
        "NEMO_GYM_MODEL_API_KEY_ENV",
        "NEMO_GYM_WRAPPER_POLICY_KEY",
        "umask 077",
    ):
        assert required in text
    assert "--model-api-key" not in text
    assert "qudgym.policy" not in text
    assert "QudEnv" not in text
    assert "gym eval export" not in text
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "NEMO_GYM_MODEL_TYPE=vllm_model" in workflow
    assert "scripts/run_nemo_gym_mock.sh" in workflow
    assert "GYM_PGID=$GYM_PID" in text
    assert 'kill -0 -- "-$GYM_PGID"' in text
    assert '"++upload_rollouts=$MLFLOW_UPLOAD_ROLLOUTS"' in text
    assert r'"++mlflow_tracking_uri=\${oc.env:NEMO_GYM_WRAPPER_MLFLOW_TRACKING_URI}"' in text
    assert "START_EXPORTER_OVERRIDES" in text
    assert "EVAL_EXPORTER_OVERRIDES" in text
    assert '"${START_EXPORTER_OVERRIDES[@]}"' in text
    assert 'unset "$MLFLOW_TOKEN_ENV"' in text
    assert 'unset NEMO_GYM_MODEL_API_KEY' in text
    assert text.index('unset "$MLFLOW_TOKEN_ENV"') < text.index('python3 - "$GYM_BIN" "${START_ARGS[@]}"')
    assert '"wandb_disabled": True' in text
    assert '"mlflow_requested": mlflow_requested' in text
    assert '"mlflow_status": "requested_not_verified"' in text
    assert '"mlflow_upload_rollouts_requested": mlflow_upload_rollouts_requested' in text


def test_adapter_source_manifest_is_committed_and_contains_no_absolute_paths():
    manifest = INTEGRATION / "source_manifest.json"
    document = json.loads(manifest.read_text(encoding="utf-8"))
    assert document["algorithm"] == "sha256"
    assert document["files"]
    assert "/Users/" not in manifest.read_text(encoding="utf-8")




def test_source_requirements_do_not_embed_machine_paths():
    text = (INTEGRATION / "requirements.txt").read_text(encoding="utf-8")
    assert "file://" not in text
    assert "/Users/" not in text


def test_nemo_adapter_does_not_duplicate_model_or_token_telemetry():
    text = (INTEGRATION / "app.py").read_text(encoding="utf-8")
    for forbidden in (
        "llm_call",
        "export_genai_otel",
        "input_tokens",
        "output_tokens",
        "SessionRecorder",
    ):
        assert forbidden not in text
