using HarmonyLib;
using System.Collections.Generic;
using System.Threading;
using UnityEngine;

// Prompt observation only: records that a conversation/option prompt was
// raised, on which thread, and what choices it offered. It does not alter
// control flow, does not block, and does not answer the prompt. The point is to
// learn the thread before deciding how an agent-driven choice could be wired
// without reintroducing the wait-on-the-wrong-thread deadlock.
static class QudGymPromptProbe
{
    static void Record(string what, System.Threading.Thread thread, List<string> options)
    {
        try
        {
            var choices = new System.Text.StringBuilder();
            if (options != null)
            {
                for (int i = 0; i < options.Count && i < 8; i++)
                    choices.Append("[").Append(i).Append("]=").Append(options[i]).Append(" ");
            }
            string name = string.IsNullOrEmpty(thread.Name) ? "-" : thread.Name;
            Debug.Log("QudGym prompt " + what
                + " thread=" + thread.ManagedThreadId
                + " name=" + name
                + " core=" + XRL.Core.XRLCore.IsCoreThread
                + " options=" + choices.ToString().Trim());
            // Mirror into the mod's own evidence file via the existing note path.
            QudGymBridge.Note("prompt " + what + " thread=" + thread.ManagedThreadId
                + " name=" + name + " core=" + XRL.Core.XRLCore.IsCoreThread
                + " options=" + choices.ToString().Trim());
        }
        catch (System.Exception ex)
        {
            Debug.Log("QudGym prompt probe failed: " + ex.GetBaseException().Message);
        }
    }

    [HarmonyPatch(typeof(XRL.UI.Popup), nameof(XRL.UI.Popup.ShowConversation),
        new System.Type[] { typeof(string), typeof(XRL.World.GameObject), typeof(string),
                            typeof(List<string>), typeof(bool), typeof(bool), typeof(bool) })]
    static class ShowConversationHook
    {
        static void Prefix(string Title, XRL.World.GameObject Context, string Intro, List<string> Options)
        {
            Record("conversation:" + (Title ?? "?"), Thread.CurrentThread, Options);
        }
    }

    [HarmonyPatch(typeof(XRL.UI.Popup), nameof(XRL.UI.Popup.ShowYesNo),
        new System.Type[] { typeof(string), typeof(string), typeof(bool), typeof(XRL.UI.DialogResult),
                            typeof(System.Action<XRL.UI.DialogResult>) })]
    static class ShowYesNoHook
    {
        static void Prefix(string Message, string Sound)
        {
            Record("yesno:" + (Message ?? "?"), Thread.CurrentThread, null);
        }
    }
}

// The probe above watched Popup.ShowConversation, which is not the path a Qud
// conversation takes. Conversations go through ConversationUI.HaveConversation,
// so that probe could never fire -- which is most of why conversation has been
// invisible. These patch the real entry point and the real selection, so a
// conversation that opens, and every option taken, is observable.
[HarmonyPatch(typeof(XRL.UI.ConversationUI), nameof(XRL.UI.ConversationUI.HaveConversation),
    new System.Type[] { typeof(XRL.World.Conversations.Conversation), typeof(XRL.World.GameObject),
                        typeof(XRL.World.GameObject), typeof(XRL.World.GameObject),
                        typeof(XRL.World.GameObject), typeof(bool), typeof(bool), typeof(bool) })]
static class ConversationHook
{
    static void Prefix(XRL.World.GameObject Speaker)
    {
        try
        {
            var choices = XRL.UI.ConversationUI.CurrentChoices;
            QudGymBridge.Note("conversation open speaker=" + (Speaker == null ? "?" : Speaker.DisplayName)
                + " thread=" + Thread.CurrentThread.ManagedThreadId
                + " core=" + XRL.Core.XRLCore.IsCoreThread
                + " choices=" + (choices == null ? "null" : choices.Count.ToString()));
            if (choices != null)
            {
                foreach (var c in choices)
                    QudGymBridge.Note("  option: " + c.GetDisplayText(false));
            }
        }
        catch (System.Exception ex)
        {
            QudGymBridge.Note("conversation hook failed " + ex.GetBaseException().Message);
        }
    }
}

[HarmonyPatch(typeof(XRL.UI.ConversationUI), nameof(XRL.UI.ConversationUI.Select))]
static class ConversationSelectHook
{
    static void Prefix(int Choice)
    {
        try
        {
            var choices = XRL.UI.ConversationUI.CurrentChoices;
            string text = "?";
            if (choices != null && Choice >= 0 && Choice < choices.Count)
                text = choices[Choice].GetDisplayText(false);
            QudGymBridge.Note("conversation select " + Choice + " -> " + text);
        }
        catch (System.Exception ex)
        {
            QudGymBridge.Note("select hook failed " + ex.GetBaseException().Message);
        }
    }
}

// The modern conversation blocks in Popup.ShowConversation, which returns the
// chosen option index; ConversationUI.Render then acts on that index with the
// game's own Select. Intercepting the popup therefore replaces only the waiting,
// and the conversation still advances by the game's code.
//
// The original is skipped, so nothing blocks on a key only a human would send.
// The options come from the call itself, which is the authoritative list.
[HarmonyPatch(typeof(XRL.UI.Popup), nameof(XRL.UI.Popup.ShowConversation),
    new System.Type[] { typeof(string), typeof(ConsoleLib.Console.IRenderable), typeof(string),
                        typeof(List<string>), typeof(bool), typeof(bool), typeof(bool) })]
static class ConversationPopupHook
{
    static bool Prefix(ref int __result, string Title, string Intro, List<string> Options)
    {
        try
        {
            var options = Options == null ? new string[0] : Options.ToArray();
            QudGymBridge.Note("conversation popup '" + (Title ?? "") + "' options=" + options.Length);
            foreach (var o in options)
                QudGymBridge.Note("  option: " + o);
            int chosen = QudGymBridge.ConversationTurn(options, 120000);
            __result = chosen;
            return false;
        }
        catch (System.Exception ex)
        {
            QudGymBridge.Note("conversation popup hook failed " + ex.GetBaseException().Message);
            __result = -1;
            return true;
        }
    }
}
