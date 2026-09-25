# Compatibility evidence workflow

This milestone defines a **redacted evidence contract**. It does not detect a Qud
build, prove that an API exists, or implement a live adapter.

## 1. Collect the install manifest

Use the existing collector from the repository root:

```bash
python scripts/collect_install_info.py '/path/to/Caves of Qud' \
  --game-version 'EXACT BUILD FROM THE GAME' \
  --mods-dir '/path/to/Mods' \
  --output local/qud-install.json
```

The output contains names, sizes, hashes, platform metadata, and limited mod
metadata. It does not copy game binaries or saves. Keep it under ignored `local/`
until manually reviewed.

Validate the generated document without printing its full contents:

```bash
qudgym compat-validate local/qud-install.json --kind manifest
```

## 2. Record diagnostic evidence separately

Start from [`compatibility-report.example.json`](compatibility-report.example.json)
and replace the placeholders with evidence from a dedicated test profile:

- exact build string;
- enabled mod IDs and load order;
- hook names, availability, and observed thread IDs;
- short redacted notes explaining the observation method.

Validate it with:

```bash
qudgym compat-validate path/to/reviewed-report.json --kind diagnostic
```

The contract rejects extra fields, duplicate mod/load-order/hook entries, unsafe
path components, and obvious private installation paths. It cannot verify that a
reported hook is genuine; that requires the exact installed game API and a
read-only diagnostic run.

## 3. Evidence required before implementation

A compatibility report is not permission to implement live control. The next
implementation gate requires:

1. exact build and mod/load-order fingerprint;
2. a minimal read-only diagnostic mod;
3. observed event thread IDs and hook availability;
4. evidence that the diagnostic does not mutate saves or game state;
5. a reviewed, redacted report committed or shared separately from local paths.

Only after that gate should we design the external transport and visible-state
projection. Snapshot, restore, and full-state hash capabilities remain disabled.
