#!/usr/bin/env python3
"""Verify the exact QudGym adapter tree that a native NeMo Gym run executes."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path, PurePosixPath

MANIFEST_NAME = 'source_manifest.json'
GENERATED_REQUIREMENTS = 'requirements.txt'
_SHA256 = re.compile(r'^[0-9a-f]{64}$')


class VerificationError(ValueError):
    """The staged tree is not the reviewed adapter tree."""


def _source_root(project_root: Path) -> Path:
    return project_root / 'integrations/nemo_gym/qudgym'


def _is_runtime_artifact(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    return (
        relative == Path('.venv.setup.lock')
        or relative.parts[0:1] == ('.venv',)
        or '__pycache__' in relative.parts
        or (path.suffix in {'.pyc', '.pyo'} and '__pycache__' in relative.parts)
    )


def _files(root: Path, *, source: bool) -> dict[str, Path]:
    if root.is_symlink():
        raise VerificationError(f'adapter directory must not be a symlink: {root}')
    if not root.is_dir():
        raise VerificationError(f'adapter directory does not exist: {root}')
    files: dict[str, Path] = {}
    for path in root.rglob('*'):
        relative_path = path.relative_to(root)
        relative = relative_path.as_posix()
        if _is_runtime_artifact(path, root):
            continue
        if path.is_symlink():
            raise VerificationError(f'symlinks are not allowed in the adapter tree: {relative}')
        if path.is_dir():
            continue
        if not path.is_file():
            raise VerificationError(f'non-regular adapter entry: {relative}')
        if relative == GENERATED_REQUIREMENTS or (source and relative == MANIFEST_NAME):
            continue
        files[relative] = path
    return files


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _load_manifest(path: Path) -> dict[str, str]:
    try:
        document = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise VerificationError(f'cannot read adapter source manifest: {path}') from exc
    if document.get('algorithm') != 'sha256' or not isinstance(document.get('files'), dict):
        raise VerificationError('adapter source manifest has an unsupported shape')
    files: dict[str, str] = {}
    for name, digest in document['files'].items():
        if not isinstance(name, str) or not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise VerificationError('adapter source manifest contains an invalid entry')
        parsed = PurePosixPath(name)
        if parsed.is_absolute() or '..' in parsed.parts or name in {MANIFEST_NAME, GENERATED_REQUIREMENTS}:
            raise VerificationError('adapter source manifest contains an unsafe path')
        files[name] = digest
    if not files:
        raise VerificationError('adapter source manifest is empty')
    return files


def _compare(root: Path, expected: dict[str, str], *, label: str) -> None:
    actual = _files(root, source=label == 'source')
    expected_names = set(expected)
    actual_names = set(actual)
    if expected_names != actual_names:
        missing = sorted(expected_names - actual_names)
        extra = sorted(actual_names - expected_names)
        details = []
        if missing:
            details.append(f'missing={missing}')
        if extra:
            details.append(f'extra={extra}')
        raise VerificationError(f'{label} adapter tree does not match manifest: ' + '; '.join(details))
    for name, digest in expected.items():
        if _sha256(actual[name]) != digest:
            raise VerificationError(f'{label} adapter file changed: {name}')


def verify_source(project_root: Path) -> None:
    source_root = _source_root(project_root)
    manifest = _load_manifest(source_root / MANIFEST_NAME)
    _compare(source_root, manifest, label='source')


def _expected_requirements(project_root: Path, nemo_root: Path) -> str:
    return (
        f'-e nemo-gym[dev] @ {nemo_root.as_uri()}\n'
        f'qudgym @ {project_root.as_uri()}\n'
    )


def verify_staged(project_root: Path, nemo_root: Path) -> None:
    source_root = _source_root(project_root)
    manifest = _load_manifest(source_root / MANIFEST_NAME)
    staged_root = nemo_root / 'resources_servers/qudgym'
    _compare(staged_root, manifest, label='staged')
    requirements = staged_root / GENERATED_REQUIREMENTS
    try:
        actual = requirements.read_text(encoding='utf-8')
    except OSError as exc:
        raise VerificationError('staged adapter has no generated requirements file') from exc
    if actual != _expected_requirements(project_root.resolve(), nemo_root.resolve()):
        raise VerificationError('staged adapter requirements do not match this checkout')


def clean_runtime_caches(root: Path) -> None:
    """Remove interpreter caches created inside the generated destination tree."""
    if not root.exists():
        return
    for path in sorted(root.rglob('__pycache__'), key=lambda item: len(item.parts), reverse=True):
        if path.is_symlink():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
    for path in root.rglob('*'):
        if path.is_file() and path.suffix in {'.pyc', '.pyo'}:
            path.unlink()


def write_source_manifest(project_root: Path) -> Path:
    source_root = _source_root(project_root)
    files = _files(source_root, source=True)
    document = {
        'algorithm': 'sha256',
        'files': {name: _sha256(path) for name, path in sorted(files.items())},
    }
    manifest_path = source_root / MANIFEST_NAME
    manifest_path.write_text(json.dumps(document, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    return manifest_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--nemo-root', type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--write-source-manifest', action='store_true')
    mode.add_argument('--check-staged', action='store_true')
    parser.add_argument('--clean-caches', action='store_true')
    args = parser.parse_args()
    project_root = args.project_root.resolve()
    try:
        if args.write_source_manifest:
            print(write_source_manifest(project_root))
        elif args.check_staged:
            if args.nemo_root is None:
                parser.error('--nemo-root is required with --check-staged')
            if args.clean_caches:
                clean_runtime_caches(args.nemo_root.resolve() / 'resources_servers/qudgym')
            verify_staged(project_root, args.nemo_root.resolve())
        else:
            verify_source(project_root)
    except (OSError, VerificationError) as exc:
        print(f'adapter verification failed: {exc}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
