#!/usr/bin/env python3
"""Local metadata-only Qud handoff; never loads game code or uploads a report."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from qudgym.eye.builds import inspect_sources, load_library
from qudgym.native_exports import InstallManifest, find_managed, read_json

ROOT = Path(__file__).resolve().parents[1]


def collect(game_dir: Path, manifest_path: Path, library_path: Path, source_root: Path) -> dict:
    _, data = read_json(manifest_path)
    manifest = InstallManifest.model_validate(data)
    entry = manifest.game_assembly()
    managed = find_managed(game_dir)
    assembly = managed / entry.name
    if assembly.stat().st_size != entry.bytes:
        raise ValueError("assembly size differs from the supplied manifest; recollect it")
    # The .NET reader repeats the check against the exact byte buffer it inspects.
    with assembly.open("rb") as file:
        if hashlib.file_digest(file, "sha256").hexdigest() != entry.sha256:
            raise ValueError("assembly hash differs from the supplied manifest; recollect it")
    presets = inspect_sources(load_library(library_path), source_root)
    dotnet = shutil.which("dotnet")
    if not dotnet:
        raise ValueError(".NET 10 SDK is required for metadata inspection; no Qud DLL upload is needed")
    with tempfile.TemporaryDirectory(prefix="qudgym-api-") as directory:
        report = Path(directory) / "api.json"
        command = [dotnet, "run", "--project", str(ROOT / "tools/QudGym.ApiProbe"),
                   "--configuration", "Release", "--", str(assembly), entry.sha256, str(report)]
        result = subprocess.run(command, timeout=180, check=False)
        if result.returncode:
            raise ValueError("metadata probe failed; no compatibility report was produced")
        _, api = read_json(report)
    if api.get("schema_version") != "qud-api-metadata/1" or api.get("assembly", {}).get("sha256") != entry.sha256:
        raise ValueError("unexpected metadata probe result")
    return {
        "schema_version": "qud-install-handoff/1",
        "reported_game_version": manifest.game_version,
        "reported_platform": manifest.platform,
        "mod_inventory": manifest.mods,
        "enabled_mods_in_order": None,
        "presets": presets,
        "api": api,
        "compatibility_status": "not_established",
        "notes": [
            "User-supplied release label, export gameversion and assembly version are not reconciled automatically.",
            "Mod inventory does not establish enabled mods or their load order.",
            "Metadata presence does not establish mod compilation, hook timing or thread safety.",
            "No game code, gameplay, reset, save, network listener or training was executed.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("game_dir", type=Path)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--library", type=Path, default=ROOT / "builds/library.json")
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        root = args.game_dir.resolve(strict=True)
        output = args.output.resolve()
        if output.is_relative_to(root) or output.exists():
            raise ValueError("output must be a new file outside the game directory")
        report = collect(root, args.manifest, args.library, args.source_root)
        text = json.dumps(report, indent=2, allow_nan=False) + "\n"
        if len(text.encode("utf-8")) > 1024 * 1024:
            raise ValueError("combined report exceeds 1 MiB")
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8") as file:
            file.write(text)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        parser.error(str(exc))
    print("Wrote a local metadata report. Review before sharing; live compatibility remains unverified.")


if __name__ == "__main__":
    main()
