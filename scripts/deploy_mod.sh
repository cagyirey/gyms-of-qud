#!/usr/bin/env bash
# Copy the mod into the game's Mods directory, replacing what is there.
#
# This step existed only as a habit, and that is why every Harmony patch written
# this session was never loaded. The game compiles the mod from source in its own
# Mods directory, so a patch has to be *copied* to take effect; the repo copy and
# the running copy are separate files. Editing Bridge.cs in the repo changed
# nothing in the game, and the symptom was a clean build, a passing binding check,
# and a mod that was doing none of it.
#
# Replaces the whole directory rather than copying files into it, because a stale
# obj/ or a leftover assembly from a previous layout is what makes the game's own
# compile fail with duplicate attribute errors. What the game sees is exactly what
# the repo holds, and nothing else.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${QUDGYM_MOD_DIR:-$HOME/Library/Application Support/com.FreeholdGames.CavesOfQud/Mods/QudGym}"

if [[ ! -d "$ROOT/mod/QudGym" ]]; then
  echo "no mod/QudGym to deploy" >&2
  exit 2
fi

# Build first. The binding check reads a built assembly rather than the sources,
# because resolving a patch target needs real metadata; so a deploy that skipped
# the build would leave the check with nothing to read, and "nothing to read" is
# exactly the state that let unloaded patches look verified.
QUDM="${QUDGYM_MANAGED:-$HOME/Library/Application Support/Steam/steamapps/common/Caves of Qud/CoQ.app/Contents/Resources/Data/Managed}"
if command -v dotnet >/dev/null 2>&1; then
  echo "building the mod"
  QUDGYM_MANAGED="$QUDM" dotnet build "$ROOT/mod/QudGym/QudGym.csproj" -c Release --nologo -v quiet
  # The F# assembly is the mod's runtime half and goes to lib/ directly.
  dotnet build "$ROOT/mod/QudGym.Impl/QudGym.Impl.fsproj" -c Release --nologo -v quiet
else
  echo "no dotnet: deploying sources only, and the binding check will refuse" >&2
fi
if pgrep -f "CoQ.app/Contents/MacOS/CoQ" >/dev/null 2>&1; then
  echo "the game is running; it holds the old copy until it restarts" >&2
fi

rm -rf "$DEST"
mkdir -p "$DEST"
# Sources and the manifest only. obj/ and bin/ are build products and are what the
# game's compiler chokes on when it finds a second copy of the assembly attributes.
for item in "$ROOT"/mod/QudGym/*; do
  name="$(basename "$item")"
  case "$name" in
    obj|bin) continue ;;
  esac
  cp -R "$item" "$DEST/"
done
# The C# assembly. QudGym.csproj builds to local/qud-mod-build/ rather than lib/,
# so the copy is explicit here. The game loads this file; everything else in this
# script only puts sources where the game's compiler can see them.
BUILT="$ROOT/local/qud-mod-build/Release/netstandard2.1/QudGym.dll"
if [[ -f "$BUILT" ]]; then
  cp "$BUILT" "$DEST/lib/QudGym.dll"
  cp "$ROOT/local/qud-mod-build/Release/netstandard2.1/QudGym.pdb" "$DEST/lib/QudGym.pdb" 2>/dev/null || true
else
  echo "no built C# assembly at $BUILT; the mod will load without its patches" >&2
fi
cp "$ROOT/mod/QudGym.Impl/presets.index" "$DEST/lib/presets.index" 2>/dev/null || true

echo "deployed to $DEST"
ls "$DEST" | sed 's/^/  /'
