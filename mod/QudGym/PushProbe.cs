using System;
using System.Reflection;
using HarmonyLib;
using XRL;

// Keyboard.PushCommand routes into PushMouseEvent, which can discard the event
// without a sound:
//
//     if (!TutorialManager.AllowMouseEvent(...) ||
//         (MouseEventQueue.Count > 2 && GameManager.bCapInputBuffer))
//         return;
//
// A discarded command still leaves the harness believing the action ran, which
// is how a live episode reports success while the player never moves. This
// records whether the event actually landed, and the two conditions that can
// stop it, so the cause is observed rather than inferred.
[HarmonyPatch(typeof(ConsoleLib.Console.Keyboard), nameof(ConsoleLib.Console.Keyboard.PushMouseEvent),
              new System.Type[] { typeof(string), typeof(int), typeof(int), typeof(object) })]
static class QudGymPushProbe
{
    static int depth;

    static void Postfix(string ev)
    {
        try
        {
            depth = (depth + 1) % 8;
            var keyboard = typeof(ConsoleLib.Console.Keyboard);
            var queueField = keyboard.GetField("MouseEventQueue",
                BindingFlags.Static | BindingFlags.NonPublic);
            int queued = 0;
            if (queueField != null && queueField.GetValue(null) is System.Collections.ICollection c)
                queued = c.Count;

            bool showingPopup = false;
            try
            {
                var tutorial = typeof(TutorialManager)
                    .GetProperty("instance", BindingFlags.Static | BindingFlags.Public)
                    ?.GetValue(null);
                if (tutorial != null)
                {
                    var prop = tutorial.GetType().GetProperty("ShowingPopup",
                        BindingFlags.Instance | BindingFlags.Public);
                    showingPopup = prop != null && (bool)prop.GetValue(tutorial, null);
                }
            }
            catch (Exception ex)
            {
                QudGymBridge.Note("push probe tutorial read failed " + ex.GetType().Name);
            }

            bool cap = false;
            try
            {
                var gm = typeof(GameManager);
                var f = gm.GetField("bCapInputBuffer", BindingFlags.Static | BindingFlags.Public);
                cap = f != null && (bool)f.GetValue(null);
            }
            catch { }

            QudGymBridge.Note(string.Format(
                "push {0} ev={1} queued={2} showingPopup={3} cap={4} wouldDrop={5}",
                depth, ev, queued, showingPopup, cap, showingPopup || (queued > 2 && cap)));
        }
        catch (Exception ex)
        {
            QudGymBridge.Note("push probe failed " + ex.GetType().Name);
        }
    }
}
