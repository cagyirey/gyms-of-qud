using System;
using System.Collections.Generic;
using System.Threading;
using Newtonsoft.Json;
using UnityEngine;
using XRL;

// This phase is deliberately startup-only and read-only. It does not load a
// game, inspect a player/world, read or write a save, inject input, install
// a Harmony patch, open a socket, or start a server.
[HasModSensitiveStaticCache]
public static class QudGymCompatibilityDiagnostic
{
    [ModSensitiveCacheInit]
    public static void ReportAfterCacheReset()
    {
        try
        {
            var payload = new Dictionary<string, object>
            {
                ["schema_version"] = "qudgym-compat/1",
                ["event"] = "cache-reset",
                ["game_build"] = BuildVersion(),
                ["marketing_version"] = MarketingVersion(),
                ["core_version"] = CoreVersion(),
                ["mod_initialized"] = ModManager.Initialized,
                ["thread_id"] = Thread.CurrentThread.ManagedThreadId,
                ["core_thread_id"] = CoreThreadId(),
                ["is_core_thread"] = XRL.Core.XRLCore.IsCoreThread,
                ["active_mods"] = ActiveMods(),
                ["diagnostic_mod"] = DiagnosticMod(),
                ["hooks"] = HookEvidence()
            };
            // Keep the first diagnostic phase in the game log only. The user
            // reviews/copies the redacted line; the mod never writes a file.
            Debug.Log("QudGym compatibility evidence " + JsonConvert.SerializeObject(payload));
        }
        catch (Exception ex)
        {
            // Exception messages can contain private paths. Keep the failure
            // observable without copying that message into telemetry.
            Debug.Log("QudGym compatibility evidence failed=" + ex.GetType().Name);
        }
    }

    private static string BuildVersion()
    {
        try
        {
            return ModManager.CoreVersion.ToString();
        }
        catch (Exception ex)
        {
            return "error:" + ex.GetType().Name;
        }
    }

    private static string MarketingVersion()
    {
        try
        {
            return ModManager.MarketingVersion.ToString();
        }
        catch (Exception ex)
        {
            return "error:" + ex.GetType().Name;
        }
    }

    private static string CoreVersion()
    {
        try
        {
            return ModManager.CoreVersion.ToString();
        }
        catch (Exception ex)
        {
            return "error:" + ex.GetType().Name;
        }
    }

    private static int? CoreThreadId()
    {
        Thread core = XRL.Core.XRLCore.CoreThread;
        return core == null ? (int?)null : core.ManagedThreadId;
    }

    private static List<Dictionary<string, object>> ActiveMods()
    {
        var result = new List<Dictionary<string, object>>();
        foreach (ModInfo mod in ModManager.ActiveMods)
        {
            if (mod == null)
                continue;
            result.Add(new Dictionary<string, object>
            {
                ["mod_id"] = mod.ID ?? "",
                ["load_order"] = mod.LoadOrder,
                ["active"] = mod.Active,
                ["enabled"] = mod.IsEnabled,
                ["state"] = mod.State.ToString()
            });
        }
        return result;
    }

    private static Dictionary<string, object> DiagnosticMod()
    {
        ModInfo mod = ModManager.GetMod(typeof(QudGymCompatibilityDiagnostic).Assembly);
        if (mod == null)
            return null;
        return new Dictionary<string, object>
        {
            ["mod_id"] = mod.ID ?? "",
            ["load_order"] = mod.LoadOrder,
            ["active"] = mod.Active,
            ["enabled"] = mod.IsEnabled,
            ["state"] = mod.State.ToString()
        };
    }

    private static List<Dictionary<string, object>> HookEvidence()
    {
        string[] names =
        {
            "XRL.IPlayerMutator",
            "XRL.World.BeginTakeActionEvent",
            "XRL.World.EndTurnEvent",
            "XRL.HasCallAfterGameLoadedAttribute",
            "XRL.CallAfterGameLoadedAttribute",
            "XRL.ModSensitiveCacheInitAttribute"
        };
        var result = new List<Dictionary<string, object>>();
        foreach (string name in names)
        {
            Type resolved = null;
            try
            {
                // Cache=false keeps this observation from changing the
                // manager's type-resolution cache.
                resolved = ModManager.ResolveType(name, false, false, false);
            }
            catch (Exception)
            {
                resolved = null;
            }
            result.Add(new Dictionary<string, object>
            {
                ["name"] = name,
                ["available"] = resolved != null,
                ["thread_id"] = resolved == null
                    ? null
                    : (object)Thread.CurrentThread.ManagedThreadId,
                ["detail"] = "type-resolution-only"
            });
        }
        return result;
    }
}
