# Gyms of Qud

Foundations for a **decision-boundary Caves of Qud environment**, for finite-candidate policies, tool-assisted search, and NeMo evaluation/RL workflows.

**This first slice runs a small mock environment, not Caves of Qud.** It contains no game assets, trained model, functioning live-game control, or functioning live-game snapshot implementation. It does include a startup-only read-only compatibility diagnostic mod. The repository is `cagyirey/gyms-of-qud`; the Python package and API are named `qudgym`/QudGym.

## What works now

| Component | Status |
|---|---|
| Structured observations, candidate actions, decision IDs, terminal/truncation semantics | Implemented and locally tested |
| Partially observed mock with zero-turn prompts, hidden RNG and sparse task rewards | Implemented and locally tested |
| Bounded snapshot/restore, full mock-state hash, stale-cursor rejection | Implemented and locally tested; mock only |
| Authenticated localhost RPC, request correlation, bounded deduplication, client | Implemented and locally tested, including real loopback HTTP |
| Candidate-scoring interface and JSONL trajectories | Implemented and locally tested |
| ATOF session recording, NeMo ATIF conversion, optional GenAI OTel projection | Implemented for mock/session boundary; live Qud not connected |
| Startup-only read-only Qud compatibility diagnostic mod | Source and exact-install compile checked; game-log run pending |
| NeMo Gym native GymnasiumServer adapter and `gymnasium_agent` recipe | Native server/agent/model contract smoke passed with a deterministic Responses fixture; real local-model inference pending |
| C# game-thread handoff/decision-boundary queue | Smoke-tested on .NET 10; not loaded by the game |
| Installed-game manifest collector | Implemented and locally tested on synthetic files |
| Live Qud reset/observe/step, action enumeration, save/restore | Not implemented |

## Run the foundation

Python 3.11+; no GPU or game installation is required for these tests.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pytest -q
qudgym smoke
mkdir -p local
qudgym smoke --atof local/mock-session.atof.jsonl
python examples/branch_and_replay.py
```

On Windows, activate the environment with `.venv\Scripts\Activate.ps1` instead.

The smoke episode reaches the mock exit in four game turns and five decisions. One decision opens a zero-turn prompt.

```python
from qudgym import QudEnv, MockBackend

with QudEnv(MockBackend()) as env:
    result = env.reset(seed=7)
    observation = result.observation
    result = env.step(observation.actions[0].id)
```

`QudEnv` is the native structured API, not a `gymnasium.Env` subclass. The optional **NeMo adapter** exposes the upstream Gymnasium-style lifecycle without converting the native observation into a fixed action space or discarding candidate metadata.

### HTTP bridge contract

```bash
export QUDGYM_TOKEN="$(python -c 'import secrets; print(secrets.token_hex(32))')"
qudgym serve-mock --oracle
# In a second shell with the SAME QUDGYM_TOKEN:
python examples/http_client.py
```

The service listens only on `127.0.0.1`. `--oracle` is explicit: player workers do not expose rewind/hash capabilities. The Python client never follows redirects, consults proxy settings, or automatically retries uncertain requests. This protocol is **not** wire-compatible with Raves of Qud.

### Record a rollout

The legacy research JSONL remains available:

```bash
mkdir -p runs
qudgym smoke --record runs/mock-seed7.jsonl
```

For interoperable session recording, use the bounded ATOF stream:

```bash
qudgym smoke --atof runs/mock-seed7.atof.jsonl
qudgym atof-validate runs/mock-seed7.atof.jsonl
qudgym export-atif runs/mock-seed7.atof.jsonl --output runs/mock-seed7.atif.json
```

ATIF export uses NVIDIA NeMo's converter; the optional direct GenAI OTel
projection is documented in the [recording guide](docs/RECORDING.md). Both
formats are session/environment telemetry, not fabricated token or logprob
training batches. No live Qud session is implied.

## Agent-eye replay and representative builds

The [agent-eye guide](docs/AGENT_EYE.md) describes the new versioned perception
contract, evidence memory, grounded actions, and opt-in NeMo presenter. It is
separate from controller RPC 0.1; no live Qud adapter or trained model is implied.

```bash
qudgym eye-demo --preset listener --output runs/listener.jsonl --html runs/listener.html
qudgym eye-replay runs/listener.jsonl --output runs/listener-replay.html
qudgym eye-builds builds/library.json
```

Open the HTML to inspect exactly the recorded policy view, with current perception,
remembered evidence, and hypotheses displayed separately. The demo presets are
synthetic fixtures, not official Qud builds. Add your own representative builds to
`builds/library.json` using the format in the guide; it is intentionally empty until
you supply them. Validation checks metadata, not character legality in the game.

See [agent-eye validation](docs/AGENT_EYE_VALIDATION.md) for executed tests and
remaining live-game/NeMo checks.

## NeMo first

See [the native NeMo Gym guide](integrations/nemo_gym/README.md). The only
rollout path is NVIDIA's `gym env start` -> `gym eval run --no-serve` ->
`gym eval profile` flow through upstream `GymnasiumServer` and
`gymnasium_agent`. A thin shell wrapper manages the CLI lifecycle; it does not
select actions, perform inference, count tokens, train, or duplicate metrics.

```bash
python scripts/stage_nemo_adapter.py /path/to/NeMo-Gym
/path/to/NeMo-Gym/.venv/bin/gym env validate \
  --resources-server qudgym --model-type vllm_model \
  --model contract-smoke --model-url http://127.0.0.1:8000/v1 \
  --model-api-key dummy

NEMO_GYM_ROOT=/path/to/NeMo-Gym \
NEMO_GYM_MODEL=qudgym-policy \
NEMO_GYM_MODEL_URL=http://127.0.0.1:8000/v1 \
NEMO_GYM_MODEL_API_KEY=dummy \
NEMO_GYM_REPEATS=5 \
scripts/run_nemo_gym_mock.sh local/nemo-gym-policy
```

The first model target is an externally served small quantized vLLM checkpoint;
quantization belongs to the vLLM launch/checkpoint, while NeMo Gym's
`vllm_model` owns the Responses-to-Chat-Completions boundary. The wrapper
verifies the staged adapter manifest, constrains the resource server to one
process worker, disables W&B/MLflow exporters, and enforces the bounded mock
success/profile contract by default. A deterministic native contract smoke has
passed; no real local-model, Platform upload, or training run is claimed yet.

The generic finite-candidate scorer remains separate: a pointer-head decision
model is not assumed to be supported by every NIM or generative RL recipe.

## What is needed from a Qud installation

```bash
python scripts/collect_install_info.py '/path/to/Caves of Qud' \
  --game-version 'EXACT VERSION FROM THE GAME' \
  --output local/qud-install.json
```

Optionally add `--mods-dir '/path/to/Mods'`. Review the resulting JSON before sharing. It contains OS/architecture, selected assembly names/sizes/SHA-256 hashes, and optional mod metadata—**not** assembly bytes, saves, absolute install paths or credentials. Validate the redacted manifest with `qudgym compat-validate local/qud-install.json --kind manifest`. See the [compatibility evidence workflow](docs/COMPATIBILITY.md) and [local integration](docs/LOCAL_INTEGRATION.md).

## Contribute through a pull request

Clone `cagyirey/gyms-of-qud`, work on a feature branch, and open an unmerged PR against `main`. The foundation branch is `feat/qudgym-foundation`. The optional publishing helper targets this existing personal repository; it does not create a repo, change visibility, push to main, force-push, or merge:

```bash
bash scripts/publish-github.sh
```

See [publication notes](docs/PUBLISH.md). No local game data belongs in commits.

## Design and next implementation work

- [Protocol and invariants](docs/PROTOCOL.md)
- [Compatibility evidence workflow](docs/COMPATIBILITY.md)
- [Session recording and observability](docs/RECORDING.md)
- [Live Qud integration checklist](docs/LOCAL_INTEGRATION.md)
- [Implementation backlog](docs/ROADMAP.md)
- [Primary-source inspection record](docs/SOURCES.md)
- [Validation record](docs/VALIDATION.md)

This repository is independent of Caves of Qud/Freehold Games, Raves of Qud and NVIDIA. No license to redistribute the game is implied. A code-distribution license for this new repository has intentionally not been chosen on the owner's behalf.
