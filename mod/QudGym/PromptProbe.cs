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

    // A confirmation is a question with exactly two answers, so it is answered the
    // way a question is: published, and the game's own code run on the answer.
    //
    // It was only observed, never gated, and it blocks. That put a wall in front of
    // every confirmation in the game, and the first one the quest runs into is
    // trade: TradeUI.ShowTradeScreen asks whether to hand over the water debt
    // before it will trade at all, so a trade could not begin.
    //
    // Reusing MenuTurn is the point. The escape rule, the default and the
    // publication are the same ones a menu already uses, rather than a second
    // question path that could disagree with the first about what is on offer.
    [HarmonyPatch(typeof(XRL.UI.Popup), nameof(XRL.UI.Popup.ShowYesNo),
        new System.Type[] { typeof(string), typeof(string), typeof(bool), typeof(XRL.UI.DialogResult),
                            typeof(System.Action<XRL.UI.DialogResult>) })]
    static class ShowYesNoGate
    {
        static bool Prefix(ref XRL.UI.DialogResult __result, string Message, bool AllowEscape,
                           XRL.UI.DialogResult defaultResult, System.Action<XRL.UI.DialogResult> callback)
        {
            try
            {
                var options = new[] { "Yes", "No" };
                QudGymBridge.Note("yesno '" + (Message ?? "") + "' escape=" + AllowEscape
                    + " default=" + defaultResult);
                // A menu's escape answer is -1, and the game's own escape answer is
                // its declared default. Mapping one onto the other keeps a
                // confirmation that was walked away from behaving as the game says.
                int chosen = QudGymBridge.MenuTurn("", Message ?? "", options, AllowEscape, -1, 120000);
                var result = chosen switch
                {
                    0 => XRL.UI.DialogResult.Yes,
                    1 => XRL.UI.DialogResult.No,
                    _ => defaultResult,
                };
                QudGymBridge.Note("yesno chose " + result);
                // Some callers pass a callback instead of reading the return value,
                // so skipping the original without calling it would discard their
                // result. The same reason PickOptionGate invokes OnResult.
                if (callback != null)
                    callback(result);
                __result = result;
                return false;
            }
            catch (System.Exception ex)
            {
                QudGymBridge.Note("yesno gate failed " + ex.GetBaseException().Message);
                __result = defaultResult;
                return false;
            }
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
            var acceptable = ChoiceAvailability.From(options);
            QudGymBridge.Note("conversation popup '" + (Title ?? "") + "' options=" + options.Length
                + " blocked=" + ChoiceAvailability.Blocked(acceptable));
            for (int i = 0; i < options.Length; i++)
                QudGymBridge.Note("  option: " + options[i]
                    + (acceptable[i] ? "" : "   [the game will not accept this]"));
            int chosen = QudGymBridge.ConversationTurn(options, acceptable, 120000);
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

// Popup has five methods that block waiting for a key. The two above were not
// enough, and the omission was found the way it should have been earlier: a live
// run left the embark summary sitting on screen -- "It's you." with the character's
// equipment list -- and the game stopped taking turns entirely, so every later
// action reported consumed=0.
//
// That summary is QudGameBootModule, raised through Popup.Show, which delegates to
// ShowBlock. ShowBlock honours Popup.Suppress exactly as the other four do, so the
// same gate applies and the game's own logging path records the text.
//
// These three are gated, and the list is now total: ShowBlock, ShowBlockPrompt,
// ShowBlockSpace and ShowSpace all wait, and all four are covered. ShowBlockPrompt
// and ShowBlockWithCopy take a prompt string as well, so the game's Suppress branch
// logs the message and the caller gets Keys.Space back, which is the same value
// those methods return when suppressed.
[HarmonyPatch(typeof(XRL.UI.Popup), nameof(XRL.UI.Popup.ShowBlock),
    new System.Type[] { typeof(string), typeof(string), typeof(string), typeof(bool),
                        typeof(bool), typeof(bool), typeof(bool),
                        typeof(Genkit.Location2D) })]
static class NotificationBlockGate
{
    static void Prefix(ref bool __state) { PopupSuppress.Raise(ref __state); }
    static void Finalizer(bool __state) { PopupSuppress.Restore(__state); }
}

[HarmonyPatch(typeof(XRL.UI.Popup), nameof(XRL.UI.Popup.ShowBlockPrompt),
    new System.Type[] { typeof(string), typeof(string), typeof(string),
                        typeof(ConsoleLib.Console.IRenderable), typeof(bool),
                        typeof(bool), typeof(bool), typeof(bool), typeof(bool) })]
static class NotificationBlockPromptGate
{
    static void Prefix(ref bool __state) { PopupSuppress.Raise(ref __state); }
    static void Finalizer(bool __state) { PopupSuppress.Restore(__state); }
}

// Two more that do not honour Suppress, and so cannot be covered by the global flag.
//
// The flag is the generic answer: eight of the sixteen public popups consult
// Popup.Suppress, so setting it once covers every notification the game will ever
// raise, including ones nobody has found yet. What is left is the short list of
// popups that ignore the flag, and these two appear in ordinary play -- ShowFail
// is how the game says a trade will not happen, and ShowYesNoCancel is a three-way
// confirmation. Both blocked.
[HarmonyPatch(typeof(XRL.UI.Popup), nameof(XRL.UI.Popup.ShowFail),
    new System.Type[] { typeof(string), typeof(bool), typeof(bool), typeof(bool) })]
static class ShowFailGate
{
    // ShowFail returns void and only reports that something could not be done. It
    // is logged through the game's own message path so the reason is not lost, and
    // it does not block.
    static bool Prefix(string Message, bool Capitalize)
    {
        try
        {
            var text = (Message ?? "").Trim();
            if (Capitalize && text.Length > 0)
                text = char.ToUpperInvariant(text[0]) + text.Substring(1);
            QudGymBridge.LogMessage(text);
        }
        catch (System.Exception)
        {
        }
        return false;
    }
}

[HarmonyPatch(typeof(XRL.UI.Popup), nameof(XRL.UI.Popup.ShowYesNoCancel),
    new System.Type[] { typeof(string), typeof(string), typeof(bool), typeof(XRL.UI.DialogResult) })]
static class ShowYesNoCancelGate
{
    static bool Prefix(ref XRL.UI.DialogResult __result, string Message, bool AllowEscape,
                       XRL.UI.DialogResult defaultResult)
    {
        try
        {
            var options = new[] { "Yes", "No", "Cancel" };
            QudGymBridge.Note("yesnocancel '" + (Message ?? "") + "' escape=" + AllowEscape
                + " default=" + defaultResult);
            int chosen = QudGymBridge.MenuTurn("", Message ?? "", options, AllowEscape, -1, 120000);
            var result = chosen switch
            {
                0 => XRL.UI.DialogResult.Yes,
                1 => XRL.UI.DialogResult.No,
                2 => XRL.UI.DialogResult.Cancel,
                _ => defaultResult,
            };
            QudGymBridge.Note("yesnocancel chose " + result);
            __result = result;
            return false;
        }
        catch (System.Exception ex)
        {
            QudGymBridge.Note("yesnocancel gate failed " + ex.GetBaseException().Message);
            __result = defaultResult;
            return false;
        }
    }
}

// Taking something out of a container is a question with a list of answers, and the
// game's own picker is where it is asked -- so it is answered the same way a
// conversation or a menu is.
//
// PickItem.ShowPicker is reached from four different places and only two of them
// are looting:
//
//   Container.cs:153   GetItemDialog   a chest
//   Inventory.cs:1361  GetItemDialog   taking something out of what you carry
//   Telekinesis, Polygel, FixitSpray, MagazineAmmoLoader, ...  SelectItemDialog
//                      which part of an ability to use
//
// PickItemDialogStyle is therefore the discriminator, and only the overload taking
// `ref bool RequestInterfaceExit` is gated, because that is the one both looting
// callers use. A gate that treated every ShowPicker as a container would have
// changed which ability part Telekinesis picks.
//
// Everything not GetItemDialog returns true, so the original runs untouched.
// The overload is resolved at runtime rather than named in the attribute, because
// a `ref bool` parameter cannot appear in an attribute's type list -- typeof(bool)
// .MakeByRefType() is not a constant expression. Naming the parameter types
// explicitly is also what makes the choice safe: ShowPicker has two overloads and
// the other one is every ability part selector in the game, which this must not
// touch.
[HarmonyPatch]
static class PickItemGate
{
    static System.Reflection.MethodBase TargetMethod()
    {
        var pickItem = typeof(XRL.UI.PickItem);
        foreach (var candidate in pickItem.GetMethods(
                     System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.Static))
        {
            if (candidate.Name != "ShowPicker")
                continue;
            var parameters = candidate.GetParameters();
            // Items, ref RequestInterfaceExit, CategoryPriority, Style, ...
            if (parameters.Length != 14)
                continue;
            if (parameters[0].ParameterType != typeof(System.Collections.Generic.IList<XRL.World.GameObject>))
                continue;
            if (!parameters[1].ParameterType.IsByRef)
                continue;
            if (parameters[3].ParameterType != typeof(XRL.UI.PickItem.PickItemDialogStyle))
                continue;
            return candidate;
        }
        throw new System.MissingMethodException(
            "XRL.UI.PickItem.ShowPicker(IList<GameObject>, ref bool, ...) was not found");
    }

    static bool Prefix(ref XRL.World.GameObject __result, ref bool RequestInterfaceExit,
                       System.Collections.Generic.IList<XRL.World.GameObject> Items,
                       XRL.UI.PickItem.PickItemDialogStyle Style, string Title)
    {
        // Not a container. The original owns this path.
        if (Style != XRL.UI.PickItem.PickItemDialogStyle.GetItemDialog)
            return true;
        try
        {
            if (Items == null || Items.Count == 0)
                return true;

            var objects = new XRL.World.GameObject[Items.Count];
            var options = new string[Items.Count];
            for (int i = 0; i < Items.Count; i++)
            {
                objects[i] = Items[i];
                options[i] = Describe(objects[i]);
            }
            QudGymBridge.Note("loot '" + (Title ?? "") + "' items=" + objects.Length
                + " escape=True default=0");
            for (int i = 0; i < options.Length && i < 12; i++)
                QudGymBridge.Note("  item: " + options[i]);

            // The picker allows escape in the game's own flow, so cancel is on offer
            // here too: index -1 is the game's own "took nothing".
            int chosen = QudGymBridge.MenuTurn(Title ?? "", "Take what?", options, true, 0, 120000);
            if (chosen < 0 || chosen >= objects.Length)
            {
                QudGymBridge.Note("loot nothing chosen");
                __result = null;
            }
            else
            {
                QudGymBridge.Note("loot chose " + options[chosen]);
                __result = objects[chosen];
            }
            // The caller watches this to know the picker owned the interface, so it
            // has to be set or the caller carries on as though nothing was taken.
            RequestInterfaceExit = true;
            return false;
        }
        catch (System.Exception ex)
        {
            // A gate that cannot answer must not take the original path with a
            // half-set result, so the original runs instead.
            QudGymBridge.Note("loot gate failed " + ex.GetBaseException().Message);
            return true;
        }
    }

    /// The game's own name for an object, stripped of markup.
    ///
    /// GetDisplayName is what the game calls a thing everywhere else, so using it
    /// keeps the option list the same words the player would read. The strip is
    /// ConsoleLib's own, because the name arrives as markup and a raw name would
    //// be the tag rather than the text.
    /// The game's own name for an object.
    ///
    /// GetDisplayName is what the game calls a thing everywhere else, so the option
    /// list is made of the same words the player would read. It is asked with
    /// NoColor, which is the game's own way of saying "without the markup", so the
    /// name needs no second pass to be readable.
    static string Describe(XRL.World.GameObject item)
    {
        if (item == null)
            return "(nothing)";
        try
        {
            bool adjunctNounActive;
            var name = item.GetDisplayName(out adjunctNounActive, Cutoff: int.MaxValue,
                                           NoColor: true) as string;
            if (string.IsNullOrEmpty(name))
                name = item.ToString();
            return ChoiceAvailability.StripMarkup(name) ?? name;
        }
        catch (System.Exception)
        {
            return "(unnamed)";
        }
    }
}

[HarmonyPatch(typeof(XRL.UI.Popup), nameof(XRL.UI.Popup.ShowBlockWithCopy),
    new System.Type[] { typeof(string), typeof(string), typeof(string),
                        typeof(string), typeof(bool) })]
static class NotificationBlockWithCopyGate
{
    static void Prefix(ref bool __state) { PopupSuppress.Raise(ref __state); }
    static void Finalizer(bool __state) { PopupSuppress.Restore(__state); }
}

// A menu the player has to navigate is a question the harness can answer without
// the menu being drawn.
//
// Popup.PickOption takes a list of options and returns the chosen index, which is
// the same shape as ShowConversation: the game builds the list, blocks until
// something picks, then acts on the index. Eighty call sites use it -- equipment
// and container pickers, wish outcomes, cybernetics, the Wishing well's choices --
// so one hook retires all of them. It does not delegate to ShowConversation, so the
// conversation hook does not already cover it; it builds and waits on its own.
//
// The original is skipped and the index is supplied instead, so the game still
// receives its own answer and runs its own code on it. Two details of the original
// are reproduced rather than assumed, because dropping either would be a quiet
// behavioural change in someone else's code:
//
//   * OnResult is invoked by the original with the chosen index, before returning.
//     Some callers pass a callback instead of reading the return value, so skipping
//     the original without calling it would silently discard their result.
//   * A cancelled menu returns -1, and only when the caller passed AllowEscape.
//     Cancel is therefore offered to the caller under exactly that condition, so a
//     menu that forbids escape cannot be talked out of, and the timeout fallback is
//     the game's own DefaultSelected rather than -1, which would not be a legal
//     answer to such a menu.
//
// A menu with no options is left alone: there is no question to publish, and the
// original has its own handling for it.
[HarmonyPatch(typeof(XRL.UI.Popup), nameof(XRL.UI.Popup.PickOption))]
static class PickOptionGate
{
    static bool Prefix(ref int __result, string Title, string Intro,
                       System.Collections.Generic.IReadOnlyList<string> Options,
                       System.Action<int> OnResult, int DefaultSelected, bool AllowEscape)
    {
        try
        {
            if (Options == null || Options.Count == 0)
                return true;

            var options = new string[Options.Count];
            for (int i = 0; i < Options.Count; i++)
                options[i] = Options[i] ?? "";
            int fallback = AllowEscape ? -1 : DefaultSelected;

            QudGymBridge.Note("menu '" + (Title ?? "") + "' options=" + options.Length
                + " escape=" + AllowEscape + " default=" + DefaultSelected
                + " blocked=" + ChoiceAvailability.Blocked(ChoiceAvailability.From(options)));
            for (int i = 0; i < options.Length && i < 12; i++)
                QudGymBridge.Note("  option: " + options[i]);

            int chosen = QudGymBridge.MenuTurn(Title, Intro, options, AllowEscape,
                                               DefaultSelected, 120000);
            if (chosen < -1 || chosen >= options.Length)
            {
                // An answer outside the list would be the game's own index into a
                // list it never offered. Fall back rather than pass it on.
                QudGymBridge.Note("menu answer " + chosen + " out of range; using default");
                chosen = fallback;
            }

            QudGymBridge.Note("menu chose " + chosen
                + (chosen >= 0 && chosen < options.Length ? " -> " + options[chosen] : " (cancelled)"));
            if (OnResult != null)
                OnResult(chosen);
            __result = chosen;
            return false;
        }
        catch (System.Exception ex)
        {
            QudGymBridge.Note("menu gate failed " + ex.GetBaseException().Message);
            __result = DefaultSelected;
            return true;
        }
    }
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
