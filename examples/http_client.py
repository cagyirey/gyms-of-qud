"""Start `qudgym serve-mock` with the same QUDGYM_TOKEN first."""
import os
from qudgym import QudEnv
from qudgym.client import HttpBackend

with QudEnv(HttpBackend("http://127.0.0.1:8765/rpc", token=os.environ["QUDGYM_TOKEN"])) as env:
    print(env.backend.capabilities().model_dump())
    result = env.reset(seed=7)
    for action in ("move:E", "move:E", "answer:open", "move:E", "move:E"):
        result = env.step(action)
    print(result.metrics.model_dump())
