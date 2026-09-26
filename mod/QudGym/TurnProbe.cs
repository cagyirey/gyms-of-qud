using HarmonyLib;
using XRL;

// The game calls this when a new character creates the player object.
// OpeningStory is the one-time arrival modal; it is not an agent action.
[PlayerMutator]
public class QudGymPlayerProbe : IPlayerMutator
{
    public void mutate(XRL.World.GameObject player)
    {
        try
        {
            player.RemovePart("OpeningStory");
        }
        catch (System.Exception ex)
        {
            QudGymBridge.Note("opening story remove failed " + ex.GetType().Name);
        }
        QudGymBridge.Mutate(player);
    }
}

// "Starting Game..." is the correct MOMENT to embark: it is when the game
// commits to a run, before the character-creation UI takes over. That is why
// Embark.fs can boot a build sheet with no character UI at all.
//
// It is not a safe moment to block on the Unity UI context, though: on a cold
// or freshly updated install the main thread can still be streaming object
// blueprints, and the core thread would wait on a thread that is not pumping.
// Embark.onUi therefore retries in bounded attempts and releases its one-shot
// latch on failure, so a slow load delays the embark instead of killing it.
[HarmonyPatch(typeof(XRL.Core.XRLCore), nameof(XRL.Core.XRLCore.WriteConsoleLine))]
static class QudGymEmbarkHook
{
    static void Postfix(string s)
    {
        if (s != "Starting Game...")
            return;
        QudGymBridge.Embark();
    }
}

// PlayerTurn reaches this when it still needs a command. Blocking here is the
// decision boundary: the original wait is skipped once a command is queued.
[HarmonyPatch(typeof(ConsoleLib.Console.Keyboard), nameof(ConsoleLib.Console.Keyboard.IdleWait))]
static class QudGymInputGate
{
    static bool Prefix()
    {
        return !QudGymBridge.SupplyCommand();
    }
}

[HarmonyPatch(typeof(XRL.UI.Popup), nameof(XRL.UI.Popup.Show), new System.Type[] { typeof(string), typeof(string), typeof(string), typeof(bool), typeof(bool), typeof(bool), typeof(bool), typeof(Genkit.Location2D) })]
static class QudGymPopupGate
{
    static bool Prefix()
    {
        return QudGymBridge.AllowPopup();
    }
}
