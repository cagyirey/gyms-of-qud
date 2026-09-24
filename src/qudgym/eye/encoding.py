"""Lossless canonical encodings. Control IDs stay in the view for action binding.

The digest identifies player information only; NEVER use it as a simulator-state
transposition key. No seed, true identity, rollout reward or oracle handle enters
these encoders. Callers must not pass a raw trajectory envelope.
"""
import json
from .contracts import AgentView


def structured(view: AgentView) -> dict:
    # Sorting set-like fields removes order accidents, but handles remain references.
    data = view.model_dump(mode="json")
    f = data["current"]
    f["entities"].sort(key=lambda e: e["id"])
    for e in f["entities"]:
        e["facts"].sort(key=lambda p: p["attribute"])
    f["zones"].sort(key=lambda z: z["id"])
    for z in f["zones"]:
        z["cells"].sort(key=lambda c: (c["y"], c["x"]))
        for c in z["cells"]:
            c["layers"].sort(key=lambda layer: layer["id"])
    f["relations"].sort(key=lambda r: (r["subject"], r["predicate"], r["object"]))
    f["actions"].sort(key=lambda a: a["id"])
    # Events remain chronological. Sorting event IDs could invert narrative order.
    data["remembered"].sort(key=lambda r: (r["subject"], r["attribute"]))
    return data


def text(view: AgentView) -> str:
    """JSON baseline, not a lossy model-generated summary of the structured view."""
    return json.dumps(structured(view), sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False)


def from_text(value: str) -> AgentView:
    return AgentView.model_validate_json(value)
