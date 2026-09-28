"""Every popup the game can raise is either covered by Suppress or gated by name.

Notifications used to be handled by naming each one, and that is a losing game: the
method list has to stay complete, and nothing fails when a popup is missed. The
game already has the generic answer -- eight of its sixteen public popups consult
`XRL.UI.Popup.Suppress`, and the suppressed branch still logs through the game's
own message path -- so the flag is set once and the remainder is a short, finite,
enumerable list.

This test is what makes that a property rather than a hope. It reads the decompiled
`Popup` when it is present and asserts that every public popup is either covered by
the flag or gated by name here, so a new popup fails rather than blocking a run.

Skipped without the decompile, which is gitignored: the game install is not
something this repository can require.
"""
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
POPUP_CS = ROOT / "local/decompile-full/XRL.UI/Popup.cs"
PROBE = ROOT / "mod/QudGym/PromptProbe.cs"

# The popups that consult Suppress. Read from the source when it is available; this
# is the fallback so the test still says something useful without the decompile.
SUPPRESSED = {
    "PickOption",
    "ShowAsync",
    "ShowBlock",
    "ShowBlockPrompt",
    "ShowBlockSpace",
    "ShowSpace",
}

# Gated by name, because they ignore the flag.
GATED = {
    "WaitNewPopupMessage",
    "ShowBlockWithCopy",
    "ShowConversation",
    "ShowFail",
    "ShowYesNo",
    "ShowYesNoCancel",
}

# Known unhandled, each with the reason it cannot block a run. Kept visible rather
# than absent, because "not in the list" and "considered and dismissed" are
# different claims and only one of them is true.
#
# The rule is reachability, not importance: a popup nothing in the game calls, or
# that only a screen this harness never opens can raise, cannot stop a run. Each
# entry has to say which, and the test checks that the reason is present.
NOT_YET_HANDLED = {
    "ShowOptionList": "no callers anywhere in the decompiled assembly",
    "ShowProgress": "no callers anywhere in the decompiled assembly",
    "ShowKeybindAsync": "raised only by AbilityManagerScreen, a window the harness does not open",
    # A real gap, and the one to fix next: naming an item opens a colour picker and
    # it blocks. No quest step taken so far names anything.
    "ShowColorPicker": "GAP: reachable by naming an item, which blocks; no quest step does that yet",
    # The async popup surface. These do not delegate to the sync methods: the async
    # Yes/No pair goes through NewPopupMessageAsync, which consults nothing, and the
    # colour picker and option list go through PickOptionAsync, which the flag does
    # cover. NewPopupMessageAsync and WaitNewPopupMessage are themselves ungated,
    # and the Wishing well calls the latter -- so this is a real gap on a path the
    # quest has not reached, not dead code.
    "ShowYesNoAsync": "GAP: routes through NewPopupMessageAsync, which is not yet gated",
    "ShowYesNoCancelAsync": "GAP: routes through NewPopupMessageAsync, which is not yet gated",
    "ShowFailAsync": "routes through ShowAsync, which honours Suppress",
    "ShowColorPickerAsync": "routes through PickOptionAsync, which honours Suppress",
    "ShowOptionListAsync": "routes through PickOptionAsync, which honours Suppress",
    # NewPopupMessageAsync is the async popup surface. ShowYesNoAsync and
    # ShowYesNoCancelAsync route through it, and it ignores Suppress, so it is the
    # remaining gap.
    "NewPopupMessageAsync": "GAP: the async popup surface itself; ShowYesNoAsync and ShowYesNoCancelAsync route through it",
}

# Screens the harness does not drive, so anything only they can raise is unreachable.
UNDRIVEN_SCREENS = ("AbilityManagerScreen", "OptionsScreen", "KeybindsScreen", "StatusScreensScreen")


def _public_popups() -> set[str]:
    if not POPUP_CS.is_file():
        return set()
    src = POPUP_CS.read_text(errors="replace")
    names = set()
    for match in re.finditer(r"public static [A-Za-z0-9_<>, ]+ ([A-Za-z]+)\(", src):
        name = match.group(1)
        if name.startswith("Show") or name == "PickOption":
            names.add(name)
    return names


def _honours_suppress(name: str) -> bool:
    """Whether the method body consults Suppress, by brace matching from its signature."""
    src = POPUP_CS.read_text(errors="replace")
    match = re.search(r"public static [A-Za-z0-9_<>, ]+ " + re.escape(name) + r"\(", src)
    if not match:
        return False
    i, depth, out = src.index("{", match.end()), 0, []
    while i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                break
        out.append(src[i])
        i += 1
    return "Suppress" in "".join(out)


def _gated_in_mod() -> set[str]:
    if not PROBE.is_file():
        return set()
    return set(re.findall(r"nameof\(XRL\.UI\.Popup\.([A-Za-z]+)\)", PROBE.read_text(encoding="utf-8")))


@pytest.mark.skipif(
    not POPUP_CS.is_file(), reason="requires the decompiled Popup source in local/"
)
def test_every_public_popup_is_covered_or_gated():
    """The property that makes this not whack-a-mole.

    A popup that is neither covered by the flag nor gated blocks, and nothing
    notices until a run hangs on it. That is the failure this whole approach exists
    to remove, so it is asserted rather than documented.
    """
    covered = {name for name in _public_popups() if _honours_suppress(name)}
    gated = _gated_in_mod()
    accounted = {name for name, reason in NOT_YET_HANDLED.items() if reason.strip()}
    uncovered = sorted(_public_popups() - covered - gated - accounted)
    assert not uncovered, (
        f"popups that block and are neither suppressed, gated, nor reasoned: {uncovered}. "
        "Either the game honours Suppress for it, PromptProbe.cs needs a gate, or "
        "NOT_YET_HANDLED needs a stated reason."
    )


def test_every_unhandled_popup_says_why():
    """An entry without a reason is an omission wearing a note.

    The list is only honest if each entry says which of the two reasons applies --
    unreachable, or a gap that is still open. A bare name is indistinguishable from
    one that was added to make the test pass.
    """
    for name, reason in NOT_YET_HANDLED.items():
        assert reason.strip(), f"{name} is listed as unhandled with no reason"
        assert (
            "GAP" in reason
            or "no callers" in reason
            or "does not open" in reason
            or "honours Suppress" in reason
        ), (
            f"{name}'s reason does not say whether it is unreachable or an open gap: {reason!r}"
        )


@pytest.mark.skipif(
    not POPUP_CS.is_file(), reason="requires the decompiled Popup source in local/"
)
def test_the_fallback_lists_match_the_game():
    """The constants above are a fallback; they must not drift from the real thing."""
    popups = _public_popups()
    assert SUPPRESSED <= popups, f"listed as suppressed but absent: {sorted(SUPPRESSED - popups)}"
    for name in SUPPRESSED:
        assert _honours_suppress(name), f"{name} is listed as honouring Suppress and does not"
    for name in GATED:
        if name in popups:
            assert not _honours_suppress(name), f"{name} is listed as gated and honours Suppress"
            assert name in _gated_in_mod(), f"{name} is listed as gated but PromptProbe.cs lacks it"


def test_suppress_is_set_globally_not_only_around_one_call():
    """One flag set once, rather than a raise-and-restore per notification call.

    A per-call pattern is the thing being replaced: it is one place per popup, so
    the set has to be kept complete. Setting it at boot means a popup nobody has
    heard of is already covered.
    """
    if shutil.which("dotnet") is None:
        pytest.skip("requires the .NET SDK")
    embark = (ROOT / "mod/QudGym.Impl/Embark.fs").read_text(encoding="utf-8")
    assert "setPopupSuppress" in embark, "the global Suppress setter is gone"
    assert re.search(r"setPopupSuppress\s+true", embark), "Suppress is never set to true"
    # And the reason the per-call gates existed is now recorded where it is set.
    assert "Popup.Suppress" in embark or "popup Suppress" in embark


def test_the_blocking_popup_primitive_is_gated():
    """WaitNewPopupMessage is where the space bar comes from, so it is gated directly.

    The public-popup scan above only collects `Show*` and `PickOption`, so this
    method is invisible to it. It is not a popup by that naming, but it is the one
    that actually blocks: the modern-UI path of ShowBlockWithCopy enters it and the
    method's own return value is Keys.Space. It also ignores Popup.Suppress, and
    gating only its caller covered one caller of several -- the Wishing well and the
    death notices reach it too.
    """
    gated = _gated_in_mod()
    assert "WaitNewPopupMessage" in gated, (
        "WaitNewPopupMessage is ungated: examining anything with a copyable "
        "description blocks on a keypress the harness cannot deliver"
    )
