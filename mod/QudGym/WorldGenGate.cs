using System.Threading.Tasks;
using HarmonyLib;
using Qud.UI;

// QudGameBootModule.bootGame contains exactly one statement that waits on the
// Unity UI thread:
//
//     _ = WorldGenerationScreen.ShowWorldGenerationScreen(258).Result;
//
// The screen itself is cosmetic: a quote and a progress bar. Show() only
// decides whether it is visible. The .Result is the problem.
// ShowWorldGenerationScreen is async and begins with `await The.UiContext`,
// and XRL.SynchronizationContextAwaiter reports IsCompleted only when
// SynchronizationContext.Current *is* that context. Called from the core
// thread it is not, so the await posts to the UI thread and .Result then
// blocks until the main thread runs it. World generation therefore inherits
// the window's focus and throttling state: the same embark completed on 6 of 8
// launches in local/live-evidence, and hung at "embark boot" on the rest,
// which is a focus-dependent race rather than a logic error.
//
// Note that the game's own EmbarkBuilder calls the same method at line 271
// *without* .Result. Fire-and-forget is the intended usage; bootGame is the
// only caller that blocks, and only for a cosmetic overlay.
//
// Skipping the call is safe for the rest of boot. SingletonWindowBase assigns
// its static `instance` in Init(), which the window manager calls when it
// builds the UI, not in Show(). So instance stays non-null and
// WorldCreationProgress.NextStep/StepProgress keep working: both call
// AddMessage and IncrementProgress unconditionally, and both of those are
// `async void`, so they post to the UI context without blocking. They update a
// window that is simply never shown.
//
// If a future build makes Show() responsible for something other than
// visibility, this patch would need revisiting. Nothing observed in 2.0.211.56
// suggests it does.
[HarmonyPatch(typeof(WorldGenerationScreen), nameof(WorldGenerationScreen.ShowWorldGenerationScreen))]
static class QudGymWorldGenGate
{
    static bool Prefix(ref Task<bool> __result)
    {
        __result = Task.FromResult(true);
        return false;
    }
}
