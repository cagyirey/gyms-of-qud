#!/usr/bin/env python3
"""Resolve every Harmony patch in the mod against the installed game. No game code runs.

Qud calls ``Harmony.PatchAll`` once per mod assembly and the call is
all-or-nothing, so one unresolvable patch target silently disables every other
patch in the mod. A single mistyped parameter list once left the game sitting at
the main menu because the embark gate was never applied, with nothing but one
MODERROR line in the game's own log to show for it.

So the check lives out here, where it is cheap and total: bind every declared
target to a real method and refuse to pass while any of them does not. It reads
assemblies and reflects over them; it does not load game code, and it makes no
claim about runtime hook behaviour.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/check_patch_bindings.fsx"
DEPLOY = ROOT / "scripts/deploy_mod.sh"
# The assembly the game actually loads. The game compiles the mod from source in
# its own Mods directory, so what matters is the deployed copy -- for a long time
# the check read a locally built DLL while the game ran a stale copy of the
# sources, and every patch passed here while none of them were loaded. A check on
# an assembly nothing runs cannot fail for the reason that matters.
MOD_DLL = Path(os.environ.get(
    "QUDGYM_MOD_DIR",
    str(Path.home() / "Library/Application Support/com.FreeholdGames.CavesOfQud/Mods/QudGym"),
)) / "lib/QudGym.dll"
MANAGED_SUFFIX = Path("CoQ.app/Contents/Resources/Data/Managed")


class BindingError(RuntimeError):
    """A patch target did not bind, so the mod's PatchAll would abort at boot."""


def find_managed(game_dir: Path) -> Path:
    managed = game_dir / MANAGED_SUFFIX
    if not managed.is_dir():
        raise BindingError(f"no game assemblies under {managed}")
    return managed


def parse_failures(output: str) -> list[str]:
    """The FAIL lines the script printed, in declaration order."""
    return [line.split(None, 2)[2].strip() for line in output.splitlines() if line.startswith("FAIL ")]


def deploy() -> None:
    """Build the mod and copy it into the game's Mods directory.

    Deploying is a separate step from building because the game compiles the mod
    from source in its own directory: a build that is never copied changes nothing
    about the running game. Doing it here rather than leaving it to memory is the
    point -- the reason every Harmony patch written today went unloaded was that
    this step was a habit rather than a step, and every check still passed.
    """
    result = subprocess.run(["bash", str(DEPLOY)], capture_output=True, text=True, timeout=600)
    if result.returncode:
        raise BindingError(f"deploy failed:\n{(result.stdout + result.stderr).strip()}")


def check(managed: Path, mod_dll: Path = MOD_DLL, dotnet: str | None = None) -> list[str]:
    """Return the unbound targets, empty when every patch binds."""
    dotnet = dotnet or shutil.which("dotnet")
    if not dotnet:
        raise BindingError(".NET SDK is required to resolve patch targets offline")
    if not mod_dll.is_file():
        raise BindingError(
            f"no deployed mod assembly at {mod_dll}; run scripts/deploy_mod.sh first"
        )
    environment = {**os.environ, "QUD_MANAGED": str(managed), "QUD_MOD_DLL": str(mod_dll)}
    result = subprocess.run(
        [dotnet, "fsi", str(SCRIPT)], capture_output=True, text=True, timeout=300,
        check=False, env=environment,
    )
    output = result.stdout + result.stderr
    failures = parse_failures(output)
    if result.returncode not in (0, 1):
        raise BindingError(f"binding check did not run:\n{output.strip()}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("game_dir", type=Path, nargs="?", default=None,
                        help="Caves of Qud .app directory; defaults to the Steam install")
    parser.add_argument("--mod-dll", type=Path, default=MOD_DLL)
    parser.add_argument("--no-deploy", action="store_true",
                        help="check the deployed copy as it is, without deploying first")
    args = parser.parse_args()
    game_dir = args.game_dir or Path.home() / (
        "Library/Application Support/Steam/steamapps/common/Caves of Qud")
    try:
        if not args.no_deploy:
            deploy()
        failures = check(find_managed(game_dir), args.mod_dll)
    except BindingError as error:
        print(error)
        return 2
    if failures:
        print(f"{len(failures)} patch target(s) do not bind; PatchAll would abort at boot:")
        for failure in failures:
            print(f"  {failure}")
        return 1
    print("every patch target binds")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
