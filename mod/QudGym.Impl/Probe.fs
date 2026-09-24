namespace QudGym

open System
open System.IO
open System.Reflection
open System.Threading

module Probe =
    let private seenLock = obj ()
    let mutable private seen = 0

    let record (path: string) (what: string) =
        let thread = Thread.CurrentThread
        let name = if String.IsNullOrEmpty thread.Name then "-" else thread.Name
        let line = sprintf "QudGym fsharp %s thread=%d name=%s" what thread.ManagedThreadId name
        File.AppendAllText(path, line + "\n")
        line

    // IsPlayer is XRL.World.GameObject.IsPlayer on this build. The actor is passed
    // as obj so this library does not reference the game assembly.
    let noteActor (path: string) (eventName: string) (actor: obj) =
        let n = lock seenLock (fun () -> seen <- seen + 1; seen)
        if n > 8 then
            ""
        else
            let who =
                if isNull actor then
                    "null"
                else
                    try
                        let flags = BindingFlags.Instance ||| BindingFlags.Public ||| BindingFlags.NonPublic
                        let method = actor.GetType().GetMethod("IsPlayer", flags, null, Type.EmptyTypes, null)
                        if isNull method then "unknown"
                        elif method.Invoke(actor, null) :?> bool then "player"
                        else "other"
                    with _ ->
                        "error"
            record path (sprintf "%s n=%d actor=%s" eventName n who)
