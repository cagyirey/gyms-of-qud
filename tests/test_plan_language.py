"""The plan language has tests, and they run with the rest of the suite.

The plan language is F#, so its tests are an fsx (`plan_language.fsx`) rather than
another Python file. That split is worth a wrapper instead of a second command:
the plan is what a caller scripts against, and a language with no test is a
language whose behaviour is only known by having run the game.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

FSX = Path(__file__).resolve().parent / "plan_language.fsx"


@pytest.mark.skipif(shutil.which("dotnet") is None, reason="requires the .NET SDK")
def test_plan_language():
    """Asserted in the fsx, against the strings a live run actually produced.

    Skipped without the SDK rather than failed, so a checkout without .NET still
    runs the Python suite -- the same arrangement the patch-binding check uses.
    """
    result = subprocess.run(
        ["dotnet", "fsi", "--quiet", str(FSX)],
        capture_output=True,
        text=True,
        timeout=900,
    )
    report = (result.stdout + result.stderr).strip()
    assert result.returncode == 0, f"plan language checks failed:\n{report}"
    assert "0 failures" in report, report
