import hashlib
import json
import runpy
import shutil
import subprocess
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
COLLECT = runpy.run_path(str(ROOT / 'scripts/collect_install_info.py'))['collect']
STAGE = runpy.run_path(str(ROOT / 'scripts/stage_nemo_adapter.py'))['stage']
VERIFY = runpy.run_path(str(ROOT / 'scripts/verify_nemo_adapter.py'))


def test_manifest_does_not_expose_absolute_paths_or_copy_files(tmp_path):
    managed = tmp_path / 'CoQ_Data/Managed'
    managed.mkdir(parents=True)
    assembly = managed / 'Assembly-CSharp.dll'
    assembly.write_bytes(b'test-fixture-not-a-game-assembly')
    (tmp_path / 'valuable-save.sav').write_text('never read this')
    result = COLLECT(tmp_path, game_version='test-build')
    assert result['assemblies'][0]['sha256'] == hashlib.sha256(assembly.read_bytes()).hexdigest()
    serialized = json.dumps(result)
    assert str(tmp_path) not in serialized
    assert 'valuable-save' not in serialized
    assert 'never read' not in serialized
    assert result['game_version'] == 'test-build'


def test_manifest_refuses_unknown_install(tmp_path):
    with pytest.raises(ValueError):
        COLLECT(tmp_path)


def test_manifest_hashes_assembly_symlinks_inside_the_install(tmp_path):
    managed = tmp_path / 'CoQ_Data' / 'Managed'
    managed.mkdir(parents=True)
    target = managed / 'Assembly-CSharp.dll.real'
    target.write_bytes(b'assembly-bytes')
    (managed / 'Assembly-CSharp.dll').symlink_to(target.name)
    result = COLLECT(tmp_path, game_version='linked-build')
    assert result['assemblies'] == [{
        'name': 'Assembly-CSharp.dll',
        'bytes': len(b'assembly-bytes'),
        'sha256': hashlib.sha256(b'assembly-bytes').hexdigest(),
    }]


def test_manifest_rejects_assembly_symlink_that_leaves_the_install(tmp_path):
    game = tmp_path / 'game'
    managed = game / 'CoQ_Data' / 'Managed'
    managed.mkdir(parents=True)
    secret = tmp_path / 'secret.dll'
    secret.write_bytes(b'not-the-game')
    (managed / 'Assembly-CSharp.dll').write_bytes(b'present')
    (managed / 'XRL.dll').symlink_to(secret)
    with pytest.raises(ValueError):
        COLLECT(game)


def test_nemo_staging_does_not_overwrite(tmp_path):
    (tmp_path / 'pyproject.toml').touch()
    (tmp_path / 'nemo_gym').mkdir()
    (tmp_path / 'nemo_gym/base_resources_server.py').touch()
    (tmp_path / 'resources_servers/gymnasium').mkdir(parents=True)
    (tmp_path / 'resources_servers/gymnasium/base.py').touch()
    dest = STAGE(tmp_path, ROOT, allow_commit_drift=True)
    requirements = (dest / 'requirements.txt').read_text()
    assert 'nemo-gym[dev] @ file:' in requirements
    assert 'qudgym @ file:' in requirements
    assert (dest / 'app.py').is_file()
    assert (dest / 'task_data.py').is_file()
    with pytest.raises(FileExistsError):
        STAGE(tmp_path, ROOT, allow_commit_drift=True)


def test_nemo_staging_rejects_unpinned_checkouts_by_default(tmp_path):
    (tmp_path / 'pyproject.toml').touch()
    (tmp_path / 'nemo_gym').mkdir()
    (tmp_path / 'nemo_gym/base_resources_server.py').touch()
    (tmp_path / 'resources_servers/gymnasium').mkdir(parents=True)
    (tmp_path / 'resources_servers/gymnasium/base.py').touch()
    with pytest.raises(ValueError, match='git NeMo Gym checkout'):
        STAGE(tmp_path, ROOT)


def _make_nemo_checkout(root):
    (root / 'pyproject.toml').touch()
    (root / 'nemo_gym').mkdir()
    (root / 'nemo_gym/base_resources_server.py').touch()
    (root / 'resources_servers/gymnasium').mkdir(parents=True)
    (root / 'resources_servers/gymnasium/base.py').touch()
    subprocess.run(['git', 'init', '-q'], cwd=root, check=True)
    subprocess.run(['git', 'config', 'user.email', 'test@example.invalid'], cwd=root, check=True)
    subprocess.run(['git', 'config', 'user.name', 'Test'], cwd=root, check=True)
    subprocess.run(['git', 'add', '.'], cwd=root, check=True)
    subprocess.run(['git', '-c', 'commit.gpgsign=false', 'commit', '-qm', 'fixture'], cwd=root, check=True)
    return subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()


def test_nemo_staging_rejects_wrong_sha_and_all_untracked_files(tmp_path):
    commit = _make_nemo_checkout(tmp_path)
    original = STAGE.__globals__['NEMO_GYM_COMMIT']
    try:
        STAGE.__globals__['NEMO_GYM_COMMIT'] = '0' * 40
        with pytest.raises(ValueError, match='commit mismatch'):
            STAGE(tmp_path, ROOT)
        STAGE.__globals__['NEMO_GYM_COMMIT'] = commit

        tracked = tmp_path / 'pyproject.toml'
        original_contents = tracked.read_text()
        tracked.write_text('tracked drift')
        with pytest.raises(ValueError, match='tracked or untracked'):
            STAGE(tmp_path, ROOT, allow_commit_drift=False)
        tracked.write_text(original_contents)
        (tmp_path / 'unexpected.txt').touch()
        with pytest.raises(ValueError, match='tracked or untracked'):
            STAGE(tmp_path, ROOT, allow_commit_drift=False)
    finally:
        STAGE.__globals__['NEMO_GYM_COMMIT'] = original


def test_nemo_manifest_rejects_modified_and_extra_staged_files(tmp_path):
    _make_nemo_checkout(tmp_path)
    destination = STAGE(tmp_path, ROOT, allow_commit_drift=True)
    app = destination / 'app.py'
    original = app.read_bytes()
    app.write_bytes(original + b'\n# drift\n')
    with pytest.raises(ValueError, match='staged adapter file changed'):
        VERIFY['verify_staged'](ROOT, tmp_path)
    app.write_bytes(original)
    (destination / 'unexpected.py').write_text('shadow = True\n')
    with pytest.raises(ValueError, match='extra='):
        VERIFY['verify_staged'](ROOT, tmp_path)


def test_nemo_manifest_rejects_extra_source_files(tmp_path):
    project = tmp_path / 'project'
    adapter = project / 'integrations/nemo_gym/qudgym'
    adapter.parent.mkdir(parents=True)
    shutil.copytree(ROOT / 'integrations/nemo_gym/qudgym', adapter)
    (adapter / 'unexpected.py').write_text('shadow = True\n')
    with pytest.raises(ValueError, match='source adapter tree does not match manifest'):
        VERIFY['verify_source'](project)


def test_generated_schemas_are_current():
    from qudgym.models import RpcRequest, RpcResponse, Observation, Transition, Capabilities
    for model in (RpcRequest, RpcResponse, Observation, Transition, Capabilities):
        assert json.loads((ROOT / 'schemas' / f'{model.__name__}.schema.json').read_text()) == model.model_json_schema()
