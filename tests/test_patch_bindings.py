"""Patch-target binding: the pure wrapper everywhere, the real game opt-in.

Every test that needs the installed game is opt-in, because the repository has to
stay testable without a Qud install. The one test that matters -- that every
patch target binds against the real assemblies -- runs in the install-handoff job
alongside the metadata probe.
"""
import importlib.util
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "check_patch_bindings", ROOT / "scripts/check_patch_bindings.py")
BINDINGS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BINDINGS)

SAMPLE = """\
ok    QudGymWorldGenGate        ShowWorldGenerationScreen(by name)
FAIL  NotificationSpaceGate    ShowSpace(String,String,String,Boolean)
FAIL  NotificationBlockSpaceGate  ShowBlockSpace(String,String,Boolean,Boolean,IRenderable,Boolean,Boolean,Boolean,Boolean)

resolved 1, unresolved 2
"""


def test_failures_are_reported_in_declaration_order():
    assert BINDINGS.parse_failures(SAMPLE) == [
        "ShowSpace(String,String,String,Boolean)",
        "ShowBlockSpace(String,String,Boolean,Boolean,IRenderable,Boolean,Boolean,Boolean,Boolean)",
    ]


def test_a_clean_run_reports_nothing():
    clean = "ok    QudGymWorldGenGate  ShowWorldGenerationScreen(by name)\n\nresolved 1, unresolved 0\n"
    assert BINDINGS.parse_failures(clean) == []


def test_missing_install_is_named(tmp_path):
    with pytest.raises(BINDINGS.BindingError, match="no game assemblies"):
        BINDINGS.find_managed(tmp_path)


def test_unbuilt_mod_is_refused_without_running_anything(tmp_path, monkeypatch):
    managed = tmp_path / "CoQ.app/Contents/Resources/Data/Managed"
    managed.mkdir(parents=True)
    monkeypatch.setattr(BINDINGS.shutil, "which", lambda _: "/synthetic/dotnet")
    monkeypatch.setattr(BINDINGS.subprocess, "run",
                        lambda *a, **k: pytest.fail("must not invoke the SDK"))
    with pytest.raises(BINDINGS.BindingError, match="not built"):
        BINDINGS.check(managed, mod_dll=managed / "QudGym.dll")


def test_missing_sdk_is_reported_rather_than_assumed(tmp_path, monkeypatch):
    managed = tmp_path / "CoQ.app/Contents/Resources/Data/Managed"
    managed.mkdir(parents=True)
    monkeypatch.setattr(BINDINGS.shutil, "which", lambda _: None)
    with pytest.raises(BINDINGS.BindingError, match=r"\.NET SDK"):
        BINDINGS.check(managed, mod_dll=managed / "QudGym.dll")


def test_a_crashed_check_is_not_mistaken_for_a_clean_result(tmp_path, monkeypatch):
    managed = tmp_path / "CoQ.app/Contents/Resources/Data/Managed"
    managed.mkdir(parents=True)
    dll = managed / "QudGym.dll"
    dll.write_bytes(b"stub")
    monkeypatch.setattr(BINDINGS.shutil, "which", lambda _: "/synthetic/dotnet")

    def run(command, **kwargs):
        # No FAIL lines, but the script died: that is a broken check, not a pass.
        return SimpleNamespace(returncode=3, stdout="", stderr="could not load 0Harmony.dll")
    monkeypatch.setattr(BINDINGS.subprocess, "run", run)
    with pytest.raises(BINDINGS.BindingError, match="did not run"):
        BINDINGS.check(managed, dll)


def test_the_real_install_and_mod_bind(tmp_path):
    """The test that would have caught the boot failure, at the cost of a second."""
    if os.environ.get("QUDGYM_RUN_DOTNET_PROBE_TESTS") != "1":
        pytest.skip("requires .NET SDK and an installed game; executed by install-handoff CI")
    for project in ("mod/QudGym/QudGym.csproj", "mod/QudGym.Impl/QudGym.Impl.fsproj"):
        subprocess.run(["dotnet", "build", str(ROOT / project), "-c", "Release"],
                       check=True, timeout=900, capture_output=True)
    managed = BINDINGS.find_managed(
        Path.home() / "Library/Application Support/Steam/steamapps/common/Caves of Qud")
    assert BINDINGS.check(managed) == []
