#!/usr/bin/env python3
"""Stage this adapter into an explicit local NeMo Gym checkout; never overwrites one."""
import argparse
import shutil
from pathlib import Path


def stage(nemo_root: Path, project: Path) -> Path:
    nemo_root, project = nemo_root.resolve(strict=True), project.resolve(strict=True)
    for required in ('nemo_gym/base_resources_server.py', 'resources_servers/gymnasium/base.py'):
        if not (nemo_root / required).is_file():
            raise ValueError('Target is not a NeMo Gym checkout with the native Gymnasium adapter')
    destination = nemo_root / 'resources_servers/qudgym'
    if destination.exists():
        raise FileExistsError('resources_servers/qudgym already exists; refusing to overwrite')
    shutil.copytree(project / 'integrations/nemo_gym/qudgym', destination,
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    # Local-only generated path, never part of the shared source recipe.
    (destination / 'requirements.txt').write_text(f'qudgym @ {project.as_uri()}\n', encoding='utf-8')
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('nemo_root', type=Path)
    args = parser.parse_args()
    try:
        result = stage(args.nemo_root, Path(__file__).resolve().parents[1])
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f'Staged {result}. Runtime integration still needs a NeMo smoke test.')


if __name__ == '__main__':
    main()
