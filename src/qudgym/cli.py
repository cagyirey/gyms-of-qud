import argparse
import json
import os
from pathlib import Path

from .env import QudEnv
from .mock import MockBackend
from .models import Capabilities, Observation, RpcRequest, RpcResponse, Transition
from .trajectory import TrajectoryRecorder


def main():
    parser = argparse.ArgumentParser(description="QudGym foundation tools (mock only)")
    commands = parser.add_subparsers(dest="command", required=True)
    smoke = commands.add_parser("smoke", help="Run a deterministic mock episode")
    smoke.add_argument("--seed", type=int, default=7)
    recording = smoke.add_mutually_exclusive_group()
    recording.add_argument("--record", type=Path, help="Write the legacy research JSONL format")
    recording.add_argument("--atof", type=Path, help="Write a sanitized ATOF 0.1 session stream")
    serve = commands.add_parser("serve-mock", help="Serve the mock backend on loopback")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--oracle", action="store_true", help="Enable privileged search APIs")
    schema = commands.add_parser("schema", help="Export JSON schemas")
    schema.add_argument("--output", type=Path, default=Path("schemas"))
    compat = commands.add_parser("compat-validate", help="Validate a redacted compatibility document")
    compat.add_argument("path", type=Path)
    compat.add_argument("--kind", choices=("auto", "manifest", "event", "diagnostic"), default="auto")
    atof = commands.add_parser("atof-validate", help="Validate an ATOF JSONL session stream")
    atof.add_argument("path", type=Path)
    atof_command = commands.add_parser("export-atif", help="Convert ATOF JSONL to ATIF with NeMo")
    atof_command.add_argument("path", type=Path)
    atof_command.add_argument("--output", type=Path)
    from .eye.cli import add_commands
    add_commands(commands)
    args = parser.parse_args()
    if args.command == "compat-validate":
        from pydantic import ValidationError

        from .compat import document_summary, parse_document
        try:
            document = parse_document(args.path.read_bytes(), kind=args.kind)
        except (OSError, ValueError, ValidationError) as exc:
            parser.error(str(exc))
        print(json.dumps(document_summary(document), sort_keys=True))
    elif args.command == "atof-validate":
        from .recording import read_atof
        try:
            events = read_atof(args.path)
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
        print(json.dumps({
            "kind": "atof-stream",
            "atof_version": "0.1",
            "event_count": len(events),
            "scope_count": sum(getattr(event, "kind", None) == "scope" for event in events),
            "mark_count": sum(getattr(event, "kind", None) == "mark" for event in events),
        }, sort_keys=True))
    elif args.command == "export-atif":
        from .recording import OptionalDependencyError, export_atif
        try:
            trajectory = export_atif(args.path, args.output)
        except (OSError, ValueError, OptionalDependencyError) as exc:
            parser.error(str(exc))
        print(json.dumps({
            "kind": "atif-trajectory",
            "schema_version": trajectory.get("schema_version"),
            "step_count": len(trajectory.get("steps") or []),
        }, sort_keys=True))
    elif args.command.startswith("eye-"):
        from .eye.cli import run
        try:
            run(args)
        except (ValueError, OSError) as exc:
            parser.error(str(exc))
    elif args.command == "smoke":
        with QudEnv(MockBackend()) as env:
            if args.atof:
                from .recording import SessionRecorder
                recorder = SessionRecorder(env, args.atof)
            else:
                recorder = TrajectoryRecorder(env, args.record) if args.record else None
            driver = recorder or env
            try:
                result = driver.reset(seed=args.seed)
                for action in ("move:E", "move:E", "answer:open", "move:E", "move:E"):
                    result = driver.step(action)
                print(json.dumps({"backend": env.backend.capabilities().backend,
                                  "outcome": result.metrics.outcome,
                                  "turns": result.metrics.turns_elapsed,
                                  "decisions": result.metrics.decisions_elapsed}))
                if result.metrics.outcome != "success":
                    raise SystemExit(1)
            finally:
                if recorder:
                    recorder.close()
    elif args.command == "serve-mock":
        if not 1 <= args.port <= 65535:
            parser.error("port must be in [1, 65535]")
        token = os.environ.get("QUDGYM_TOKEN", "")
        if len(token) < 32:
            parser.error("Set QUDGYM_TOKEN to a random token of at least 32 characters")
        import uvicorn

        from .rpc import RpcService
        from .server import create_app
        backend = MockBackend(allow_oracle=args.oracle)
        try:
            uvicorn.run(create_app(RpcService(backend), token=token), host="127.0.0.1", port=args.port)
        finally:
            backend.close()
    else:
        from .compat import DiagnosticEvent, DiagnosticReport, InstallManifest
        from .recording import AtofMarkEvent, AtofScopeEvent
        args.output.mkdir(parents=True, exist_ok=True)
        for model in (RpcRequest, RpcResponse, Observation, Transition, Capabilities,
                      InstallManifest, DiagnosticEvent, DiagnosticReport,
                      AtofScopeEvent, AtofMarkEvent):
            (args.output / f"{model.__name__}.schema.json").write_text(
                json.dumps(model.model_json_schema(), indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
