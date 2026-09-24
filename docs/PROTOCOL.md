# Controller protocol 0.1

## Decision boundaries, not render frames

`reset` returns the initial `Transition`; `step` returns the next resolved `Transition`. Resolution means that the submitted input has actually been consumed and the game is waiting for another command, waiting for a prompt answer, or terminal. It does not mean merely queued input, an arbitrary snapshot, one `EndTurn`, or one Unity render tick.

A command can consume zero turns, one turn, or multiple turns. `observation.turn` is simulation time; `metrics.decisions_elapsed` is agent decisions. A prompt is a first-class observation with its own candidates and fresh decision ID.

## Envelopes

```json
{"protocol_version":"0.1","request_id":"controller-request-42","operation":{"op":"step","decision_id":"episode:17","action_id":"move:E"}}
```

The reply repeats the protocol version and request ID, and has exactly one non-null `result` or `error`. JSON schemas are generated from the Python models in `schemas/`. Regenerate with `qudgym schema` after changing them. Unknown fields/versions are rejected.

Operations: `hello`, `reset`, `observe`, `step`; privileged optional operations: `snapshot`, `restore`, `release`, `state_hash`. There is no arbitrary reflection, shell, file-read or code-evaluation operation. Closing a client does not terminate Qud.

## Candidate semantics

Action IDs are opaque selections from the current observation, not executable command strings. The eventual Qud adapter must match IDs to its own authorized action registry. It must not forward an arbitrary supplied ID to a game command interpreter.

Candidates are **admissible inputs based on player-visible knowledge**, not an omniscient promise that the action will succeed. Do not remove an action because an unseen enemy, hidden obstacle, undiscovered item property, or unobserved status makes it fail. Candidate availability can itself leak state.

## Cursors, replay and retries

Every resolved step/reset/restore changes the decision cursor. Restoring an identical world MUST NOT resurrect an old cursor; it would allow old in-flight commands to become valid again (ABA).

The reference service serializes requests and keeps a bounded cache of 256 completed replies. An identical request ID/content within that window returns the stored reply. Reusing the ID with different content fails. This is not durable exactly-once execution across crashes or unbounded cache eviction. A stale step is still rejected after cache eviction.

After an uncertain transport outcome, reconnect and observe/reconcile; do not create a new request ID and repeat the mutation blindly. `reset` cannot be assumed idempotent after deduplication data is lost. For live code, interrupted application must fault the worker until reconciled. Infrastructure failures are errors, not game deaths or zero-reward outcomes.

## Observation and oracle separation

The observation schema is an allowlist: perceived map, player stats, perceived entity descriptors, messages, prompt and candidates. No true item identities/blueprints, enemy internals, PRNG state, snapshot handles, full-state hashes or credentials appear in it.

The full-state hash is a privileged API and includes transition-relevant hidden state. It must never be passed to a fair-play policy as a feature. Observation equality is not Markov-state equality; a player-observation digest is not safe for simulator transpositions.

Snapshots are server-held opaque handles, bound to the worker/episode. Reset invalidates them. There are at most 128 per reference worker; clients release unused handles. Mock replay captures the mock PRNG and decision/time-limit counters. This proves nothing about Qud's save system. Live capabilities remain false until tested.

## Scope and safety

The HTTP reference server is a development controller, not a multi-tenant production gateway. It requires a random bearer token of at least 32 printable ASCII characters, rejects browser Origin headers, bounds requests/replies to 1 MiB, and binds to loopback. No remote plaintext deployment is supported. Use an authenticated tunnel or a separately designed secure worker plane later.

One game process owns one serialized action stream. Do not confuse multiple model requests with independently cloned games. Live reset and snapshot operations must use dedicated test profiles; never overwrite a player's original save.
