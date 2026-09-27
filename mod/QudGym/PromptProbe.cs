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

// A notification that only asks to be dismissed does not belong in front of the
// player, and it cannot be dismissed with a synthetic key: these popups block in a
// Keyboard.getvk loop until a key arrives, and TutorialManager.AllowPushKey
// returns false whenever ShowingPopup, so Keyboard.PushKey is refused for exactly
// as long as the dialog is up. The refusal is silent, which is why pushing space
// against a live notification looked like a dead action rather than a refused one.
//
// So the game's own escape hatch is used instead of rebuilding one. Popup has a
// public static Suppress, and every blocking popup in this family already honours
// it identically:
//
//     if (Suppress) { if (LogMessage) MessageQueue.AddPlayerMessage(Message); return Keys.Space; }
//
// That is the desired behaviour, already written by the game: the text is logged
// through the game's own path, with its own wording, capitalisation and markup
// handling, and nothing waits. Raising the flag around the call reuses that rather
// than reimplementing the logging here, so there is one mechanism and no second
// copy of the message format to drift.
//
// The flag is restored in a Finalizer rather than a Postfix because a Postfix is
// skipped when the original throws, which would leave notifications suppressed for
// the rest of the session.
static class PopupSuppress
{
    internal static void Raise(ref bool __state)
    {
        __state = XRL.UI.Popup.Suppress;
        XRL.UI.Popup.Suppress = true;
    }

    internal static void Restore(bool __state)
    {
        XRL.UI.Popup.Suppress = __state;
    }
}

[HarmonyPatch(typeof(XRL.UI.Popup), nameof(XRL.UI.Popup.ShowSpace),
    new System.Type[] { typeof(string), typeof(string), typeof(string),
                        typeof(ConsoleLib.Console.Renderable), typeof(bool),
                        typeof(bool), typeof(string) })]
static class NotificationSpaceGate
{
    static void Prefix(ref bool __state) { PopupSuppress.Raise(ref __state); }
    static void Finalizer(bool __state) { PopupSuppress.Restore(__state); }
}

[HarmonyPatch(typeof(XRL.UI.Popup), nameof(XRL.UI.Popup.ShowBlockSpace),
    new System.Type[] { typeof(string), typeof(string), typeof(bool), typeof(bool),
                        typeof(ConsoleLib.Console.IRenderable), typeof(bool),
                        typeof(bool), typeof(bool) })]
static class NotificationBlockSpaceGate
{
    static void Prefix(ref bool __state) { PopupSuppress.Raise(ref __state); }
    static void Finalizer(bool __state) { PopupSuppress.Restore(__state); }
}

// Quest notices are the case that actually blocked: accepting Mehmet's quest put
// up "You have received a new quest, What's Eating the Watervine?!" waiting for
// space, with nothing to decide.
//
// The gate is on the five named Quest methods, not on Popup. Popup.Show is void
// with 779 callers and is also how Wishing presents a maze choice, and
// Popup.ShowBlock is how the Wishing well asks which maze to build; suppressing
// either would silently remove a decision from the game. Popup.ShowBlock also
// carries 14 callers, and at least one is a real choice, so a gate there would have
// broken the Wishing well to fix a quest notice. One named method per notice, and
// every popup that genuinely needs an answer is left alone.
//
// ShowFinishStepPopup and ShowFailStepPopup take a QuestStep and announce step
// completion and failure the same way, so all five state changes are covered.
[HarmonyPatch(typeof(XRL.World.Quest), nameof(XRL.World.Quest.ShowStartPopup))]
static class QuestStartNoticeGate
{
    static void Prefix(ref bool __state) { PopupSuppress.Raise(ref __state); }
    static void Finalizer(bool __state) { PopupSuppress.Restore(__state); }
}

[HarmonyPatch(typeof(XRL.World.Quest), nameof(XRL.World.Quest.ShowFailPopup))]
static class QuestFailNoticeGate
{
    static void Prefix(ref bool __state) { PopupSuppress.Raise(ref __state); }
    static void Finalizer(bool __state) { PopupSuppress.Restore(__state); }
}

[HarmonyPatch(typeof(XRL.World.Quest), nameof(XRL.World.Quest.ShowFailStepPopup))]
static class QuestFailStepNoticeGate
{
    static void Prefix(ref bool __state) { PopupSuppress.Raise(ref __state); }
    static void Finalizer(bool __state) { PopupSuppress.Restore(__state); }
}

[HarmonyPatch(typeof(XRL.World.Quest), nameof(XRL.World.Quest.ShowFinishPopup))]
static class QuestFinishNoticeGate
{
    static void Prefix(ref bool __state) { PopupSuppress.Raise(ref __state); }
    static void Finalizer(bool __state) { PopupSuppress.Restore(__state); }
}

[HarmonyPatch(typeof(XRL.World.Quest), nameof(XRL.World.Quest.ShowFinishStepPopup))]
static class QuestFinishStepNoticeGate
{
    static void Prefix(ref bool __state) { PopupSuppress.Raise(ref __state); }
    static void Finalizer(bool __state) { PopupSuppress.Restore(__state); }
}
