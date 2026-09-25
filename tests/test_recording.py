import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from qudgym import MockBackend, QudEnv
from qudgym.compat import DiagnosticEvent
from qudgym.errors import TransportUncertain
from qudgym.recording import (
    AtofMarkEvent,
    AtofScopeEvent,
    OptionalDependencyError,
    RecordingConfig,
    SessionRecorder,
    export_genai_otel,
    genai_span_attributes,
    read_atof,
)

ROOT = Path(__file__).resolve().parents[1]


def test_session_recorder_writes_paired_sanitized_atof_scopes(tmp_path):
    path = tmp_path / "episode.atof.jsonl"
    with QudEnv(MockBackend()) as env, SessionRecorder(env, path) as recorder:
        recorder.reset(seed=7)
        recorder.step("move:E")

    events = read_atof(path)
    assert any(isinstance(event, AtofMarkEvent) and event.name == "episode_reset" for event in events)
    starts = [event for event in events if isinstance(event, AtofScopeEvent) and event.scope_category == "start"]
    ends = [event for event in events if isinstance(event, AtofScopeEvent) and event.scope_category == "end"]
    assert len(starts) == len(ends)
    assert {event.uuid for event in starts} == {event.uuid for event in ends}
    serialized = path.read_text(encoding="utf-8")
    assert "qudgym.step" in serialized
    assert "move:E" in serialized
    assert "save" not in serialized.lower()
    assert "snapshot" not in serialized.lower()


def test_session_recorder_rejects_invalid_action_without_writing_a_step(tmp_path):
    path = tmp_path / "episode.atof.jsonl"
    with QudEnv(MockBackend()) as env, SessionRecorder(env, path) as recorder:
        recorder.reset(seed=1)
        with pytest.raises(ValueError):
            recorder.step("bad\naction")

    events = read_atof(path)
    assert not any(isinstance(event, AtofScopeEvent) and event.name == "qudgym.decision" for event in events)


def test_uncertain_mutation_is_recorded_without_a_retry(tmp_path):
    class UncertainBackend(MockBackend):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def step(self, *args, **kwargs):
            self.calls += 1
            raise TransportUncertain("private transport detail")

    path = tmp_path / "uncertain.atof.jsonl"
    backend = UncertainBackend()
    with QudEnv(backend) as env, SessionRecorder(env, path) as recorder:
        recorder.reset(seed=1)
        with pytest.raises(TransportUncertain):
            recorder.step("wait")

    assert backend.calls == 1
    text = path.read_text(encoding="utf-8")
    assert '"error_type":"transport_uncertain"' in text
    assert "private transport detail" not in text


def test_diagnostic_event_contract_accepts_startup_evidence():
    event = DiagnosticEvent.model_validate({
        "schema_version": "qudgym-compat/1",
        "event": "cache-reset",
        "game_build": "2.0.4",
        "marketing_version": "2.0.4",
        "core_version": "2.0.4.55",
        "mod_initialized": True,
        "thread_id": 7,
        "core_thread_id": 3,
        "is_core_thread": False,
        "active_mods": [{"mod_id": "QudGymDiagnostic", "load_order": 1, "active": True}],
        "diagnostic_mod": {"mod_id": "QudGymDiagnostic", "load_order": 1, "active": True},
        "hooks": [{
            "name": "XRL.ModSensitiveCacheInitAttribute",
            "available": True,
            "thread_id": 7,
            "detail": "type-resolution-only",
        }],
    })
    assert event.core_version == "2.0.4.55"
    assert event.hooks[0].detail == "type-resolution-only"


def test_diagnostic_event_rejects_missing_thread_for_resolved_hook():
    with pytest.raises(ValidationError):
        DiagnosticEvent.model_validate({
            "schema_version": "qudgym-compat/1",
            "event": "cache-reset",
            "game_build": "2.0.4",
            "thread_id": 7,
            "hooks": [{"name": "XRL.EndTurnEvent", "available": True}],
        })


def test_llm_call_is_content_opt_in_and_keeps_model_evidence(tmp_path):
    path = tmp_path / "llm.atof.jsonl"
    with QudEnv(MockBackend()) as env, SessionRecorder(
        env, path, config=RecordingConfig(capture_content=True),
    ) as recorder:
        recorder.reset(seed=1)
        with recorder.llm_call(
            model_name="synthetic-model",
            provider_name="synthetic",
            input_messages=[{"role": "user", "content": "hello"}],
        ) as call:
            call.output_text = "world"
            call.response_id = "response-1"
            call.finish_reasons = ["stop"]
            call.usage = {"input_tokens": 2, "output_tokens": 1}

    events = read_atof(path)
    llm_events = [event for event in events if isinstance(event, AtofScopeEvent) and event.category == "llm"]
    assert len(llm_events) == 2
    assert "hello" in path.read_text(encoding="utf-8")
    assert llm_events[0].category_profile == {"model_name": "synthetic-model"}
    assert llm_events[1].data is not None

    private_path = path.with_name("private.atof.jsonl")
    with QudEnv(MockBackend()) as env, SessionRecorder(env, private_path) as recorder:
        recorder.reset(seed=1)
        with recorder.llm_call(
            model_name="synthetic-model",
            input_messages=[{"role": "user", "content": "do not capture"}],
        ) as call:
            call.output_text = "also private"
    assert "do not capture" not in private_path.read_text(encoding="utf-8")
    assert "also private" not in private_path.read_text(encoding="utf-8")


def test_genai_projection_uses_standard_attributes_without_content():
    event = AtofScopeEvent(
        uuid="llm-1", parent_uuid="agent-1", timestamp="2026-01-01T00:00:00.000001Z",
        name="model", scope_category="start", category="llm",
        category_profile={"model_name": "synthetic-model"},
        metadata={"provider_name": "synthetic", "session_id": "session-1"},
        data={"messages": [{"role": "user", "content": "private"}]},
    )
    attributes = genai_span_attributes(event)
    assert attributes["gen_ai.operation.name"] == "chat"
    assert attributes["gen_ai.provider.name"] == "synthetic"
    assert attributes["gen_ai.request.model"] == "synthetic-model"
    assert "gen_ai.input.messages" not in attributes
    assert isinstance(genai_span_attributes(event, capture_content=True)["gen_ai.input.messages"], str)


def test_otel_projection_requires_optional_api(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text(json.dumps({
        "kind": "scope", "scope_category": "start", "atof_version": "0.1",
        "uuid": "scope-1", "parent_uuid": None,
        "timestamp": "2026-01-01T00:00:00.000001Z", "name": "qudgym-agent",
        "attributes": [], "category": "agent", "category_profile": None,
        "data": None, "data_schema": None, "metadata": {},
    }) + "\n", encoding="utf-8")
    try:
        import opentelemetry  # noqa: F401
    except ImportError:
        with pytest.raises(OptionalDependencyError):
            export_genai_otel(path)
    else:
        pytest.skip("optional OpenTelemetry API is installed; SDK integration is environment-specific")


def test_diagnostic_mod_is_startup_only_and_does_not_restore_transport():
    source = (ROOT / "mod/QudGymCompat/Diagnostic.cs").read_text(encoding="utf-8")
    code = "\n".join(line.split("//", 1)[0] for line in source.splitlines())
    manifest = json.loads((ROOT / "mod/QudGymCompat/manifest.json").read_text(encoding="utf-8"))
    assert manifest == {
        "ID": "QudGymDiagnostic",
        "Title": "QudGym Compatibility Diagnostic",
        "Version": "0.1.0",
    }
    for required in (
        "[ModSensitiveCacheInit]", "ModManager.ResolveType", "ModManager.CoreVersion",
        "ModManager.MarketingVersion", "XRL.Core.XRLCore.IsCoreThread",
    ):
        assert required in source
    for forbidden in (
        "File.", "persistentDataPath", "Keyboard", "Harmony", "WebSocket",
        "Suave", "The.Game", "The.Player", "Save", "Snapshot",
    ):
        assert forbidden not in code


def test_recording_config_is_bounded():
    config = RecordingConfig(max_events=2, max_bytes=1024)
    assert config.max_events == 2
    with pytest.raises(ValidationError):
        RecordingConfig(max_events=1)
