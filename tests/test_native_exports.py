import hashlib
import json
from pathlib import Path

import pytest

from qudgym.eye.builds import BuildLibrary, BuildPreset, inspect_sources, load_library
from qudgym.native_exports import (
    InstallManifest, NativeExport, NativeExportSource, export_summary, find_managed,
    read_export, read_json,
)

ROOT = Path(__file__).resolve().parents[1]


def test_real_library_sources_are_pinned_and_inert():
    library = load_library(ROOT / "builds/library.json")
    reports = inspect_sources(library, ROOT)
    assert {r["id"] for r in reports} == {"artifex", "marauder"}
    for report in reports:
        assert report["declared_export_game_version"] == "2.0.211.55"
        assert report["enabled_mods_in_order"] is None
        assert report["game_legality_checked"] is False
        assert report["character_instantiated"] is False
    artifex = next(r for r in reports if r["id"] == "artifex")
    assert artifex["selections"]["QudAttributesModule"]["PointsPurchased"]["Strength"] == 4
    assert artifex["selections"]["QudAttributesModule"]["apSpent"] == -38


def test_v1_creation_code_remains_supported():
    preset = BuildPreset(id="example", revision="1", game_build="test", creation_code="supplied")
    assert BuildLibrary(presets=(preset,)).by_id("example") == preset
    assert preset.schema_version == "qud-build/1"
    assert preset.mods == ()


@pytest.mark.parametrize("code", [None, "", "  "])
def test_no_missing_or_blank_source(code):
    with pytest.raises(ValueError):
        BuildPreset(id="p", revision="1", game_build="x", creation_code=code)


def test_native_sources_require_v2_and_one_source():
    source = NativeExportSource(path="loadouts/example.json", sha256="0" * 64)
    with pytest.raises(ValueError):
        BuildPreset(id="p", revision="1", game_build="x", native_export=source)
    preset = BuildPreset(schema_version="qud-build/2", id="p", revision="1", game_build="x",
                         native_export=source, mods=None)
    with pytest.raises(ValueError):
        BuildLibrary(presets=(preset,))
    with pytest.raises(ValueError):
        BuildPreset(schema_version="qud-build/2", id="p", revision="1", game_build="x",
                    native_export=source, creation_code="also supplied")


@pytest.mark.parametrize("path", ["/a.json", "../a.json", "x/../a.json", "C:/a.json",
                                     "a\\b.json", "./a.json", "x//a.json", "a.dll"])
def test_native_path_shape(path):
    with pytest.raises(ValueError):
        NativeExportSource(path=path, sha256="0" * 64)


def test_hash_mismatch(tmp_path):
    path = tmp_path / "a.json"
    path.write_text("{}")
    with pytest.raises(ValueError, match="hash changed"):
        read_export(NativeExportSource(path="a.json", sha256="0" * 64), tmp_path)


def test_source_root_symlink_escape(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    (root / "link.json").symlink_to(outside)
    with pytest.raises(ValueError, match="outside"):
        read_export(NativeExportSource(path="link.json", sha256=hashlib.sha256(b"{}").hexdigest()), root)


@pytest.mark.parametrize("raw", ['{"a":1,"a":2}', '{"x":NaN}', '{"x":1e1000}', '[]'])
def test_strict_json(tmp_path, raw):
    file = tmp_path / "x.json"
    file.write_text(raw)
    with pytest.raises(ValueError):
        read_json(file)


def test_bounded_json(tmp_path):
    file = tmp_path / "big.json"
    file.write_bytes(b" " * (1024 * 1024 + 1))
    with pytest.raises(ValueError, match="1 MiB"):
        read_json(file)


def test_unknown_clr_module_stays_data():
    export = NativeExport.model_validate({"gameversion": "x", "buildversion": "y", "modules": [{
        "moduleType": "Untrusted.Type, ArbitraryAssembly", "data": {"$type": "Other.Type", "nested": [1]}
    }]})
    assert export_summary(export)["uninterpreted_modules"] == ["Untrusted.Type"]
    assert export_summary(export)["selections"] == {}


def test_duplicate_clr_module_rejected():
    data = {"gameversion": "x", "buildversion": "y", "modules": [
        {"moduleType": "A, Version=1", "data": {}},
        {"moduleType": "A, Version=2", "data": {}},
    ]}
    with pytest.raises(ValueError, match="duplicate"):
        NativeExport.model_validate(data)


@pytest.mark.parametrize("relative", ["Managed", "Qud_Data/Managed", "Contents/Resources/Data/Managed",
                                      "Qud.app/Contents/Resources/Data/Managed"])
def test_install_layouts(tmp_path, relative):
    managed = tmp_path / relative
    managed.mkdir(parents=True)
    (managed / "Assembly-CSharp.dll").write_bytes(b"fixture")
    selected = managed if relative == "Managed" else tmp_path
    assert find_managed(selected) == managed


def test_ambiguous_install_rejected(tmp_path):
    for app in ("A.app", "B.app"):
        path = tmp_path / app / "Contents/Resources/Data/Managed"
        path.mkdir(parents=True)
        (path / "Assembly-CSharp.dll").write_bytes(b"test")
    with pytest.raises(ValueError, match="exactly one"):
        find_managed(tmp_path)


def test_install_symlink_escape_rejected(tmp_path):
    root = tmp_path / "game"
    root.mkdir()
    outside = tmp_path / "outside.dll"
    outside.write_bytes(b"fixture")
    (root / "Assembly-CSharp.dll").symlink_to(outside)
    with pytest.raises(ValueError, match="outside"):
        find_managed(root)


def test_manifest_declarations_are_not_reconciled():
    manifest = InstallManifest.model_validate({"manifest_version": "0.1", "game_version": "2.0.4",
        "platform": {"system": "Darwin", "machine": "arm64"},
        "assemblies": [{"name": "Assembly-CSharp.dll", "bytes": 1, "sha256": "0" * 64}],
        "mods": [{"id": "ExampleContentPack"}]})
    assert manifest.game_version == "2.0.4"
    assert manifest.game_assembly().name == "Assembly-CSharp.dll"
    assert "enabled" not in manifest.mods[0]
    assert "version" not in manifest.game_assembly().model_dump()


def test_manifest_duplicate_assembly_rejected():
    entry = {"name": "Assembly-CSharp.dll", "bytes": 1, "sha256": "0" * 64}
    with pytest.raises(ValueError, match="duplicate"):
        InstallManifest.model_validate({"manifest_version": "0.1", "platform": {}, "assemblies": [entry, entry]})


def test_preset_declared_version_must_match_source():
    library = load_library(ROOT / "builds/library.json")
    first = library.presets[0].model_copy(update={"game_build": "different"})
    with pytest.raises(ValueError, match="disagree"):
        inspect_sources(library.model_copy(update={"presets": (first,)}), ROOT)
