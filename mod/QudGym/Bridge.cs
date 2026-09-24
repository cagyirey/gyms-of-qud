using System;
using System.IO;
using System.Reflection;
using UnityEngine;

// The game only reflects types in the assembly it compiles from .cs files.
// The probe itself lives in the F# library next to this mod.
static class QudGymBridge
{
    static MethodInfo record;
    static MethodInfo noteActor;
    static MethodInfo embark;
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
            allowPopup = embarkType.GetMethod("allowPopup");
            Type session = impl.GetType("QudGym.Session");
            listen = session.GetMethod("listen");
            supply = session.GetMethod("supply");
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
    public static bool SupplyCommand()
    {
        Ensure();
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
            string text = command as string;
            if (string.IsNullOrEmpty(text))
                return false;
            ConsoleLib.Console.Keyboard.PushCommand(text);
            return true;
        }
        catch (Exception ex)
        {
            Debug.Log("QudGym supply failed: " + ex.GetBaseException().Message);
            return false;
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

    public static void Mutate(XRL.World.GameObject player)
    {
        Write("mutate player=" + (player == null ? "null" : "present"));
    }
}
