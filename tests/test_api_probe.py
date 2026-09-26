"""Pure wrapper tests run everywhere; the SDK integration is opt-in and required in its CI job."""
import hashlib
import importlib.util
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("inspect_qud_install", ROOT / "scripts/inspect_qud_install.py")
WRAPPER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(WRAPPER)


def manifest(tmp_path, payload=b"synthetic-file"):
    managed = tmp_path / "Qud.app/Contents/Resources/Data/Managed"
    managed.mkdir(parents=True)
    (managed / "Assembly-CSharp.dll").write_bytes(payload)
    doc = {"manifest_version": "0.1", "game_version": "2.0.4", "platform": {"machine": "arm64"},
           "assemblies": [{"name": "Assembly-CSharp.dll", "bytes": len(payload),
                           "sha256": hashlib.sha256(payload).hexdigest()}],
           "mods": [{"id": "ExamplePack"}]}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(doc))
    return path, doc


def test_wrapper_keeps_all_version_domains_separate(tmp_path, monkeypatch):
    path, doc = manifest(tmp_path)
    monkeypatch.setattr(WRAPPER.shutil, "which", lambda _: "/synthetic/dotnet")
    def run(command, **kwargs):
        assert kwargs["timeout"] == 180 and not kwargs["check"]
        assert command[-2] == doc["assemblies"][0]["sha256"]
        Path(command[-1]).write_text(json.dumps({"schema_version": "qud-api-metadata/1",
            "assembly": {"sha256": command[-2], "version": "2.0.211.55"},
            "runtime_hooks_verified": False}))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(WRAPPER.subprocess, "run", run)
    report = WRAPPER.collect(tmp_path / "Qud.app", path, ROOT / "builds/library.json", ROOT)
    assert report["reported_game_version"] == "2.0.4"
    assert report["api"]["assembly"]["version"] == "2.0.211.55"
    assert report["compatibility_status"] == "not_established"
    assert report["enabled_mods_in_order"] is None
    assert str(tmp_path) not in json.dumps(report)


@pytest.mark.parametrize("same_size", [True, False])
def test_wrapper_rejects_changed_install_before_starting_sdk(tmp_path, monkeypatch, same_size):
    path, _ = manifest(tmp_path, b"abcdef")
    (tmp_path / "Qud.app/Contents/Resources/Data/Managed/Assembly-CSharp.dll").write_bytes(
        b"ghijkl" if same_size else b"different-size")
    monkeypatch.setattr(WRAPPER.subprocess, "run", lambda *a, **k: pytest.fail("must not invoke SDK"))
    with pytest.raises(ValueError, match="differs"):
        WRAPPER.collect(tmp_path / "Qud.app", path, ROOT / "builds/library.json", ROOT)


def test_wrapper_requires_sdk(tmp_path, monkeypatch):
    path, _ = manifest(tmp_path)
    monkeypatch.setattr(WRAPPER.shutil, "which", lambda _: None)
    with pytest.raises(ValueError, match=".NET 10 SDK"):
        WRAPPER.collect(tmp_path / "Qud.app", path, ROOT / "builds/library.json", ROOT)


@pytest.mark.skipif(os.environ.get("QUDGYM_RUN_DOTNET_PROBE_TESTS") != "1",
                    reason="requires .NET 10 SDK; executed by install-handoff CI")
def test_metadata_probe_against_synthetic_assembly(tmp_path):
    probe = ROOT / "tools/QudGym.ApiProbe"
    fixture = ROOT / "tools/QudGym.ApiProbe.Fixture"
    for project in (probe, fixture):
        subprocess.run(["dotnet", "build", str(project), "-c", "Release", "-o", str(tmp_path / project.name)],
                       check=True, timeout=180)
    assembly = tmp_path / fixture.name / "QudGym.ApiProbe.Fixture.dll"
    executable = tmp_path / probe.name / "QudGym.ApiProbe.dll"
    before = assembly.read_bytes()
    digest = hashlib.sha256(before).hexdigest()
    report = tmp_path / "report.json"
    command = ["dotnet", str(executable), str(assembly), digest, str(report)]
    subprocess.run(command, check=True, timeout=30)
    data = json.loads(report.read_text())
    assert data["assembly"]["version"] == "2.0.211.55"
    assert data["assembly"]["sha256"] == digest
    assert data["game_code_executed"] is False
    assert data["runtime_hooks_verified"] is False
    assert str(tmp_path) not in report.read_text()
    types = {q["type_name"]: q for q in data["queries"]}
    assert types["XRL.IPlayerMutator"]["found"] is True
    assert types["XRL.World.Zone"]["found"] is False
    methods = {m["name"]: m for m in types["XRL.World.GameObject"]["members"]}
    assert methods["Stat"]["parameters"] == ["String"]
    assert methods["Stat"]["return_type"] == "Int32"
    assert methods["GetPart"]["generic_parameters"] == 1
    assert "OmittedSecret" not in methods
    snapshot = report.read_bytes()
    assert subprocess.run(command, timeout=30).returncode != 0  # refuse overwrite
    assert report.read_bytes() == snapshot
    command[-1] = str(tmp_path / "wrong-hash.json")
    command[-2] = "0" * 64
    assert subprocess.run(command, timeout=30).returncode != 0
    assert not Path(command[-1]).exists()
    malformed = tmp_path / "not-assembly.dll"
    malformed.write_bytes(b"not a managed assembly")
    command[-3] = str(malformed)
    command[-2] = hashlib.sha256(malformed.read_bytes()).hexdigest()
    command[-1] = str(tmp_path / "malformed.json")
    assert subprocess.run(command, timeout=30).returncode != 0
    assert not Path(command[-1]).exists()
    assert assembly.read_bytes() == before
