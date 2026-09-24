"""Exact mock replay despite fresh decision IDs. No ML/GPU dependency."""
from qudgym import QudEnv, MockBackend

with QudEnv(MockBackend(allow_oracle=True)) as env:
    start = env.reset(seed=42)
    root = env.backend.snapshot()
    initial = env.backend.state_hash()
    first = env.step("move:E")
    first_hash = env.backend.state_hash()
    restored = env.restore(root.handle)
    assert env.backend.state_hash() == initial
    assert restored.observation.decision_id != start.observation.decision_id
    replay = env.step("move:E")
    assert env.backend.state_hash() == first_hash
    assert replay.observation.player == first.observation.player
    env.backend.release(root.handle)
    print("Replay verified: identical simulator state, different action cursor.")
