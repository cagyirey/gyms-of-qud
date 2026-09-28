using System;
using System.IO;
using System.Linq;
using System.Reflection;
using UnityEngine;

// The game only reflects types in the assembly it compiles from .cs files.
// The probe itself lives in the F# library next to this mod.
// Whether the game will accept a given choice, read from the game itself.
//
// A conversation marks a choice it will not accept by rendering that choice in
// colour K. Choice.GetTextColor returns "G", or "g" once the choice has already
// been taken, and any IConversationPart can override it with a ColorTextEvent.
// Across the game's own parts that override is only ever used to say "blocked":
//
//     RequireReputation  -> "K" unless PlayerReputation meets the requirement
//     IWaterRitualPart   -> "K" when !Affordable || !Available
//     SecretHandler      -> "K" when a required note is not in the journal
//
// So K is the whole of the game's vocabulary for a choice it will refuse, and
// G/g/M mean only fresh, already-taken, and secret-revealed respectively.
//
// The colour is read from the tag the game already put at the front of the string.
// IConversationElement.GetDisplayText(WithColor: true) builds "{{<color>|text}}" by
// prepending, so the colour is the first tag and the first '|' closes it. That is
// reading the game's own verdict rather than matching its wording, which is the
// only thing that survives the wording changing between runs -- the same node has
// been offered as "I'm looking for work.", "Do you have work that needs doing?"
// and "My services are available if you have work to offer."
//
// Only the first tag is read, because a choice's trailing tag is styled
// independently. "Live and drink. {{K|[End]}}" is the game's own exit and it is
// selectable, so searching the whole string for K would refuse a working choice.
static class ChoiceAvailability
{
    /// The game's own name for an object, with console markup removed.
    ///
    /// ConsoleLib's own strip, not a regex: the text arrives as {{G|like this}} and
    /// anything hand-rolled here would have to learn the markup language in order
    /// to unlearn it again. `Strip(string)` is the overload that takes and returns a
    /// plain string, so it is the one that can be reflected over -- the
    /// ReadOnlySpan overloads cannot.
    public static string StripMarkup(string text)
    {
        if (string.IsNullOrEmpty(text))
            return text;
        try
        {
            var markup = AppDomain.CurrentDomain.GetAssemblies()
                .FirstOrDefault(a => a.GetName().Name == "Assembly-CSharp")?
                .GetType("ConsoleLib.Console.Markup", false);
            var strip = markup?.GetMethod("Strip", BindingFlags.Public | BindingFlags.Static,
                                          null, new[] { typeof(string) }, null);
            if (strip == null)
                return text;
            return strip.Invoke(null, new object[] { text }) as string ?? text;
        }
        catch (Exception)
        {
            // A name the strip cannot read is still better than no name.
            return text;
        }
    }

    /// <summary>Per option: true when the game will accept it.</summary>
    public static bool[] From(string[] options)
    {
        if (options == null)
            return null;
        var acceptable = new bool[options.Length];
        for (int i = 0; i < options.Length; i++)
            acceptable[i] = Accepts(options[i]);
        return acceptable;
    }

    public static bool Accepts(string option)
    {
        if (string.IsNullOrEmpty(option) || !option.StartsWith("{{", StringComparison.Ordinal))
            return true;
        int bar = option.IndexOf('|');
        // "{{" with no '|' is not a tag the game wrote, so there is no verdict to
        // read. Treating it as acceptable keeps an unreadable option answerable
        // rather than silently removing a choice the player can see.
        return bar < 0 || option.Substring(2, bar - 2) != "K";
    }

    /// <summary>How many of the offered options the game will refuse.</summary>
    public static string Blocked(bool[] acceptable)
    {
        if (acceptable == null)
            return "?";
        int blocked = 0;
        for (int i = 0; i < acceptable.Length; i++)
        {
            if (!acceptable[i])
                blocked++;
        }
        return blocked.ToString();
    }
}

static class QudGymBridge
{
    static MethodInfo record;
    static MethodInfo noteActor;
    static MethodInfo embark;
    static MethodInfo prepareEarly;
    static MethodInfo boot;
    static MethodInfo describeScreen;
    static MethodInfo describeParts;
    // The conversation step, implemented in F#: it publishes the options the
    // game is offering as a decision boundary and waits for the answer.
    static MethodInfo conversationTurn;
    static MethodInfo allowPopup;
    static MethodInfo listen;
    static MethodInfo supply;
    static bool ready;

    static void Ensure()
    {
        if (ready)
            return;
        ready = true;
        try
        {
            string dir = Path.Combine(Application.persistentDataPath, "Mods", "QudGym", "lib");
            Assembly impl = Assembly.LoadFrom(Path.Combine(dir, "QudGym.Impl.dll"));
            Type probe = impl.GetType("QudGym.Probe");
            record = probe.GetMethod("record");
            noteActor = probe.GetMethod("noteActor");
            Type embarkType = impl.GetType("QudGym.Embark");
            embark = embarkType.GetMethod("start");
            prepareEarly = embarkType.GetMethod("prepareEarly");
            boot = embarkType.GetMethod("boot");
            describeScreen = embarkType.GetMethod("describeScreen");
            describeParts = embarkType.GetMethod("describeParts");
            allowPopup = embarkType.GetMethod("allowPopup");
            Type session = impl.GetType("QudGym.Session");
            listen = session.GetMethod("listen");
            supply = session.GetMethod("supply");
            conversationTurn = session.GetMethod("conversationTurn");
        }
        catch (Exception ex)
        {
            Debug.Log("QudGym fsharp load failed: " + ex.Message);
        }
    }

    static void Write(string what)
    {
        Ensure();
        if (record == null)
            return;
        try
        {
            string path = Path.Combine(Application.persistentDataPath, "QudGym-diagnostic.txt");
            object line = record.Invoke(null, new object[] { path, what });
            Debug.Log(line as string ?? "QudGym fsharp recorded");
        }
        catch (Exception ex)
        {
            Debug.Log("QudGym fsharp call failed: " + ex.GetBaseException().Message);
        }
    }

    public static void Startup()
    {
        Write("startup");
    }

    public static void Listen(string gameBuild)
    {
        Ensure();
        if (listen == null)
        {
            Write("control missing");
            return;
        }
        try
        {
            if (describeScreen != null)
                describeScreen.Invoke(null, new object[] { Path.Combine(Application.persistentDataPath, "QudGym-diagnostic.txt") });
        }
        catch (Exception ex)
        {
            Debug.Log("QudGym screen probe failed: " + ex.GetBaseException().Message);
        }
        try
        {
            string root = Application.persistentDataPath;
            listen.Invoke(null, new object[]
            {
                Path.Combine(root, "QudGym-control.txt"),
                Path.Combine(root, "QudGym-diagnostic.txt"),
                gameBuild ?? "unknown"
            });
        }
        catch (Exception ex)
        {
            Debug.Log("QudGym control failed: " + ex.GetBaseException().Message);
            Write("control failed " + ex.GetBaseException().GetType().Name);
        }
    }

    // Called on the game thread from Keyboard.IdleWait. Blocks until an agent
    // steps, then queues that command for the turn loop to consume.
    private static bool partsDumped;

    public static bool SupplyCommand()
    {
        Ensure();
        if (!partsDumped && describeParts != null)
        {
            try
            {
                partsDumped = true;
                describeParts.Invoke(null, new object[] { XRL.The.Player });
            }
            catch (Exception ex)
            {
                Debug.Log("QudGym parts dump failed: " + ex.GetBaseException().Message);
            }
        }
        if (supply == null)
            return false;
        try
        {
            XRL.World.GameObject player = XRL.The.Player;
            if (player == null || player.CurrentCell == null)
                return false;
            int turn = 0;
            XRL.XRLGame game = XRL.The.Game;
            if (game != null && game.Turns > 0 && game.Turns < int.MaxValue)
                turn = (int)game.Turns;
            object command = supply.Invoke(null, new object[] { player, turn });
            if (command == null)
                return false;
            // The F# side resolves an action id to one of the game's own Cmd*
            // verbs plus its argument, so movement, interaction and pickup go
            // through exactly the path a keypress takes. A bare string is still
            // accepted for verbs that take no argument.
            string name = command as string;
            if (name != null)
            {
                if (name.Length == 0)
                    return false;
                ConsoleLib.Console.Keyboard.PushCommand(name, null);
                return true;
            }
            if (command is ValueTuple<string, object> pair)
            {
                if (string.IsNullOrEmpty(pair.Item1))
                    return false;
                ConsoleLib.Console.Keyboard.PushCommand(pair.Item1, pair.Item2);
                return true;
            }
            // Report a shape we do not recognise. Silently returning false here
            // is what hid the tuple mismatch: F# boxed a System.Tuple, this
            // tested for a ValueTuple, every argument-bearing command was
            // dropped, and the harness counters had already advanced, so the
            // reply read as a success that never reached the game.
            Note("supply command shape " + command.GetType().FullName + " dropped");
            return false;
        }
        catch (Exception ex)
        {
            Debug.Log("QudGym supply failed: " + ex.GetBaseException().Message);
            return false;
        }
    }

    /// Run one step of a conversation in place of the game's own key loop.
    ///
    /// Returns the chosen option index, or -1 to let the game read input itself.
    /// Called on the game turn thread, which is exactly where the game's own
    /// conversation loop runs; the loop blocks there for a human's keys, and this
    /// is the same block, except the options are published and the answer comes
    /// back over the transport.
    ///
    /// `acceptable` is the game's own verdict per option, from ChoiceAvailability.
    /// It travels with the options rather than being recomputed downstream because
    /// it is a fact about the game, and the F# side should not have to know that
    /// the game spells "blocked" as a colour.
    public static int ConversationTurn(string[] options, bool[] acceptable, int timeoutMilliseconds)
    {
        Ensure();
        if (conversationTurn == null)
            return -1;
        try
        {
            var player = XRL.The.Player;
            if (player == null)
                return -1;
            int turn = 0;
            var game = XRL.The.Game;
            if (game != null && game.Turns > 0 && game.Turns < int.MaxValue)
                turn = (int)game.Turns;
            // A conversation has no title and cannot be cancelled, and -1 is the
            // answer when nobody replies, which is what this always did.
            return (int)conversationTurn.Invoke(
                null,
                new object[] { player, turn, timeoutMilliseconds, "", "", false, -1, options, acceptable });
        }
        catch (Exception ex)
        {
            Note("conversation turn failed " + ex.GetBaseException().Message);
            return -1;
        }
    }

    /// Publish a PickOption menu and wait for the choice. Returns the chosen index,
    /// or -1 if the caller cancelled and the game permitted it.
    ///
    /// The same turn mechanism as a conversation, because a menu is the same shape
    /// of question: the game builds a list, blocks until something picks, then acts
    /// on the index. The difference is only that a menu has a title and may allow
    /// the player to walk away, so both are passed through rather than assumed.
    public static int MenuTurn(string title, string intro, string[] options,
                               bool allowEscape, int defaultSelected, int timeoutMilliseconds)
    {
        Ensure();
        if (conversationTurn == null)
            return defaultSelected;
        try
        {
            var player = XRL.The.Player;
            if (player == null)
                return defaultSelected;
            int turn = 0;
            var game = XRL.The.Game;
            if (game != null && game.Turns > 0 && game.Turns < int.MaxValue)
                turn = (int)game.Turns;
            // The fallback is the game's own default selection, not -1: -1 is not a
            // legal answer to a menu that forbids escape, so a timeout must not
            // invent one.
            return (int)conversationTurn.Invoke(
                null,
                new object[]
                {
                    player, turn, timeoutMilliseconds, title ?? "", intro ?? "",
                    allowEscape, defaultSelected, options ?? new string[0],
                    ChoiceAvailability.From(options)
                });
        }
        catch (Exception ex)
        {
            Note("menu turn failed " + ex.GetBaseException().Message);
            return defaultSelected;
        }
    }

    /// Put a line into the game's own message log.
    ///
    /// Goes through the game's logging path rather than a private buffer, so a
    /// line added here is a line the player would have seen -- it reaches the
    /// console, the history command and the mod's own log subscription alike.
    public static void LogMessage(string text)
    {
        try
        {
            if (string.IsNullOrEmpty(text))
                return;
            XRL.Core.XRLCore.CallNewMessageLogEntryCallbacks(text);
        }
        catch (Exception ex)
        {
            Note("log message failed " + ex.GetBaseException().Message);
        }
    }

    public static void Note(string what)
    {
        Write(what);
    }

    public static void NoteActor(string what, object actor)
    {
        Ensure();
        if (noteActor == null)
        {
            Write(what + " actor=unlogged");
            return;
        }
        try
        {
            string path = Path.Combine(Application.persistentDataPath, "QudGym-diagnostic.txt");
            object line = noteActor.Invoke(null, new object[] { path, what, actor });
            Debug.Log(line as string ?? "QudGym fsharp recorded");
        }
        catch (Exception ex)
        {
            Debug.Log("QudGym fsharp call failed: " + ex.GetBaseException().Message);
        }
    }

    // False while the F# boot is inside bootGame, so the embark acknowledgement
    // cannot block the game thread. Popups are allowed again before the turn loop.
    public static bool AllowPopup()
    {
        try
        {
            Ensure();
            if (allowPopup == null)
                return true;
            object value = allowPopup.Invoke(null, null);
            if (value is bool)
                return (bool)value;
            return true;
        }
        catch (Exception)
        {
            return true;
        }
    }

    public static void Embark()
    {
        Ensure();
        if (embark == null)
        {
            Write("embark missing");
            return;
        }
        try
        {
            string path = Path.Combine(Application.persistentDataPath, "QudGym-diagnostic.txt");
            embark.Invoke(null, new object[] { path });
        }
        catch (Exception ex)
        {
            Debug.Log("QudGym embark failed: " + ex.GetBaseException().Message);
            Write("embark invoke failed " + ex.GetBaseException().GetType().Name);
        }
    }

    // Invoked from the game's after-game-loaded callback, on the core thread
    // with the UI context live. This is the only safe point to boot a build
    // sheet programmatically.
    public static void AfterGameLoaded()
    {
        Write("after game loaded");
        Embark();
    }

    // Runs on the core thread at menu-up. The UI context is pumping here, so
    // the EmbarkBuilder AddComponent hop completes. Doing it later, from inside
    // the game's own boot, deadlocks against the main thread.
    public static void PrepareEarly()
    {
        Ensure();
        if (prepareEarly == null)
            return;
        try
        {
            string path = Path.Combine(Application.persistentDataPath, "QudGym-diagnostic.txt");
            prepareEarly.Invoke(null, new object[] { path });
        }
        catch (Exception ex)
        {
            Write("prepare early failed " + ex.GetBaseException().GetType().Name);
        }
    }

    // Runs at "Starting Game...". Uses the prepared builder; no UI hop here.
    public static void Boot()
    {
        Ensure();
        if (boot == null)
        {
            Write("boot missing");
            return;
        }
        try
        {
            string path = Path.Combine(Application.persistentDataPath, "QudGym-diagnostic.txt");
            boot.Invoke(null, new object[] { path });
        }
        catch (Exception ex)
        {
            Write("boot invoke failed " + ex.GetBaseException().GetType().Name);
        }
    }

    public static void Mutate(XRL.World.GameObject player)
    {
        Write("mutate player=" + (player == null ? "null" : "present"));
    }
}
