# First live-install handoff

No live Qud integration is accepted in this repository yet. No live API compatibility, full-state capture or turn throughput has been established.

## Why the previous transport is quarantined

The former F#/Suave path was removed rather than treated as a working backend. It duplicated the controller protocol with a hand-written JSON parser, used an unbounded request cache, blocked the game turn thread on unbounded tasks, had no disconnect/cancellation path, and was not covered by the CI build. Those choices make a failed or disconnected client capable of wedging the game and make protocol behavior difficult to audit.

A replacement must keep transport outside the game-turn state machine, use the versioned protocol models and bounded deduplication semantics, make cancellation/fault/reconciliation explicit, and pass tests against the exact installed game API before it is allowed to expose live capabilities. The engine-neutral `BoundaryQueue` remains the only handoff scaffold; it is not a socket server.

## Information to provide

Run `scripts/collect_install_info.py` as shown in the README, provide the exact in-game version/build, and identify the enabled mods/load order. The generated JSON is useful for compatibility targeting; it does not contain API signatures or detect the build automatically. Keep it in ignored `local/` until reviewed.

No DLL uploads or saves are necessary at this stage. For the next patch, a locally generated compile-error excerpt or a narrowly scoped method-signature report may be needed. Do not upload whole game installations, decompiled source dumps, private logs, or personal save games.

## What the C# scaffold does

`bridge/QudGym.BridgeCore` is an engine-neutral queue and decision-boundary handoff. Network callers enqueue typed action selections. The owning game-turn thread explicitly initializes the queue, begins actions and completes a task with an immutable serialized observation at a resolved boundary. It checks thread ownership, stale cursors, pre-dispatch cancellation, queue capacity, and fault/reconciliation behavior. Post-dispatch cancellation does not pretend to undo an applied command.

The `netstandard2.1` target is a provisional standalone library target, NOT a claim about the installed Qud compiler/runtime. Confirm the local mod target before choosing whether to compile this as a referenced assembly or adapt its source to the mod compiler.

It is not yet a loadable Qud mod. It has no Qud manifest, player mutator attachment, HTTP listener, input injection, visible-state extractor or save adapter. No guessed Qud method names are embedded in executable code. The test project can be compiled independently with .NET 10 and requires no game DLLs:

```bash
dotnet run --project bridge/QudGym.BridgeCore.SmokeTests -c Release
```

## Integration order

First add a read-only diagnostic mod, checked against the exact local build. Record event thread IDs and availability of turn/input hooks without changing game state. Inspect APIs locally for observation, current prompts, pending commands and save lifecycle.

Next implement a visible-state projection and a *small* supported action registry: cardinal movement, wait, a simple menu choice. Bind network work to a proven game-input boundary. Qud can block inside input/prompt handling, so merely waiting for an event that cannot fire until input arrives is a deadlock. Resolve off-turn actions and prompts explicitly; an event subscription is not sufficient.

Only then add controlled scenario reset from a dedicated test character/profile. Never run reset/save experiments against the user's only copy of a real run. Do not assume the OS supports cheap process fork, or that Unity state is fork-safe.

Snapshot research comes after correct stepping. Enumerate all PRNGs, scheduler queues, world/zone caches, pending effects, save-incomplete state and external configuration. Include game build and enabled-mod fingerprints in snapshot metadata. Enable deterministic restore only after repeated stochastic branch→restore→replay checks agree; keep capabilities disabled otherwise.
