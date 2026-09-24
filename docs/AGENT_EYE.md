# Agent-eye milestone

This slice implements **perception, deterministic evidence memory, grounded action
references, a replay viewer, and synthetic capability tests**. It does not train a
policy, learn a world model, or connect to a live Qud process.

## Run it

Install the project as described in the root README, then:

```bash
qudgym eye-demo --preset listener --output runs/listener.jsonl --html runs/listener.html
qudgym eye-demo --preset bow --output runs/bow.jsonl --html runs/bow.html
qudgym eye-replay runs/bow.jsonl --output runs/bow-replay.html
qudgym eye-schema --output /tmp/agent-eye-schemas
qudgym eye-builds builds/library.json
```

Open the generated HTML in a browser. It is self-contained: no CDN, model server,
remote assets, telemetry, or network fetches. Output files are create-only. The
viewer accepts **agent-eye-trace/1**, not the older research-trajectory JSONL, which
contains control metadata such as seeds. Never pass raw trajectory envelopes as
model observations.

The demo presets `blade`, `bow`, and `listener` are deliberately synthetic. Their
ranges, perception rules, costs and cooldowns are NOT claims about Caves of Qud.
The demo first inspects a trinket, moves away and returns, then uses a small rule
baseline to disable a practice target. One explicitly labeled **scripted
hypothesis** illustrates non-authoritative model output; no model generated it.

## Contract boundaries

`src/qudgym/eye/contracts.py` is the source of truth. `eye-schema` exports JSON
Schemas as build artifacts. The new `agent-eye/1` frame and `agent-view/1` policy
view coexist with controller RPC **0.1**; this is not a silent wire upgrade.

```
permitted player information -> Frame -> EvidenceMemory -> AgentView
                                                     -> policy / replay / text
```

The actual game adapter must collect only information the character is entitled
to access. Schema validation alone cannot establish that a field came from a
legitimate perception path. There is no generic engine-object serializer here.
The existing RPC observation remains minimal; it has NOT acquired real Qud
anatomy, inventory or sensory hooks by virtue of this change.

### Current perception

A Frame contains a controlled actor (optional during character creation), known
zone extents with sparse disclosed cells, entities, relations, events, prompts and
candidate actions. An omitted cell/property is unknown, not empty or false.
Each cell can have several named layers. Each entity has typed scalar facts;
multiple abilities/effects are separate entities, not repeated property keys.

Facts carry a source channel and evidence turn. `observed` means available in the
current decision; `reported` retains the claim's supplied timestamp; `unknown`
requires a null value and unknown channel. A known zero and an unknown quantity
are distinct. Position has its own evidence: hearing can disclose location
without disclosing identity. Sensory channel names are vocabulary, not implemented
or verified Qud sensory rules.

Relations bind carried items, anatomy, equipment, abilities, effects and known
connections. All references must resolve within the supplied frame. Event text is
retained; the model does not have to rely on an exhaustive hand-authored event enum.
The engine's hidden GUID or blueprint must not be used as a policy-facing handle.
The `cell:` handle prefix is reserved for internal terrain-memory keys.

### Remembered evidence and hypotheses

EvidenceMemory accepts only Frames. It retains last-disclosed properties, spatial
layers, relations and raw events, with timestamps and decision provenance. Missing
current data cannot move a remembered entity or refresh its evidence. A currently
unknown identity does not erase an earlier disclosed identity: the earlier value
stays in the **remembered** section, not in the current contact record.

Retention is bounded (default 4096 fact records, 256 events and 256 decision IDs),
and evictions are counted. Eviction or omission is NOT a claim that an object was
destroyed. Explicit negative observations can be represented as observed facts or
messages; no absence-based deletion heuristic is implemented.

Hypotheses are separate objects with supporting decision references and a model
label. They cannot overwrite facts; scores, when supplied, are not asserted to be
calibrated probabilities. No inference engine or learned association model is
included. The synthetic arena assigns a fresh contact handle after losing and
reacquiring a contact, instead of using engine identity to solve reidentification.
Its continuity between sight and hearing is a fixture assumption, not a Qud rule.

Repeated identical decisions are idempotent. Changed payloads on retained cursors
are rejected. Reset is explicit between episodes. Backward game time is rejected:
a TAS branch must restore/rebuild the agent's observation history as well as the
simulator. This milestone does not implement policy-memory snapshot/fork support.
The retained cursor cache is bounded, not an unbounded exactly-once history.

### Grounded actions and encoding

Actions carry an operation and optional source/target entity references,
destination, mode, quantity and **known** turn cost. These are admissible attempts,
not promises of success or arbitrary engine command strings. Candidate generation
must not consult hidden state when deciding what to disclose.

`encoding.structured` canonicalizes set-like ordering; `encoding.text` is its
lossless JSON baseline. Numerical values, explicit unknowns, sources and binding
references survive the round trip. Opaque IDs are still present for control; a
learned policy must be trained/tested against incidental handle dependence. The
diagnostic CapabilityScorer is tested under handle renaming, display-label changes
and entity/candidate permutation. Ties remain policy decisions; we do not claim
all future scorers are invariant.

The diagnostic scorer uses current observed range/readiness and relative
positions. It is neither a neural model nor a credible Qud combat strategy. The
synthetic arena is a test harness, not a replacement for the production backend.

## Your representative-build library

`builds/library.json` starts empty on purpose. Add your presets in this shape:

```json
{
  "schema_version": "qud-build-library/1",
  "presets": [{
    "schema_version": "qud-build/1",
    "id": "my-build-name",
    "revision": "1",
    "game_build": "EXACT INSTALLED GAME BUILD",
    "mods": [],
    "creation_code": "EXACT PLAYER-SUPPLIED CREATION CODE",
    "notes": "Optional human notes; not automatically supplied to a policy"
  }]
}
```

`mods` is the enabled mod list in load order. Metadata validation checks shape,
unique IDs and nonempty creation codes; it does **not** test game legality or load
the character. Creation/import must be implemented against the installed game.
Build IDs/notes are bookkeeping, not features substituted for actual capabilities.
Do not commit game assemblies, saves, account paths, or installation reports.

## NeMo integration

The native NeMo Gym adapter remains the existing `GymnasiumServer`/`gymnasium_agent`
path. No new trainer, tokenizer, rollout service or model gateway is introduced.
`SeedSpec.representation` defaults to `native`; opt in with `agent-eye-v1`.

An example dataset and alternative config are included at:

- `integrations/nemo_gym/qudgym/data/agent_eye_example.jsonl`
- `integrations/nemo_gym/qudgym/configs/agent_eye.yaml`

Use that config INSTEAD OF the native example config, not alongside it: the server
names intentionally match. Stage using the existing staging script into a fresh
pinned NeMo checkout. The recipe still references your existing `policy_model`.

The policy receives `current.actions` and `current.decision_id`, but returns the
same `{"action_id":"...","decision_id":"..."}` selection. A presenter and its
memory belong to each session and are released with that session. Invalid-action
retries do not append duplicate memory. The legacy converter uses ONLY the already
sanitized corridor observation, preserves known movement offsets, and refuses
unmapped action metadata rather than silently discarding it. It cannot manufacture
anatomy or sensory details absent from the old protocol. The richer arena is NOT
wired into the production session manager in this PR.

Presenter behavior is unit-tested; native NeMo imports, rollouts, cleanup behavior
and model integration still need a real NeMo runtime test. Context length and
inference throughput are not benchmarked. The full canonical frame remains the
source of truth for any later sparse/tensor/token-budget representation.

## Next implementation gates

1. Read the exact installed Qud API and bind real perception at resolved game
   decision boundaries; verify each supplied property against the player UI.
2. Load the supplied preset library in a dedicated test profile and record real
   agent-eye traces. Test sensory disclosure and reidentification using those builds.
3. Wire the rich backend into worker/session lifecycle, then execute native NeMo
   rollouts with the opt-in representation.
4. Compare learned candidate policies with matched information; add recurrent
   encoders, observation ablations and transition-prediction objectives. Do not call
   the current deterministic memory/scorer a learned policy or world model.

Design reading behind this milestone: NLE (arXiv:2006.13760), DRRN
(arXiv:1511.04636), GATA (arXiv:2002.09127), Language Dynamics Distillation
(arXiv:2210.00066), PaW (arXiv:2606.02388), and Hierarchical Behaviour Spaces
(arXiv:2604.24558). These are research references, not evidence of Qud performance.
