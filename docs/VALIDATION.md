# Validation record

## Native NeMo Gym contract smoke — 2026-09-24–25

- Installed and ran `NVIDIA-NeMo/Gym@1c8261080bdc881b3e9b7f870e6418f160516991` (`0.7.0rc0`, Python 3.13.14) from a clean temporary checkout.
- Core Python suite: **171 passed, 1 skipped** in the default environment; the full pinned NeMo environment passed **172 passed, 1 skipped** after making the adapter tests compatible with the real imported Gym classes. The native CI job additionally stages the adapter and runs the full documented lifecycle against a test-only model fixture.
- The first startup exposed a real staging defect: the generated resource-server requirements installed QudGym but omitted the local editable NeMo Gym package, so the server venv failed on `ModuleNotFoundError: fastapi`. The staging helper now installs both local projects; this was not hidden or reclassified as a policy failure.
- After that correction, NeMo Gym's actual resource server, upstream `gymnasium_agent`, and model server all started and passed the shipped readiness check.
- Two rollouts were collected through `gym eval run --no-serve` using a deterministic test-only model. The native `openai_model` path and the intended external-server `vllm_model` Responses-to-Chat-Completions path each completed the same two repeats. Both returned reward `+1`, `outcome=success`, `is_mock=true`, four game turns, and five decisions.
- NeMo Gym produced native `rollouts.jsonl`, materialized inputs, aggregate metrics, model-call capture, `ng_trajectory` attachments, and a complete reward-profile join for both repeats. The committed lifecycle wrapper completed the same two-repeat flow, explicitly disabled W&B/MLflow exporter setup and rollout export, enforced the repeat/success/profile contract, and wrote its compact summary. The environment-backed key mode was also exercised with a test-only key.
- Native NeMo Gym/Lens OpenTelemetry was not enabled in this smoke; the existing standalone GenAI OTel projection tests remain the only executed OTel path. No OTLP network export was attempted.
- The closed-session replay cache is now globally bounded (256 sessions / 300 seconds by default), with unit coverage for eviction and expiry; terminal replay still succeeds inside that bound.
- The wrapper uses a dedicated process group for Gym cleanup, rejects unexpected untracked NeMo paths, verifies the committed adapter source manifest, nulls W&B/MLflow exporter availability fields, and passes `upload_rollouts=false` to both Gym commands. This prevents ambient exporter configuration or raw mock content from being exported by the local workflow.
- Staging and execution enforce the pinned NeMo Gym commit and reject tracked or untracked local NeMo modifications unless the explicit drift override is set; the resource server is constrained to one process worker because its session/replay state is process-local.
- Native rollout health reported both rows as `unobserved`, not healthy, because `gymnasium_agent` does not provide the producer turn/tool evidence required by several checks. This limitation remains explicit.
- `gym eval export` rejected both rows because the current `gymnasium_agent` native trajectory contains coverage gaps. This confirms the documented strict boundary; no ATIF was fabricated and export remains non-gating.
- This was a protocol/rollout contract smoke, not model-quality evidence. No small quantized model, Caves of Qud process, NeMo Platform mutation, Studio upload, live Qud API, save operation, or training job was executed.
- NeMo Platform preflight found a listener on the canonical `:8080` with a non-200 readiness response, while a separate NeMo process was healthy on `:8090` and local configuration still pointed at `:8080`. No second platform was started, no configuration was rewritten, and no Studio/Intake mutation was attempted.

## Diagnostic and recording validation — 2026-09-24

- Full Python suite: **154 passed** (144 inherited tests + 10 recording/diagnostic tests).
- `qudgym smoke --atof` and `qudgym atof-validate` passed on a mock session; the stream contains paired ATOF scopes and one reset mark.
- NVIDIA NeMo `nvidia-nat-atif[full]` 1.8 ATOF→ATIF conversion passed in a temporary Python 3.13 environment; output was ATIF-v1.7 with deterministic agent/tool steps.
- GenAI OTel projection passed with a temporary OpenTelemetry API/SDK in-memory provider; one mock action produced root agent, workflow, and tool spans.
- The startup-only diagnostic C# project compiled against the installed `Managed` candidate whose `Assembly-CSharp.dll` SHA-256 matched the collected manifest and whose XRL marker was present. The local compiler emitted two known assembly-version conflict warnings; no build output or game assembly is tracked.
- `python examples/branch_and_replay.py`, the real loopback `examples/http_client.py` smoke, `qudgym smoke`, compileall, and the .NET 10 `BridgeCore` smoke test passed.
- At the time of this diagnostic/recording checkpoint, no game process, diagnostic log run, live API, save mutation, native NeMo rollout, or model inference was executed. The later native NeMo contract smoke is recorded above; the local C# compile still is not evidence that Qud loaded the mod.

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
- No live Qud or F#/Suave transport was executed. The native NeMo runtime result was added later and is recorded above; no real model rollout was executed at this checkpoint.

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
- The earlier checkpoint had no NeMo package/runtime available. The adapter has since passed a deterministic native Gym contract smoke, but actual small-model behavior, model-driven failure cases, and Platform integration remain unvalidated.
- No .NET SDK/compiler available. C# library and smoke tests are source scaffolds; the CI job is configured but has not run here.
- Python 3.11/3.12 CI matrix entries have not run locally; only 3.13.5 was tested.
- No NIM inference endpoint, pointer-head serving integration, GPU training, NeMo RL job or trained decision model.

The mock is a protocol/control-path fixture, not evidence of performance in Qud.

## Publication recheck

The original `749b736` archive was extracted into a fresh directory. All **52 tests passed again**. `PYTHONPATH=src python -m qudgym.cli smoke` reported success at 4 turns / 5 decisions, `PYTHONPATH=src python examples/branch_and_replay.py` verified replay, and compileall succeeded. Repository naming and publishing documentation/helper were updated for the existing `cagyirey/gyms-of-qud`; core implementation behavior is unchanged. The helper receives a syntax check only, not an authenticated CLI test. Consult the PR's actual GitHub Actions results for remote CI; local evidence is not a substitute for that result.
