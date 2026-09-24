# Gyms of Qud

Foundations for a **decision-boundary Caves of Qud environment**, for finite-candidate policies, tool-assisted search, and NeMo evaluation/RL workflows.

**This first slice runs a small mock environment, not Caves of Qud.** It contains no game assets, trained model, complete mod, or functioning live-game snapshot implementation. The repository is `cagyirey/gyms-of-qud`; the Python package and API are named `qudgym`/QudGym.

## What works now

| Component | Status |
|---|---|
| Structured observations, candidate actions, decision IDs, terminal/truncation semantics | Implemented and locally tested |
| Partially observed mock with zero-turn prompts, hidden RNG and sparse task rewards | Implemented and locally tested |
| Bounded snapshot/restore, full mock-state hash, stale-cursor rejection | Implemented and locally tested; mock only |
| Authenticated localhost RPC, request correlation, bounded deduplication, client | Implemented and locally tested, including real loopback HTTP |
| Candidate-scoring interface and JSONL trajectories | Implemented and locally tested |
| NeMo Gym native GymnasiumServer adapter and gymnasium_agent recipe | Source-reviewed scaffold; NeMo runtime smoke test outstanding |
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

```bash
mkdir -p runs
qudgym smoke --record runs/mock-seed7.jsonl
```

Records retain candidate IDs, before/after observations, rewards, terminal causes, task/objective versions and control metadata. They are research trajectories, **not** on-policy training batches containing fabricated token IDs or log probabilities.

## NeMo first

See [the NeMo integration guide](integrations/nemo_gym/README.md). It extends upstream `GymnasiumServer` and uses `gymnasium_agent`, leaving rollout/token accounting and training to NeMo. The generic finite-candidate scorer remains separate: a pointer-head decision model is not assumed to be supported by every NIM or generative RL recipe.

## What is needed from a Qud installation

```bash
python scripts/collect_install_info.py '/path/to/Caves of Qud' \
  --game-version 'EXACT VERSION FROM THE GAME' \
  --output local/qud-install.json
```

Optionally add `--mods-dir '/path/to/Mods'`. Review the resulting JSON before sharing. It contains OS/architecture, selected assembly names/sizes/SHA-256 hashes, and optional mod metadata—**not** assembly bytes, saves, absolute install paths or credentials. State which mods are actually enabled and their load order separately. See [local integration](docs/LOCAL_INTEGRATION.md).

## Contribute through a pull request

Clone `cagyirey/gyms-of-qud`, work on a feature branch, and open an unmerged PR against `main`. The foundation branch is `feat/qudgym-foundation`. The optional publishing helper targets this existing personal repository; it does not create a repo, change visibility, push to main, force-push, or merge:

```bash
bash scripts/publish-github.sh
```

See [publication notes](docs/PUBLISH.md). No local game data belongs in commits.

## Design and next implementation work

- [Protocol and invariants](docs/PROTOCOL.md)
- [Live Qud integration checklist](docs/LOCAL_INTEGRATION.md)
- [Implementation backlog](docs/ROADMAP.md)
- [Primary-source inspection record](docs/SOURCES.md)
- [Validation record](docs/VALIDATION.md)

This repository is independent of Caves of Qud/Freehold Games, Raves of Qud and NVIDIA. No license to redistribute the game is implied. A code-distribution license for this new repository has intentionally not been chosen on the owner's behalf.
