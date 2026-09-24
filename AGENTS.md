# Working agreements

- Work on branches and open PRs. Do not merge or push code directly to main without instruction.
- This repository belongs under cagyirey's personal account, not geodesa-ai.
- Use the NVIDIA NeMo ecosystem where it already provides the required component. The NeMo adapter extends upstream GymnasiumServer and uses gymnasium_agent; do not write another trainer, model gateway, or token-accounting layer.
- There is NO working live Qud backend yet. Never call mock outcomes Qud evaluations.
- Read the exact installed game API before implementing Qud hooks. The game turn thread and Unity rendering thread are different; do not infer thread safety from stale comments.
- step means one resolved decision boundary, not one keypress or one EndTurn callback. Prompts can consume zero turns.
- Player observations/candidate filtering must not leak hidden entities, object identity, PRNG state, blueprint metadata, or oracle hashes.
- Snapshot/restore/full-state-hash capabilities remain disabled for live Qud until evidence-backed deterministic replay tests pass. A save file is not automatically a complete snapshot.
- Never retry an ambiguously completed mutation with a fresh request ID. Reject stale decision cursors after reset/restore.
- No copyrighted game binaries/assets, raw saves, credentials, private logs, or local absolute paths in commits or image layers.
- Run `python -m pytest -q`, both examples, and the C# smoke project when the SDK is available. Report unexecuted integration checks explicitly.
