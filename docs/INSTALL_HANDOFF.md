# Native exports and installed-API handoff

This slice is **metadata-only**. It neither implements a Qud mod nor establishes live
hook timing, thread safety, observation fairness, scenario reset or deterministic restore.
No new game hooks should be guessed from an export version or assembly filename.

## Keep version domains separate

The collector's `game_version` is user supplied. A native export's `gameversion`, its
`buildversion` (format version), and the executable assembly's version are different
fields. Do not rewrite exports or claim incompatibility merely because these strings
differ. The probe reports the actual assembly identity and SHA-256 so evidence can
be matched to a particular installed binary. Even equal strings are not a compatibility test.

A mod inventory is not an enabled-mod list. New native presets use `mods: null` until
the owner identifies enabled mod IDs in load order. `mods: []` instead explicitly
means no enabled mods. Existing v1 creation-code presets remain supported.

## Native build sources

`builds/library.json` now references the existing owner-supplied Artifex and Marauder
exports without changing them. Native sources use `qud-build/2` within a
`qud-build-library/2`, an explicit repository-relative `.json` path, and the SHA-256
of the original bytes. Review content changes before incrementing `revision` and
updating the hash. Version 1 creation codes are still accepted.

The metadata-only `qudgym eye-builds builds/library.json` command checks shape.
`inspect_sources(library, source_root)` and the command below also resolve sources
inside an explicit root, verify hashes and compare each preset's declaration with
its native export. `$type` and `moduleType` are inert JSON strings: no CLR types are
instantiated. Unknown modules are recorded as uninterpreted rather than silently
considered supported. Purchase amounts remain purchase amounts; they are not
converted into final attributes. The instantiated game character remains authoritative.

## Run on the machine with Qud installed

Use the .NET 10 SDK (not just the runtime) and Python 3.11+. Neither a game binary
upload nor a new mod installation is required for this step.

```bash
python -m pip install -e '.[dev]'
# Save the previously generated install manifest at ignored local/qud-install.json.
python scripts/inspect_qud_install.py '/path/to/Caves of Qud' \
  --manifest local/qud-install.json \
  --output local/qud-api.json
```

The first argument accepts an installation directory, the macOS `.app` bundle, or
its Managed directory. Ambiguous installations and symlinks escaping the selected
root fail. The output must be a new file outside the game directory. Nothing is
uploaded. Review the result before sharing it; keep local reports out of public commits.

The wrapper verifies size and SHA-256 against the supplied manifest. The C# probe
verifies the hash again against the exact bytes it inspects. A changed install is
rejected: regenerate and review its manifest after an update instead of bypassing the check.
.NET may restore its normal SDK/build prerequisites; the probe itself has no external
NuGet packages, network client, game listener, or dependency on game assemblies at build time.

## What the probe reads

`tools/QudGym.ApiProbe` uses `PEReader` and `System.Reflection.Metadata`. It reads the
managed metadata image, not an execution load context. It reports the assembly
identity/MVID/hash, reference assembly names/versions and a fixed allowlist of
candidate player-mutator, event, perception, inventory/body and input-related types.
These queries are hypotheses to inspect, not claims that the installed API has them.

Only selected **declared** method/field signatures and flags are exported. Property
accessor signatures provide property types and access flags; no getter is invoked.
There is a 64 MiB assembly limit, a 96-member limit per selected type with explicit
truncation flags, and a 1 MiB report limit. Absent types are explicitly reported;
absence from Assembly-CSharp alone does not prove absence from every game assembly.
No IL, method implementations, embedded strings, game assets, saves, absolute
installation paths or runtime field values are exported.

A successful report deliberately keeps `compatibility_status: not_established` and
`runtime_hooks_verified: false`. The next patch can use this evidence to compile a
read-only diagnostic mod, establish callback thread IDs and input/turn ordering on
the user's machine, then add sanitized observation capture. This does not skip those gates.

## Validation

New Python tests exercise strict JSON, source hashes and root bounds, legacy creation
codes, independent version domains, unknown module retention, installed-binary drift,
SDK absence and wrapper behavior. The opt-in SDK integration builds an entirely
synthetic assembly with throwing executable code, inspects selected metadata and
checks signatures, missing types, hash rejection, malformed inputs, overwrite refusal
and unchanged input bytes. It is required in `.github/workflows/install-handoff.yml`.
It is not a Qud compatibility or macOS-runtime test. The existing full CI remains in place.

## Primary API references

- Microsoft PEReader API: https://learn.microsoft.com/dotnet/api/system.reflection.portableexecutable.pereader
- Microsoft metadata signature provider: https://learn.microsoft.com/dotnet/api/system.reflection.metadata.isignaturetypeprovider-2
- Repository agreements: `AGENTS.md` and `docs/LOCAL_INTEGRATION.md`.
