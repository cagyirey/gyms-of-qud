"""CLI entrypoints kept separate from the native controller."""
import json
from pathlib import Path
from .builds import BuildLibrary, load_library
from .contracts import AgentView, Frame, Hypothesis
from .fixtures import ArenaFixture, CapabilityScorer, PRESETS
from .memory import EvidenceMemory
from .replay import Record, read_trace, render_html, write_trace


def add_commands(commands):
    demo = commands.add_parser("eye-demo", help="Record a synthetic agent-eye capability fixture")
    demo.add_argument("--preset", choices=PRESETS, default="bow")
    demo.add_argument("--output", type=Path, required=True)
    demo.add_argument("--html", type=Path)
    replay = commands.add_parser("eye-replay", help="Render an agent-eye trace to offline HTML")
    replay.add_argument("trace", type=Path)
    replay.add_argument("--output", type=Path, required=True)
    builds = commands.add_parser("eye-builds", help="Validate preset metadata, NOT game legality")
    builds.add_argument("library", type=Path)
    schema = commands.add_parser("eye-schema", help="Export the agent-eye and preset schemas")
    schema.add_argument("--output", type=Path, default=Path("schemas/eye"))


def demo_records(preset: str) -> list[Record]:
    game = ArenaFixture(preset=preset)
    memory = EvidenceMemory()
    scorer = CapabilityScorer()
    frames = []
    # Show zero-time inspection, a lost visual contact and (for listener) hearing.
    prefix = ["look", "yes", "back", "back", "forward", "forward"]
    for i in range(game.max_decisions + 1):
        frame = game.observe()
        hypotheses = ()
        if i == 4:
            hypotheses = (Hypothesis(id="demo-guess",
                text="The contact may still be near its last observed position (scripted example, not a learned prediction).",
                based_on_decisions=(frames[-1].view.current.decision_id,), model="scripted-demo-not-a-model"),)
        view = memory.update(frame, hypotheses=hypotheses)
        if frame.phase == "terminal":
            frames.append(Record(is_mock=True, view=view))
            break
        if i < len(prefix):
            action = prefix[i]
        else:
            scores = scorer.score(view)
            action = max(frame.actions, key=lambda a: scores[a.id]).id
        frames.append(Record(is_mock=True, view=view, selected_action=action))
        game.step(action, decision_id=frame.decision_id)
    return frames


def run(args):
    if args.command == "eye-demo":
        if args.html and (args.html.resolve() == args.output.resolve() or args.html.exists()):
            raise ValueError("HTML output must be a new, distinct file")
        records = demo_records(args.preset)
        write_trace(records, args.output)
        if args.html:
            render_html(records, args.html)
        print(json.dumps({"is_mock": True, "preset": args.preset, "records": len(records),
                          "final_turn": records[-1].view.current.turn,
                          "final_phase": records[-1].view.current.phase}))
    elif args.command == "eye-replay":
        render_html(read_trace(args.trace), args.output)
    elif args.command == "eye-builds":
        library = load_library(args.library)
        print(json.dumps({"presets": [p.id for p in library.presets], "game_legality_checked": False}))
    elif args.command == "eye-schema":
        args.output.mkdir(parents=True, exist_ok=True)
        for model in (AgentView, BuildLibrary):
            (args.output / f"{model.__name__}.schema.json").write_text(
                json.dumps(model.model_json_schema(), indent=2) + "\n", encoding="utf-8")
