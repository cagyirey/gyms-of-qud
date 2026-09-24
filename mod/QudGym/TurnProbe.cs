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

// _Start logs this on the game thread after LoadEverything, before the menu
// loop. The view is already MainMenu, so the menu never calls SetGameViewStack.
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
