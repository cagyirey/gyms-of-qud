# Validation record

## Diagnostic and recording validation — 2026-09-24

- Full Python suite: **154 passed** (144 inherited tests + 10 recording/diagnostic tests).
- `qudgym smoke --atof` and `qudgym atof-validate` passed on a mock session; the stream contains paired ATOF scopes and one reset mark.
- NVIDIA NeMo `nvidia-nat-atif[full]` 1.8 ATOF→ATIF conversion passed in a temporary Python 3.13 environment; output was ATIF-v1.7 with deterministic agent/tool steps.
- GenAI OTel projection passed with a temporary OpenTelemetry API/SDK in-memory provider; one mock action produced root agent, workflow, and tool spans.
- The startup-only diagnostic C# project compiled against the installed `Managed` candidate whose `Assembly-CSharp.dll` SHA-256 matched the collected manifest and whose XRL marker was present. The local compiler emitted two known assembly-version conflict warnings; no build output or game assembly is tracked.
- `python examples/branch_and_replay.py`, the real loopback `examples/http_client.py` smoke, `qudgym smoke`, compileall, and the .NET 10 `BridgeCore` smoke test passed.
- No game process, diagnostic log run, live API, save mutation, native NeMo rollout, or model inference was executed. The local compile is not evidence that Qud loaded the mod.

## Compatibility contract validation — 2026-09-24

- Full Python suite: **144 passed** (139 inherited tests + 5 compatibility tests).
- Typed install-manifest and diagnostic-report validation passed against synthetic fixtures.
- `qudgym compat-validate` emitted summaries without echoing the source path or document body.
- Compatibility schemas export through `qudgym schema`.
- At the time of the contract-only validation, no game installation, diagnostic mod, live API, save mutation, or NeMo rollout was executed; the later diagnostic/recording checks are recorded above.

## Follow-up validation — 2026-09-24

- `python -m pytest -q`: **64 passed** on the cleanup branch.
- `python -m compileall -q src scripts integrations`: passed.
- `qudgym smoke`: passed (mock success, 4 turns / 5 decisions).
- `python examples/branch_and_replay.py`: passed; identical mock state hash and fresh cursor.
- .NET 10 `QudGym.BridgeCore` smoke test: passed.
- No live Qud, F#/Suave transport, NeMo runtime, or model rollout was executed.

## Executed locally — 2026-09-23

Runtime: Python 3.13.5. Direct library versions present: pydantic 2.13.4, pytest 9.0.2, FastAPI 0.128.2, httpx 0.28.1, uvicorn 0.48.0. This is an environment record, not a complete dependency lockfile.

- `python -m pytest -q`: **52 passed**.
- Real localhost HTTP server/client test: authenticated reset, snapshot, step, restore, hash comparison, release and full mock episode.
- Duplicate submissions, concurrent duplicate submissions, request ID conflict, cache eviction and stale decision rejection.
- PRNG replay, independent reset seeds, snapshot aliasing/capacity/release/invalidation, zero-turn prompts, game death and decision-budget truncation.
- Oracle default denial, observation field allowlisting, invalid protocol/version rejection, body size limit, browser-origin rejection, token requirement.
- Ambiguous transport failure is not retried; mismatched request IDs and invalid result schemas are not returned as successful transitions.
- Session isolation, capacity limits, expiry, authoritative outcome verification and close.
- Install manifest collection against synthetic files; no original save or absolute root included.
- NeMo adapter staging against a synthetic checkout layout; repeat staging refuses overwrite. This is NOT a NeMo rollout test.
- Generated JSON schemas agree with committed models.
- `python -m pip install --no-index --no-deps --no-build-isolation -e .`: succeeded using available local dependencies.
- `qudgym smoke`: mock success, **4 turns / 5 decisions**.
- `python examples/branch_and_replay.py`: identical full mock-state hash after replay, fresh decision IDs.
- `python -m compileall -q src scripts integrations`: succeeded.
- `bash -n scripts/publish-github.sh`: succeeded. This checks syntax, not GitHub publishing.

## Not executed / not established

- GitHub publication was not part of the original archive validation. The earlier claim that connected actions were read-only was incorrect; publication is being performed through the GitHub write connector into the owner-created `cagyirey/gyms-of-qud`. No authenticated CLI is present locally.
- No live Caves of Qud installation or game process. Game hooks, action completeness, scenario reset, save semantics and performance remain unvalidated.
- No NeMo package/runtime available. Native adapter imports/configuration/actual model-driven rollouts and failure masking require validation in the pinned checkout.
- No .NET SDK/compiler available. C# library and smoke tests are source scaffolds; the CI job is configured but has not run here.
- Python 3.11/3.12 CI matrix entries have not run locally; only 3.13.5 was tested.
- No NIM inference endpoint, pointer-head serving integration, GPU training, NeMo RL job or trained decision model.

The mock is a protocol/control-path fixture, not evidence of performance in Qud.

## Publication recheck

The original `749b736` archive was extracted into a fresh directory. All **52 tests passed again**. `PYTHONPATH=src python -m qudgym.cli smoke` reported success at 4 turns / 5 decisions, `PYTHONPATH=src python examples/branch_and_replay.py` verified replay, and compileall succeeded. Repository naming and publishing documentation/helper were updated for the existing `cagyirey/gyms-of-qud`; core implementation behavior is unchanged. The helper receives a syntax check only, not an authenticated CLI test. Consult the PR's actual GitHub Actions results for remote CI; local evidence is not a substitute for that result.
