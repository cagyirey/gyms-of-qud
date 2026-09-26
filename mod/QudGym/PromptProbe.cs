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
        static void Prefix(string title, XRL.World.GameObject actor, string message, List<string> options)
        {
            Record("conversation:" + (title ?? "?"), Thread.CurrentThread, options);
        }
    }

    [HarmonyPatch(typeof(XRL.UI.Popup), nameof(XRL.UI.Popup.ShowYesNo),
        new System.Type[] { typeof(string), typeof(string), typeof(bool), typeof(XRL.UI.DialogResult),
                            typeof(System.Action<XRL.UI.DialogResult>) })]
    static class ShowYesNoHook
    {
        static void Prefix(string title, string message)
        {
            Record("yesno:" + (title ?? "?"), Thread.CurrentThread, null);
        }
    }
}
