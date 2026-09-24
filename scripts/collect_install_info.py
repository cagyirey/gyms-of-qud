#!/usr/bin/env python3
"""Collect a small, reviewable compatibility manifest. Never copies game binaries/saves."""
from __future__ import annotations
import argparse
import hashlib
import json
import platform
from pathlib import Path

ASSEMBLIES = ('Assembly-CSharp.dll', 'Assembly-CSharp-firstpass.dll', 'XRL.dll',
              'UnityEngine.CoreModule.dll', 'UnityEngine.dll', '0Harmony.dll', 'Newtonsoft.Json.dll')


def collect(root: Path, *, game_version: str | None = None, mods_dir: Path | None = None) -> dict:
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError('Point at the game directory or its Managed directory')
    candidates = ([root] if root.name == 'Managed' else [])
    candidates += list(root.glob('*_Data/Managed'))
    candidates += [root / 'Contents/Resources/Data/Managed']
    candidates += list(root.glob('*.app/Contents/Resources/Data/Managed'))
    managed = next((p for p in candidates if p.is_dir() and not p.is_symlink()
                    and any((p / name).is_file() for name in ASSEMBLIES)), None)
    if managed is None:
        raise ValueError('No known managed game assemblies found; select the game or Managed directory')
    assemblies = []
    for name in ASSEMBLIES:
        path = managed / name
        if not path.is_file() or path.is_symlink():
            continue
        hasher = hashlib.sha256()
        with path.open('rb') as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b''):
                hasher.update(chunk)
        assemblies.append({'name': name, 'bytes': path.stat().st_size, 'sha256': hasher.hexdigest()})
    mods = []
    if mods_dir is not None:
        if not mods_dir.is_dir():
            raise ValueError('mods-dir must be a directory')
        for directory in sorted(mods_dir.iterdir()):
            if not directory.is_dir() or directory.is_symlink():
                continue
            manifest = directory / 'manifest.json'
            if not manifest.is_file() or manifest.is_symlink() or manifest.stat().st_size > 65536:
                continue
            try:
                data = json.loads(manifest.read_text(encoding='utf-8-sig'))
            except (OSError, ValueError):
                mods.append({'directory_name': directory.name, 'manifest_status': 'unreadable'})
                continue
            if not isinstance(data, dict):
                continue
            entry = {'directory_name': directory.name}
            for name in ('ID', 'Title', 'Version', 'id', 'title', 'version'):
                if name in data and isinstance(data[name], (str, int, float, bool)):
                    entry[name] = data[name]
            mods.append(entry)
    return {'manifest_version': '0.1', 'game_version': game_version,
            'platform': {'system': platform.system(), 'release': platform.release(),
                         'machine': platform.machine()},
            'assemblies': assemblies, 'mods': mods,
            'notes': ['Hashes and names only; no game binaries or saves included.',
                      'Mod inventory does not establish which mods are enabled or their load order.',
                      'Review before sharing; game_version is user supplied, not detected.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('game_dir', type=Path)
    parser.add_argument('--game-version', help='Exact build string shown by Qud')
    parser.add_argument('--mods-dir', type=Path)
    parser.add_argument('--output', type=Path, help='New file; refuses to overwrite. Otherwise prints JSON.')
    args = parser.parse_args()
    try:
        info = collect(args.game_dir, game_version=args.game_version, mods_dir=args.mods_dir)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    text = json.dumps(info, indent=2, allow_nan=False) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open('x', encoding='utf-8') as file:
            file.write(text)
    else:
        print(text, end='')


if __name__ == '__main__':
    main()
