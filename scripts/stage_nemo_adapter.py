#!/usr/bin/env python3
"""Stage into the pinned local NeMo Gym checkout; never overwrite one."""
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

NEMO_GYM_COMMIT = '1c8261080bdc881b3e9b7f870e6418f160516991'


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ['git', '-C', str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _validate_checkout(nemo_root: Path, *, allow_commit_drift: bool) -> None:
    if allow_commit_drift:
        return
    try:
        actual = _git(nemo_root, 'rev-parse', 'HEAD')
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError('Target must be a git NeMo Gym checkout') from exc
    if actual != NEMO_GYM_COMMIT:
        raise ValueError(
            f'NeMo Gym commit mismatch: expected {NEMO_GYM_COMMIT}, found {actual}'
        )
    if _git(nemo_root, 'status', '--porcelain', '--untracked-files=all'):
        raise ValueError('NeMo Gym checkout has tracked or untracked local changes; review or set the explicit drift override')


def _verify(project: Path, *args: str) -> None:
    command = [
        sys.executable,
        str(project / 'scripts/verify_nemo_adapter.py'),
        '--project-root',
        str(project),
        *args,
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        message = result.stderr.strip() or result.stdout.strip() or 'adapter verification failed'
        raise ValueError(message)


def stage(nemo_root: Path, project: Path, *, allow_commit_drift: bool = False) -> Path:
    nemo_root, project = nemo_root.resolve(strict=True), project.resolve(strict=True)
    _verify(project)
    _validate_checkout(nemo_root, allow_commit_drift=allow_commit_drift)
    for required in (
        'pyproject.toml',
        'nemo_gym/base_resources_server.py',
        'resources_servers/gymnasium/base.py',
    ):
        if not (nemo_root / required).is_file():
            raise ValueError('Target is not a NeMo Gym checkout with the native Gymnasium adapter')
    destination = nemo_root / 'resources_servers/qudgym'
    if destination.exists():
        raise FileExistsError('resources_servers/qudgym already exists; refusing to overwrite')
    shutil.copytree(
        project / 'integrations/nemo_gym/qudgym',
        destination,
        ignore=shutil.ignore_patterns(
            'source_manifest.json', 'requirements.txt', '__pycache__', '*.pyc', '*.pyo', '.venv', '*.log'
        ),
    )
    # The isolated server venv needs both local projects. NeMo Gym is editable
    # because this adapter is reviewed against one pinned checkout; QudGym is
    # likewise local until an internal wheel/index exists.
    requirements = (
        f'-e nemo-gym[dev] @ {nemo_root.as_uri()}\n'
        f'qudgym @ {project.as_uri()}\n'
    )
    (destination / 'requirements.txt').write_text(requirements, encoding='utf-8')
    _verify(project, '--check-staged', '--nemo-root', str(nemo_root))
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('nemo_root', type=Path)
    args = parser.parse_args()
    try:
        result = stage(
            args.nemo_root,
            Path(__file__).resolve().parents[1],
            allow_commit_drift=os.environ.get('NEMO_GYM_ALLOW_COMMIT_DRIFT') == '1',
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f'Staged {result}. Run NeMo Gym validation before starting services.')


if __name__ == '__main__':
    main()
