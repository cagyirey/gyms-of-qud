# QudGym compatibility diagnostic

This is a **startup-only, read-only** Qud script mod. At the mod cache-reset
boundary it records:

- the exact marketing/core version strings reported by the installed build;
- whether mod initialization completed;
- the current managed thread, the core-thread ID, and `IsCoreThread`;
- active mod IDs/load order and this mod's resolved `ModInfo`;
- a fixed allowlist of lifecycle/event type names and whether they resolve.

It does not load a game, construct or mutate a player, inspect hidden world
state, read or write a save, inject input, install a Harmony patch, open a
socket, start a server, or use `File`/persistent-data paths. The output is a
single redacted JSON payload in the game log for the user to review and copy.
A raw log or installation path must not be committed.

Type resolution is not proof that a runtime callback is registered or safe.
The next diagnostic phase may add passive, non-blocking callback observations
only after this startup report is reviewed against the exact installed build.

## Install and run

1. Copy this directory into the Qud profile-specific mods directory. Use a
   dedicated test profile; do not enable it in a personal save/profile.
2. Enable only this mod for the startup run and record the enabled mod list
   and load order separately.
3. Start the game to the main menu. Do not load or create a game for this
   phase.
4. Copy only the `QudGym compatibility evidence {...}` line from the log into
   a reviewed local file. Validate one JSON object with:

```bash
qudgym compat-validate reviewed-event.json --kind event
```

The validator checks shape, redaction, and evidence consistency; it cannot
prove that a resolved type is a usable callback or that a mod was enabled in
the game.

## Optional local compile

The game normally compiles script mods itself. For a local syntax/reference
check, provide the exact installed `Managed` directory without committing any
assemblies or build output:

```bash
dotnet build mod/QudGymCompat/QudGymCompat.csproj \
  -p:QudManaged=/path/to/the/exact/install/Managed
```

A successful local compile is not evidence that the mod loaded in Qud. The
reviewed log line and a dedicated-profile run are still required.
