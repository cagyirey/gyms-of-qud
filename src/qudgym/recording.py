"""Sanitized session recording with ATOF, ATIF, and GenAI OTel projections.

The recorder owns environment/session telemetry only. It does not own model
routing, prompt construction, token accounting, or a live Qud transport. The
ATOF stream is the hand-off point: NVIDIA NeMo's ATOF-to-ATIF converter can
consume it, and the optional OTel projection below is a small direct path for
non-NeMo runners.
"""
from __future__ import annotations

import json
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any, Literal, Self, TypeAlias
from uuid import uuid4

from pydantic import Field, JsonValue, TypeAdapter, field_validator, model_validator

from .env import QudEnv
from .errors import QudGymError
from .models import Identifier, Model, Transition

ATOF_VERSION = "0.1"


class RecordingLimitError(ValueError):
    """The configured event or byte budget was exhausted."""


class OptionalDependencyError(RuntimeError):
    """An optional exporter dependency is not installed."""


class AtofBase(Model):
    atof_version: Literal["0.1"] = ATOF_VERSION
    uuid: Identifier
    parent_uuid: Identifier | None = None
    timestamp: str
    name: Identifier
    data: JsonValue | None = None
    data_schema: dict[str, str] | None = None
    metadata: dict[str, JsonValue] | None = None

    @field_validator("timestamp")
    @classmethod
    def timestamp_is_rfc3339(cls, value: str) -> str:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("ATOF timestamps must be RFC 3339 strings") from exc
        if parsed.tzinfo is None:
            raise ValueError("ATOF timestamps must include a UTC offset")
        return value

    @field_validator("data_schema")
    @classmethod
    def data_schema_shape(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        if value is not None and set(value) != {"name", "version"}:
            raise ValueError("ATOF data_schema requires exactly name and version")
        return value


class AtofScopeEvent(AtofBase):
    kind: Literal["scope"] = "scope"
    scope_category: Literal["start", "end"]
    attributes: tuple[str, ...] = ()
    category: str
    category_profile: dict[str, JsonValue] | None = None

    @field_validator("attributes")
    @classmethod
    def attributes_are_canonical(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if list(value) != sorted(set(value)):
            raise ValueError("ATOF attributes must be sorted and deduplicated")
        return value

    @field_validator("category")
    @classmethod
    def category_is_present(cls, value: str) -> str:
        if not value:
            raise ValueError("ATOF scope category must not be empty")
        return value

    @model_validator(mode="after")
    def custom_category_has_subtype(self) -> AtofScopeEvent:
        if self.category == "custom" and not (self.category_profile or {}).get("subtype"):
            raise ValueError("custom ATOF categories require category_profile.subtype")
        return self


class AtofMarkEvent(AtofBase):
    kind: Literal["mark"] = "mark"
    category: str | None = None
    category_profile: dict[str, JsonValue] | None = None

    @model_validator(mode="after")
    def custom_category_has_subtype(self) -> AtofMarkEvent:
        if self.category == "custom" and not (self.category_profile or {}).get("subtype"):
            raise ValueError("custom ATOF categories require category_profile.subtype")
        return self


AtofEvent: TypeAlias = AtofScopeEvent | AtofMarkEvent
_ATOF_ADAPTER = TypeAdapter(AtofEvent)


def _timestamp_to_microseconds(value: str) -> int:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("ATOF timestamps must include a UTC offset")
    return int(parsed.timestamp() * 1_000_000)


def _timestamp_to_nanoseconds(value: str) -> int:
    return _timestamp_to_microseconds(value) * 1_000


def _now_rfc3339(previous: int) -> tuple[str, int]:
    current = max(previous + 1, time.time_ns() // 1_000)
    seconds, micros = divmod(current, 1_000_000)
    value = datetime.fromtimestamp(seconds, UTC).replace(microsecond=micros)
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z"), current


def read_atof(path: str | Path, *, max_line_bytes: int = 1_048_576) -> list[AtofEvent]:
    """Read and validate an ATOF JSONL stream, sorted by event time."""
    if max_line_bytes < 1:
        raise ValueError("max_line_bytes must be positive")
    events: list[AtofEvent] = []
    with Path(path).open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            if len(line.encode("utf-8")) > max_line_bytes:
                raise ValueError(f"ATOF line {line_number} exceeds the size limit")
            events.append(_ATOF_ADAPTER.validate_json(line))
    return sorted(events, key=lambda event: _timestamp_to_microseconds(event.timestamp))


class RecordingConfig(Model):
    session_id: Identifier = Field(default_factory=lambda: uuid4().hex)
    agent_name: Identifier = "qudgym-agent"
    agent_version: Identifier = "0.1.0"
    provider_name: Identifier = "qudgym"
    capture_content: bool = False
    max_events: int = Field(default=10_000, ge=2, le=1_000_000, strict=True)
    max_bytes: int = Field(default=16 * 1024 * 1024, ge=1_024, le=1024 * 1024 * 1024, strict=True)


@dataclass
class LLMCall:
    """Mutable result fields for one in-process model call."""

    input_messages: list[dict[str, JsonValue]] = field(default_factory=list)
    output_text: str = ""
    tool_calls: list[dict[str, JsonValue]] = field(default_factory=list)
    response_id: str | None = None
    finish_reasons: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)


class SessionRecorder:
    """Write one bounded ATOF stream for a QudGym session.

    The stream is create-only and flushed after every event. A failed or
    uncertain mutation is recorded as an error and is never retried here.
    Observations are the already-sanitized `QudEnv` values; no backend or
    save state is queried by this class.
    """

    def __init__(self, env: QudEnv, path: str | Path, *, config: RecordingConfig | None = None):
        self.env = env
        self.config = config or RecordingConfig()
        self.path = Path(path)
        self._file = self.path.open("x", encoding="utf-8")
        self._lock = RLock()
        self._events = 0
        self._bytes = 0
        self._last_microseconds = 0
        self._root_uuid: str | None = None
        self._root_closed = False
        self._closed = False

    @property
    def event_count(self) -> int:
        return self._events

    def _next_timestamp(self) -> str:
        value, micros = _now_rfc3339(self._last_microseconds)
        self._last_microseconds = micros
        return value

    def _metadata(self) -> dict[str, JsonValue]:
        return {
            "session_id": self.config.session_id,
            "version": self.config.agent_version,
            "agent_version": self.config.agent_version,
            "provider_name": self.config.provider_name,
        }

    def _scope(
        self,
        *,
        uuid: str,
        parent_uuid: str | None,
        scope_category: Literal["start", "end"],
        name: str,
        category: str,
        data: JsonValue | None = None,
        category_profile: dict[str, JsonValue] | None = None,
        metadata: dict[str, JsonValue] | None = None,
    ) -> AtofScopeEvent:
        return AtofScopeEvent(
            uuid=uuid,
            parent_uuid=parent_uuid,
            timestamp=self._next_timestamp(),
            name=name,
            scope_category=scope_category,
            attributes=(),
            category=category,
            category_profile=category_profile,
            data=data,
            metadata=self._metadata() if metadata is None else metadata,
        )

    def _write(self, event: AtofEvent) -> None:
        if self._closed:
            raise ValueError("recording is closed")
        if self._events >= self.config.max_events:
            raise RecordingLimitError("ATOF event budget exhausted")
        line = json.dumps(
            event.model_dump(mode="json"),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ) + "\n"
        encoded = line.encode("utf-8")
        if self._bytes + len(encoded) > self.config.max_bytes:
            raise RecordingLimitError("ATOF byte budget exhausted")
        self._file.write(line)
        self._file.flush()
        self._events += 1
        self._bytes += len(encoded)

    @staticmethod
    def _error_data(error: Exception) -> dict[str, JsonValue]:
        if isinstance(error, QudGymError):
            return {"error_type": error.code}
        return {"error_type": type(error).__name__}

    def _ensure_open(self) -> None:
        if self._closed:
            raise ValueError("recording is closed")

    def reset(self, *, seed: int = 0) -> Transition:
        with self._lock:
            self._ensure_open()
            if self._root_uuid is not None:
                raise ValueError("a recorded session can only be reset once")
            self._root_uuid = uuid4().hex
            root = self._root_uuid
            self._write(self._scope(
                uuid=root, parent_uuid=None, scope_category="start",
                name=self.config.agent_name, category="agent",
                data={"seed": seed},
            ))
            try:
                result = self.env.reset(seed=seed)
            except Exception as exc:
                self._write(self._scope(
                    uuid=root, parent_uuid=None, scope_category="end",
                    name=self.config.agent_name, category="agent",
                    data=self._error_data(exc),
                ))
                self._root_closed = True
                raise
            self._write(AtofMarkEvent(
                uuid=uuid4().hex, parent_uuid=root, timestamp=self._next_timestamp(),
                name="episode_reset", data={
                    "role": "system",
                    "content": "QudGym episode initialized",
                    "transition": result.model_dump(mode="json"),
                }, metadata=self._metadata(),
            ))
            return result

    @staticmethod
    def _safe_action(action_id: str) -> str:
        if not isinstance(action_id, str) or not action_id or len(action_id) > 160:
            raise ValueError("action_id must be a non-empty string of at most 160 characters")
        if any(ord(character) < 32 for character in action_id):
            raise ValueError("action_id must not contain control characters")
        return action_id

    def step(self, action_id: str) -> Transition:
        with self._lock:
            self._ensure_open()
            if self._root_uuid is None or self._root_closed:
                raise ValueError("reset before recording steps")
            if self.env.current is None:
                raise ValueError("reset before recording steps")
            action_id = self._safe_action(action_id)
            before = self.env.current.observation
            function_uuid = uuid4().hex
            tool_uuid = uuid4().hex
            tool_call_id = f"qudgym-{function_uuid}"
            function_start = {
                "action_id": action_id,
                "decision_id": before.decision_id,
            }
            self._write(self._scope(
                uuid=function_uuid, parent_uuid=self._root_uuid,
                scope_category="start", name="qudgym.decision", category="function",
                data=function_start,
            ))
            self._write(self._scope(
                uuid=tool_uuid, parent_uuid=function_uuid,
                scope_category="start", name="qudgym.step", category="tool",
                data=function_start,
                category_profile={"tool_call_id": tool_call_id},
            ))
            try:
                result = self.env.step(action_id)
            except Exception as exc:
                error = self._error_data(exc)
                self._write(self._scope(
                    uuid=tool_uuid, parent_uuid=function_uuid,
                    scope_category="end", name="qudgym.step", category="tool",
                    data={"error": error}, category_profile={"tool_call_id": tool_call_id},
                ))
                self._write(self._scope(
                    uuid=function_uuid, parent_uuid=self._root_uuid,
                    scope_category="end", name="qudgym.decision", category="function",
                    data={"error": error},
                ))
                raise
            result_data = result.model_dump(mode="json")
            self._write(self._scope(
                uuid=tool_uuid, parent_uuid=function_uuid,
                scope_category="end", name="qudgym.step", category="tool",
                data={"result": result_data}, category_profile={"tool_call_id": tool_call_id},
            ))
            self._write(self._scope(
                uuid=function_uuid, parent_uuid=self._root_uuid,
                scope_category="end", name="qudgym.decision", category="function",
                data={"outcome": result.metrics.outcome},
            ))
            return result

    def reconcile(self):
        with self._lock:
            self._ensure_open()
            if self._root_uuid is None or self._root_closed:
                raise ValueError("reset before reconciling")
            observation = self.env.reconcile()
            self._write(AtofMarkEvent(
                uuid=uuid4().hex, parent_uuid=self._root_uuid,
                timestamp=self._next_timestamp(), name="request_reconciled",
                data={
                    "role": "system",
                    "content": "Uncertain request reconciled; reward is unknown",
                    "observation": observation.model_dump(mode="json"),
                }, metadata=self._metadata(),
            ))
            return observation

    @contextmanager
    def llm_call(
        self,
        *,
        model_name: str,
        provider_name: str | None = None,
        input_messages: Sequence[Mapping[str, JsonValue]] = (),
    ) -> Iterator[LLMCall]:
        """Record one model call without calculating tokens or owning inference.

        The caller may set the output fields on the yielded object before the
        context exits. Content is included only when `capture_content=True` in
        the recording configuration; response metadata and provider-reported
        usage are safe to retain as pass-through fields.
        """
        with self._lock:
            self._ensure_open()
            if self._root_uuid is None or self._root_closed:
                raise ValueError("reset before recording model calls")
            call = LLMCall(input_messages=[dict(message) for message in input_messages])
            call_uuid = uuid4().hex
            start_data: JsonValue | None = None
            if self.config.capture_content and call.input_messages:
                start_data = {"messages": deepcopy(call.input_messages)}
            metadata = self._metadata()
            if provider_name is not None:
                metadata["provider_name"] = provider_name
            self._write(self._scope(
                uuid=call_uuid, parent_uuid=self._root_uuid,
                scope_category="start", name=model_name, category="llm",
                data=start_data, category_profile={"model_name": model_name},
                metadata=metadata,
            ))
        try:
            yield call
        except Exception as exc:
            with self._lock:
                error = self._error_data(exc)
                error_profile: dict[str, JsonValue] = {"model_name": model_name}
                error_profile["error_type"] = error["error_type"]
                self._write(self._scope(
                    uuid=call_uuid, parent_uuid=self._root_uuid,
                    scope_category="end", name=model_name, category="llm",
                    data=None, category_profile=error_profile, metadata=metadata,
                ))
            raise
        with self._lock:
            end_data: dict[str, JsonValue] = {}
            end_profile: dict[str, JsonValue] = {"model_name": model_name}
            if self.config.capture_content:
                if call.output_text:
                    end_data["content"] = call.output_text
                if call.tool_calls:
                    end_data["tool_calls"] = deepcopy(call.tool_calls)
            if call.response_id is not None:
                end_profile["response_id"] = call.response_id
            if call.finish_reasons:
                end_profile["finish_reasons"] = list(call.finish_reasons)
            if call.usage:
                end_profile["usage"] = dict(call.usage)
            self._write(self._scope(
                uuid=call_uuid, parent_uuid=self._root_uuid,
                scope_category="end", name=model_name, category="llm",
                data=end_data or None, category_profile=end_profile,
                metadata=metadata,
            ))

    def record_mark(self, name: str, data: dict[str, JsonValue]) -> None:
        with self._lock:
            self._ensure_open()
            if self._root_uuid is None or self._root_closed:
                raise ValueError("reset before recording marks")
            self._write(AtofMarkEvent(
                uuid=uuid4().hex, parent_uuid=self._root_uuid,
                timestamp=self._next_timestamp(), name=name, data=data,
                metadata=self._metadata(),
            ))

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                if self._root_uuid is not None and not self._root_closed:
                    final = self.env.current.model_dump(mode="json") if self.env.current else None
                    self._write(self._scope(
                        uuid=self._root_uuid, parent_uuid=None, scope_category="end",
                        name=self.config.agent_name, category="agent",
                        data={"final_transition": final},
                    ))
                    self._root_closed = True
            finally:
                self._file.close()
                self._closed = True

    @contextmanager
    def paused(self) -> Iterator[None]:
        """Explicitly mark a non-mutating recording pause.

        This does not touch the environment. It exists so a caller can record
        a boundary around external work without fabricating a Qud transition.
        """
        self.record_mark("recording_pause", {"role": "system", "content": "Recording paused"})
        try:
            yield
        finally:
            self.record_mark("recording_resume", {"role": "system", "content": "Recording resumed"})

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def export_atif(
    source: str | Path,
    output: str | Path | None = None,
) -> dict[str, Any]:
    """Convert ATOF JSONL to ATIF using NVIDIA NeMo's native converter."""
    try:
        from nat.atof.scripts.atof_to_atif_converter import convert_file
    except ImportError as exc:
        raise OptionalDependencyError(
            "ATIF export requires NVIDIA NeMo Agent Toolkit; install nvidia-nat-atif[full]"
        ) from exc
    trajectory = convert_file(source, output)
    return trajectory.to_json_dict()


def genai_span_attributes(event: AtofScopeEvent, *, capture_content: bool = False) -> dict[str, Any]:
    """Return the GenAI semantic attributes for an ATOF scope start."""
    attributes: dict[str, Any] = {
        "gen_ai.operation.name": {
            "agent": "invoke_agent",
            "function": "invoke_workflow",
            "tool": "execute_tool",
            "llm": "chat",
        }.get(event.category, "invoke_workflow"),
        "gen_ai.provider.name": str((event.metadata or {}).get("provider_name", "qudgym")),
        "qudgym.atof.uuid": event.uuid,
        "qudgym.atof.category": event.category,
    }
    metadata = event.metadata or {}
    session_id = metadata.get("session_id")
    if isinstance(session_id, str):
        attributes["gen_ai.conversation.id"] = session_id
    if event.category == "agent":
        attributes["gen_ai.agent.name"] = event.name
        version = metadata.get("agent_version")
        if isinstance(version, str):
            attributes["gen_ai.agent.version"] = version
    profile = event.category_profile or {}
    if event.category == "llm":
        model = profile.get("model_name")
        if isinstance(model, str):
            attributes["gen_ai.request.model"] = model
    if event.category == "tool":
        attributes["gen_ai.tool.name"] = event.name
        tool_id = profile.get("tool_call_id")
        if isinstance(tool_id, str):
            attributes["qudgym.tool_call_id"] = tool_id
    if capture_content and event.category == "llm" and isinstance(event.data, dict):
        messages = event.data.get("messages")
        if messages is not None:
            attributes["gen_ai.input.messages"] = _otel_content(messages)
    return attributes


def _end_attributes(
    event: AtofScopeEvent, *, capture_content: bool = False,
) -> dict[str, Any]:
    attributes: dict[str, Any] = {}
    data = event.data if isinstance(event.data, dict) else {}
    result = data.get("result")
    if isinstance(result, dict):
        metrics = result.get("metrics")
        if isinstance(metrics, dict):
            for source, target in (
                ("outcome", "qudgym.outcome"),
                ("turns_elapsed", "qudgym.turns_elapsed"),
                ("decisions_elapsed", "qudgym.decisions_elapsed"),
            ):
                if source in metrics:
                    attributes[target] = metrics[source]
    if isinstance(data.get("outcome"), str):
        attributes["qudgym.outcome"] = data["outcome"]
    if isinstance(data.get("error"), dict):
        error_type = data["error"].get("error_type")
        if isinstance(error_type, str):
            attributes["error.type"] = error_type
    profile = event.category_profile or {}
    if isinstance(profile.get("error_type"), str):
        attributes["error.type"] = profile["error_type"]
    if event.category == "llm":
        response_id = profile.get("response_id")
        if isinstance(response_id, str):
            attributes["gen_ai.response.id"] = response_id
        finish_reasons = profile.get("finish_reasons")
        if isinstance(finish_reasons, list) and all(
            isinstance(reason, str) for reason in finish_reasons
        ):
            attributes["gen_ai.response.finish_reasons"] = finish_reasons
        usage = profile.get("usage")
        if isinstance(usage, dict):
            for source, target in (
                ("input_tokens", "gen_ai.usage.input_tokens"),
                ("output_tokens", "gen_ai.usage.output_tokens"),
            ):
                value = usage.get(source)
                if isinstance(value, int):
                    attributes[target] = value
        if capture_content:
            output = data.get("output_messages")
            if output is None:
                output = data.get("choices")
            if output is None and isinstance(data.get("content"), str):
                output = [{
                    "role": "assistant",
                    "parts": [{"type": "text", "content": data["content"]}],
                }]
            if output is not None:
                attributes["gen_ai.output.messages"] = _otel_content(output)
    return attributes


def _otel_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def export_genai_otel(
    source: str | Path,
    *,
    tracer_provider: object | None = None,
    capture_content: bool = False,
) -> int:
    """Project an ATOF stream into GenAI OTel spans and return ended spans.

    The OpenTelemetry SDK/provider remains host-owned. This function only uses
    the OTel API and never configures an exporter or sends data over a socket.
    """
    try:
        from opentelemetry import trace
        from opentelemetry.trace import SpanKind, Status, StatusCode
    except ImportError as exc:
        raise OptionalDependencyError(
            "GenAI OTel export requires opentelemetry-api and a host-configured provider"
        ) from exc

    events = read_atof(source)
    tracer = trace.get_tracer("qudgym", "0.1.0", tracer_provider=tracer_provider)
    active: dict[str, tuple[Any, Any]] = {}
    ended = 0
    for event in events:
        if isinstance(event, AtofMarkEvent):
            parent = active.get(event.parent_uuid or "")
            if parent is not None:
                parent[0].add_event(
                    "qudgym.atof.mark",
                    {"qudgym.mark.name": event.name},
                    timestamp=_timestamp_to_nanoseconds(event.timestamp),
                )
            continue
        timestamp = _timestamp_to_nanoseconds(event.timestamp)
        if event.scope_category == "start":
            parent_context = active.get(event.parent_uuid or "")[1] if event.parent_uuid in active else None
            kind = SpanKind.CLIENT if event.category in {"tool", "llm"} else SpanKind.INTERNAL
            span = tracer.start_span(
                _span_name(event),
                context=parent_context,
                kind=kind,
                attributes=genai_span_attributes(event, capture_content=capture_content),
                start_time=timestamp,
            )
            active[event.uuid] = (span, trace.set_span_in_context(span))
            continue
        active_span = active.pop(event.uuid, None)
        if active_span is None:
            continue
        span = active_span[0]
        span.set_attributes(_end_attributes(event, capture_content=capture_content))
        data = event.data if isinstance(event.data, dict) else {}
        if isinstance(data.get("error"), dict):
            span.set_status(Status(StatusCode.ERROR, "recorded operation error"))
        span.end(end_time=timestamp)
        ended += 1

    for span, _context in active.values():
        span.set_status(Status(StatusCode.ERROR, "unclosed ATOF scope"))
        span.end()
    return ended


def _span_name(event: AtofScopeEvent) -> str:
    if event.category == "agent":
        return f"invoke_agent {event.name}"
    if event.category == "tool":
        return f"execute_tool {event.name}"
    if event.category == "llm":
        model = (event.category_profile or {}).get("model_name")
        return f"chat {model}" if isinstance(model, str) and model else "chat"
    return f"invoke_workflow {event.name}"


__all__ = [
    "ATOF_VERSION",
    "AtofEvent",
    "AtofMarkEvent",
    "AtofScopeEvent",
    "LLMCall",
    "OptionalDependencyError",
    "RecordingConfig",
    "RecordingLimitError",
    "SessionRecorder",
    "export_atif",
    "export_genai_otel",
    "genai_span_attributes",
    "read_atof",
]
