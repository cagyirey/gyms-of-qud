import hashlib
import json
import runpy
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
COLLECT = runpy.run_path(str(ROOT / 'scripts/collect_install_info.py'))['collect']
STAGE = runpy.run_path(str(ROOT / 'scripts/stage_nemo_adapter.py'))['stage']


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
    (tmp_path / 'nemo_gym').mkdir()
    (tmp_path / 'nemo_gym/base_resources_server.py').touch()
    (tmp_path / 'resources_servers/gymnasium').mkdir(parents=True)
    (tmp_path / 'resources_servers/gymnasium/base.py').touch()
    dest = STAGE(tmp_path, ROOT)
    assert 'qudgym @ file:' in (dest / 'requirements.txt').read_text()
    assert (dest / 'app.py').is_file()
    with pytest.raises(FileExistsError):
        STAGE(tmp_path, ROOT)


def test_generated_schemas_are_current():
    from qudgym.models import RpcRequest, RpcResponse, Observation, Transition, Capabilities
    for model in (RpcRequest, RpcResponse, Observation, Transition, Capabilities):
        assert json.loads((ROOT / 'schemas' / f'{model.__name__}.schema.json').read_text()) == model.model_json_schema()
