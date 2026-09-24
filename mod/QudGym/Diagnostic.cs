using System;
using System.Collections.Generic;
using System.IO;
using System.Threading;
using UnityEngine;
using XRL;

// Runs when scripting mods are compiled, which is before a character exists.
// Records the thread and which hook types this build actually has. Does not mutate a save.
[HasModSensitiveStaticCache]
public static class QudGymDiagnostic
{
    static readonly string[] Hooks =
    {
        "XRL.IPlayerMutator",
        "XRL.PlayerMutator",
        "XRL.World.IPart",
        "XRL.World.BeginTakeActionEvent",
        "XRL.World.EndTurnEvent",
        "XRL.HasCallAfterGameLoadedAttribute"
    };

    [ModSensitiveCacheInit]
    public static void Report()
    {
        QudGymBridge.Startup();
        string version = "unknown";
        try
        {
            version = ModManager.CoreVersion.ToString();
        }
        catch (Exception ex)
        {
            version = "error:" + ex.GetType().Name;
        }
        var found = new List<string>();
        foreach (string name in Hooks)
        {
            Type resolved = null;
            try
            {
                resolved = ModManager.ResolveType(name, false, false, true);
            }
            catch (Exception)
            {
                resolved = null;
            }
            found.Add(name + "=" + (resolved != null));
        }
        QudGymBridge.Listen(version);
        string line = "QudGym diagnostic version=" + version
            + " thread=" + Thread.CurrentThread.ManagedThreadId
            + " " + string.Join(" ", found);
        Debug.Log(line);
        try
        {
            File.AppendAllText(Path.Combine(Application.persistentDataPath, "QudGym-diagnostic.txt"), line + "\n");
        }
        catch (Exception ex)
        {
            Debug.Log("QudGym diagnostic file write failed: " + ex.Message);
        }
    }
}
