import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from qudgym.compat import DiagnosticReport, InstallManifest, parse_document

ROOT = Path(__file__).resolve().parents[1]


def manifest_payload():
    return {
        'manifest_version': '0.1',
        'game_version': 'test-build',
        'platform': {'system': 'TestOS', 'release': '1', 'machine': 'test'},
        'assemblies': [{'name': 'Assembly-CSharp.dll', 'bytes': 4, 'sha256': 'a' * 64}],
        'mods': [{'directory_name': 'ExampleMod', 'ID': 'example', 'Version': '1'}],
        'notes': ['synthetic fixture'],
    }


def diagnostic_payload():
    return {
        'schema_version': 'qudgym-compat/1',
        'game_build': 'test-build',
        'profile': 'dedicated-test',
        'enabled_mods': [{'mod_id': 'example', 'load_order': 0, 'version': '1'}],
        'hooks': [{'name': 'mod_loaded', 'available': True, 'thread_id': 7}],
        'notes': ['read-only diagnostic evidence'],
    }


def test_install_manifest_contract_accepts_redacted_collector_shape():
    document = InstallManifest.model_validate(manifest_payload())
    assert document.assemblies[0].sha256 == 'a' * 64
    assert document.mods[0].directory_name == 'ExampleMod'


def test_compatibility_contract_rejects_unsafe_or_ambiguous_entries():
    payload = diagnostic_payload()
    payload['game_build'] = '/Users/example/Caves of Qud'
    with pytest.raises(ValidationError):
        DiagnosticReport.model_validate(payload)

    payload = diagnostic_payload()
    payload['hooks'].append(payload['hooks'][0].copy())
    with pytest.raises(ValidationError):
        DiagnosticReport.model_validate(payload)

    payload = diagnostic_payload()
    payload['hooks'][0]['thread_id'] = None
    with pytest.raises(ValidationError):
        DiagnosticReport.model_validate(payload)

    payload = manifest_payload()
    payload['assemblies'].append(payload['assemblies'][0].copy())
    with pytest.raises(ValidationError):
        InstallManifest.model_validate(payload)


def test_bundled_diagnostic_template_is_valid():
    path = ROOT / 'docs' / 'compatibility-report.example.json'
    document = parse_document(path.read_bytes(), kind='diagnostic')
    assert isinstance(document, DiagnosticReport)
    assert document.profile == 'dedicated-test'


def test_schema_command_exports_compatibility_contracts(tmp_path):
    subprocess.run(
        [sys.executable, '-m', 'qudgym.cli', 'schema', '--output', str(tmp_path)],
        check=True, capture_output=True, text=True,
    )
    assert (tmp_path / 'InstallManifest.schema.json').is_file()
    assert (tmp_path / 'DiagnosticEvent.schema.json').is_file()
    assert (tmp_path / 'DiagnosticReport.schema.json').is_file()
    assert (tmp_path / 'AtofScopeEvent.schema.json').is_file()
    assert (tmp_path / 'AtofMarkEvent.schema.json').is_file()


def test_compat_cli_prints_only_a_summary(tmp_path):
    path = tmp_path / 'report.json'
    path.write_text(json.dumps(diagnostic_payload()), encoding='utf-8')
    result = subprocess.run(
        [sys.executable, '-m', 'qudgym.cli', 'compat-validate', str(path), '--kind', 'diagnostic'],
        check=True, capture_output=True, text=True,
    )
    summary = json.loads(result.stdout)
    assert summary == {
        'enabled_mod_count': 1,
        'game_build': 'test-build',
        'hook_count': 1,
        'kind': 'diagnostic-report',
        'profile': 'dedicated-test',
        'schema_version': 'qudgym-compat/1',
    }
    assert str(path) not in result.stdout
