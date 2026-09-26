namespace QudGym

open System
open System.IO
open System.Reflection
open System.Threading
open System.Threading.Tasks

// Boots one BuildLibrary sheet without the character-creation UI.
// Called from the game thread once the menu is up. AddComponent has to hop
// to the UI context; bootGame stays here, which is where XRLCore.NewGame runs it.
module Embark =
    let private gate = obj ()
    let mutable private started = false
    let mutable private suppressPopups = false
    // Prepared during the early phase, while the UI context is live and the
    // core thread is not yet inside the game's boot.
    let mutable private prepared : (obj * obj) option = None
    // prepare() has no path parameter, but the builder fallback wants to log.
    let mutable private logPath = ""

    let allowPopup () = not suppressPopups

    let private flags =
        BindingFlags.Public ||| BindingFlags.NonPublic ||| BindingFlags.Instance
        ||| BindingFlags.Static ||| BindingFlags.FlattenHierarchy

    let private gameAssembly =
        lazy
            (AppDomain.CurrentDomain.GetAssemblies()
             |> Array.find (fun a -> a.GetName().Name = "Assembly-CSharp"))

    let private ty name =
        let found = gameAssembly.Value.GetType(name, false)
        if isNull found then failwith ("missing type " + name) else found

    let private valueOf (t: Type) (target: obj) name =
        match t.GetProperty(name, flags) with
        | null ->
            match t.GetField(name, flags) with
            | null -> failwith (t.Name + " has no member " + name)
            | field -> field.GetValue(target)
        | prop -> prop.GetValue(target)

    let private staticValue name t = valueOf t null name
    let private instanceValue name (target: obj) = valueOf (target.GetType()) target name

    let private setValue (t: Type) (target: obj) name value =
        match t.GetProperty(name, flags) with
        | null ->
            match t.GetField(name, flags) with
            | null -> failwith (t.Name + " has no settable " + name)
            | field -> field.SetValue(target, value)
        | prop -> prop.SetValue(target, value)

    let private invokeMethod (t: Type) (target: obj) name (args: obj[]) =
        let matches =
            t.GetMethods(flags)
            |> Array.filter (fun m ->
                m.Name = name
                && m.IsStatic = isNull target
                && m.GetParameters().Length = args.Length)
        if matches.Length = 0 then
            failwith (t.Name + " has no " + name + "/" + string args.Length)
        let callable =
            matches
            |> Array.tryFind (fun m ->
                m.GetParameters()
                |> Array.mapi (fun i p ->
                    isNull args.[i] || p.ParameterType.IsAssignableFrom(args.[i].GetType()))
                |> Array.forall id)
            |> Option.defaultValue matches.[0]
        callable.Invoke(target, args)

    let private invoke name args (target: obj) =
        invokeMethod (target.GetType()) target name args

    let private invokeStatic name args t =
        invokeMethod t null name args

    let private describe (ex: exn) =
        let rec inner (e: exn) =
            match e with
            | :? TargetInvocationException as wrapped when not (isNull wrapped.InnerException) ->
                inner wrapped.InnerException
            | :? AggregateException as wrapped when wrapped.InnerExceptions.Count > 0 ->
                inner wrapped.InnerExceptions.[0]
            | _ -> e
        let e = inner ex
        let frame =
            if isNull e.StackTrace || e.StackTrace.Length = 0 then ""
            else " @ " + e.StackTrace.Split('\n').[0].Trim()
        e.GetType().Name + ": " + e.Message + frame

    let private onUi work =
        let manager = staticValue "Instance" (ty "GameManager")
        match instanceValue "uiSynchronizationContext" manager with
        | :? SynchronizationContext as context ->
            // The Unity main thread may still be streaming blueprints when this
            // first fires, in which case the posted work is simply not pumped
            // yet. Wait in bounded attempts rather than failing the episode on
            // the first miss, and never hold the core thread indefinitely.
            let mutable attempt = 0
            let mutable failure : exn option = None
            let mutable value : 'a option = None
            while attempt < 3 && failure.IsNone && value.IsNone do
                attempt <- attempt + 1
                let finished = TaskCompletionSource<'a>()
                try
                    context.Post(
                        SendOrPostCallback(fun _ ->
                            try finished.TrySetResult(work ()) |> ignore
                            with ex -> finished.TrySetException(ex) |> ignore),
                        null)
                    if finished.Task.Wait(20000) then
                        try value <- Some(finished.Task.GetAwaiter().GetResult())
                        with ex -> failure <- Some ex
                    // The caller records the eventual failure with the episode
                    // path; Embark has no log path of its own here.
                with ex ->
                    failure <- Some ex
            match failure, value with
            | Some ex, _ -> raise ex
            | _, Some v -> v
            | _ -> failwith "ui hop timed out"
        | _ -> failwith "ui synchronization context missing"

    let private loadoutJson () =
        let asm = Assembly.GetExecutingAssembly()
        use stream = asm.GetManifestResourceStream("artifex.json")
        if isNull stream then failwith "embedded artifex.json missing"
        use reader = new StreamReader(stream)
        reader.ReadToEnd()

    let private prepare code =
        let coreType = ty "XRL.Core.XRLCore"
        let core = staticValue "Core" coreType
        match instanceValue "Game" core with
        | null -> ()
        | existing -> invoke "Release" [||] existing |> ignore
        invoke "LoadEverything" [||] core |> ignore
        invoke "ResetGameBasedStaticCaches" [||] core |> ignore
        let gameType = ty "XRL.XRLGame"
        let game =
            Activator.CreateInstance(
                gameType,
                [| staticValue "_Console" coreType; staticValue "_Buffer" coreType |])
        setValue coreType core "Game" game
        invoke "CreateNewGame" [||] game |> ignore
        invoke "Reset" [||] core |> ignore
        let manager = staticValue "Instance" (ty "GameManager")
        match instanceValue "gameQueue" manager with
        | null -> ()
        | queue ->
            try invoke "clear" [||] queue |> ignore
            with _ ->
                try invoke "Clear" [||] queue |> ignore
                with _ -> ()
        // Prefer a builder that needs no Unity main-thread work.
        //
        // The onUi hop is the sole cause of the intermittent embark: posting to
        // GameManager.Instance.uiSynchronizationContext from inside the game's
        // own boot races the main thread, and the turn thread then waits on a
        // context nobody is pumping. EmbarkBuilder is a MonoBehaviour, but
        // InitModulesFromCode only writes module data, so a detached instance is
        // tried first and the hop is kept strictly as a fallback.
        let builderType = ty "XRL.CharacterBuilds.EmbarkBuilder"
        let mutable builder = null
        try
            let detached = Activator.CreateInstance(builderType)
            invoke "InitModulesFromCode" [| box code; box true |] detached |> ignore
            builder <- detached
        with ex ->
            if logPath <> "" then
                Probe.record logPath ("builder detached path failed: " + ex.GetBaseException().Message) |> ignore
        let builder =
            if isNull builder then
                onUi (fun () ->
                    let host = instanceValue "gameObject" manager
                    let created = invoke "AddComponent" [| box builderType |] host
                    invoke "InitModulesFromCode" [| box code; box true |] created |> ignore
                    created)
            else
                if logPath <> "" then
                    Probe.record logPath "builder prepared without the ui hop" |> ignore
                builder
        game, builder

    // Silent loadCode writes module data without re-running shouldBeEnabled.
    // Genotype stays off until chartype has data, and the sheet does not store
    // chartype. "New" and "Classic" are the IDs in EmbarkModules.xml.
    let private select (builder: obj) (typeName: string) (fieldName: string) (value: string) =
        let modules = valueOf (builder.GetType()) builder "modules" :?> System.Collections.IEnumerable
        for m in modules do
            if m.GetType().Name = typeName then
                let data = instanceValue "data" m
                let current =
                    if isNull data then null
                    else
                        match data.GetType().GetField(fieldName, flags) with
                        | null -> null
                        | field -> field.GetValue(data) :?> string
                if String.IsNullOrEmpty current then
                    let dataType = ty ("XRL.CharacterBuilds.Qud." + typeName + "Data")
                    let created =
                        let withValue = dataType.GetConstructor([| typeof<string> |])
                        if isNull withValue then
                            let blank = Activator.CreateInstance(dataType)
                            dataType.GetField(fieldName, flags).SetValue(blank, value)
                            blank
                        else
                            withValue.Invoke([| box value |])
                    invoke "setDataDirect" [| created |] m |> ignore

    let private enableReady (builder: obj) =
        select builder "QudGamemodeModule" "Mode" "Classic"
        select builder "QudChartypeModule" "type" "New"
        let modules = valueOf (builder.GetType()) builder "modules" :?> System.Collections.IEnumerable
        for m in modules do
            let should = invoke "shouldBeEnabled" [||] m :?> bool
            let enabled = instanceValue "enabled" m :?> bool
            if should && not enabled then
                invoke "enable" [||] m |> ignore

    let private copyIntoInfo (builder: obj) =
        let info = instanceValue "info" builder
        enableReady builder
        let modules = instanceValue "modules" info
        let dataList = instanceValue "_data" info
        let addModules = modules.GetType().GetMethod("Add")
        let addData = dataList.GetType().GetMethod("Add")
        let enabled = instanceValue "enabledModules" builder :?> System.Collections.IEnumerable
        let names = System.Collections.Generic.List<string>()
        for m in enabled do
            addModules.Invoke(modules, [| m |]) |> ignore
            names.Add(m.GetType().Name)
            match invoke "getData" [||] m with
            | null -> ()
            | data -> addData.Invoke(dataList, [| data |]) |> ignore
        info, names

    /// Runs on the core thread while the menu is up and the UI context is
    /// pumping. This is the only point where the AddComponent hop can complete:
    /// once the core thread is inside the game's own boot the main thread stops
    /// servicing the context and the hop deadlocks.
    /// Reads, live, the two XRLCore flags that look like they gate the mod
    /// new-game flow, plus whether the UI context we hop to is present at all.
    /// Metadata cannot say who sets them, so this observes them instead.
    /// Reflection-only dump of statics that could hold the live ScreenBuffer.
    /// Metadata cannot show a static field's value, so this asks at runtime.
    let private intOf (v: obj) =
        try Convert.ToInt32(v) with _ -> -1

    let describeScreen (path: string) =
        // "ConsoleLib.Console" is a namespace, not a type, which is why a direct
        // GetType on it returns null. Scan the namespace for whatever static
        // hands out the live ScreenBuffer.
        let asm = gameAssembly.Value
        let flags = BindingFlags.Static ||| BindingFlags.Public ||| BindingFlags.NonPublic
        let hits = ResizeArray<string>()
        for t in asm.GetTypes() do
            if t.Namespace = "ConsoleLib.Console" && t.Name <> "ScreenBuffer" then
                for f in t.GetFields(flags) do
                    if f.FieldType.Name = "ScreenBuffer" then
                        let v =
                            try
                                match f.GetValue(null) with
                                | null -> "null"
                                | o -> sprintf "%dx%d" (intOf (instanceValue "Width" o)) (intOf (instanceValue "Height" o))
                            with ex -> "err:" + ex.GetType().Name
                        hits.Add(sprintf "%s.%s=%s" t.Name f.Name v)
                for pr in t.GetProperties(flags) do
                    if pr.PropertyType.Name = "ScreenBuffer" then
                        let v =
                            try
                                match pr.GetValue(null, null) with
                                | null -> "null"
                                | o -> sprintf "%dx%d" (intOf (instanceValue "Width" o)) (intOf (instanceValue "Height" o))
                            with ex -> "err:" + ex.GetType().Name
                        hits.Add(sprintf "%s.%s=%s" t.Name pr.Name v)
        let names =
            asm.GetTypes()
            |> Array.filter (fun t -> t.Namespace = "ConsoleLib.Console")
            |> Array.map (fun t -> t.Name)
            |> String.concat ","
        Probe.record path (sprintf "screen probe: sbhits=[%s] types=[%s]" (String.concat ";" hits) names) |> ignore

    /// Diagnostic: part names for visible entities, so the actor predicate is
    /// chosen from what objects actually carry rather than guessed from names.
    let describeParts (player: obj) =
        if isNull player then ()
        else
            let cell = instanceValue "CurrentCell" player
            if isNull cell then ()
            else
                let zone = instanceValue "ParentZone" cell
                let x0 = intOf (instanceValue "X" cell)
                let y0 = intOf (instanceValue "Y" cell)
                let acc = ResizeArray<string>()
                for dy in -2 .. 2 do
                    for dx in -2 .. 2 do
                        let c = try invoke "GetCell" [| box (x0 + dx); box (y0 + dy) |] zone with _ -> null
                        if not (isNull c) then
                            let objs = try invoke "GetRealNonSceneryObjects" [||] c with _ -> null
                            if not (isNull objs) then
                                for o in (objs :?> Collections.IEnumerable) do
                                    if not (isNull o) then
                                        let nm =
                                            try
                                                match instanceValue "DisplayName" o with
                                                | :? string as x -> x
                                                | _ -> "?"
                                            with _ -> "?"
                                        let parts =
                                            try
                                                let pp = o.GetType().GetProperty("parts", BindingFlags.Instance ||| BindingFlags.Public ||| BindingFlags.NonPublic)
                                                if isNull pp then "?"
                                                else
                                                    match pp.GetValue(o, null) with
                                                    | null -> ""
                                                    | coll ->
                                                        (coll :?> Collections.IEnumerable)
                                                        |> Seq.cast<obj>
                                                        |> Seq.filter (fun q -> not (isNull q))
                                                        |> Seq.map (fun q -> q.GetType().Name)
                                                        |> Seq.distinct
                                                        |> String.concat ","
                                            with ex -> "err:" + ex.GetType().Name
                                        acc.Add(sprintf "%s [%+d,%+d] parts=%s" nm dx dy parts) |> ignore
                let path = Path.Combine(Path.GetTempPath(), "qudgym-parts.txt")
                File.WriteAllLines(path, acc |> Seq.truncate 16)

    let describeFlags () =
        let read name =
            try
                let t = ty "XRL.Core.XRLCore"
                let f = t.GetField(name, BindingFlags.Instance ||| BindingFlags.Static ||| BindingFlags.Public ||| BindingFlags.NonPublic)
                if isNull f then "missing"
                else
                    match f.GetValue(staticValue "Core" t) with
                    | null -> "null"
                    | v -> string v
            with ex -> "err:" + ex.GetType().Name
        let ctxState =
            try
                let manager = staticValue "Instance" (ty "GameManager")
                let c = instanceValue "uiSynchronizationContext" manager
                if isNull c then "null" else c.GetType().Name
            with ex -> "err:" + ex.GetType().Name
        sprintf "_isNewGameModFlow=%s waitForSegmentOnGameThread=%s uiCtx=%s"
            (read "_isNewGameModFlow") (read "waitForSegmentOnGameThread") ctxState

    let prepareEarly (path: string) =
        lock gate (fun () ->
            if started || prepared.IsSome then ()
            else
                match staticValue "IsCoreThread" (ty "XRL.Core.XRLCore") with
                | :? bool as onCore when onCore ->
                    started <- true
                    try
                        logPath <- path
                        Probe.record path ("embark prepare " + describeFlags ()) |> ignore
                        let json = loadoutJson ()
                        let code =
                            invokeStatic "Compress" [| box json |] (ty "XRL.CharacterBuilds.CodeCompressor")
                            :?> string
                        let game, builder = prepare code
                        prepared <- Some(game, builder)
                        Probe.record path "embark prepared" |> ignore
                    with ex ->
                        prepared <- None
                        lock gate (fun () -> started <- false)
                        Probe.record path ("embark prepare failed " + describe ex) |> ignore
                | _ -> ())

    /// Runs on the core thread at "Starting Game...". Uses the already-prepared
    /// builder, so no UI hop happens on this path at all.
    let boot (path: string) =
        let proceed =
            lock gate (fun () ->
                if started && prepared.IsSome then true
                else false)
        if not proceed then ()
        else
            try
                logPath <- path
                Probe.record path ("embark start " + describeFlags ()) |> ignore
                let game, builder = prepared.Value
                let info, names = copyIntoInfo builder
                Probe.record path ("embark modules " + String.Join(",", names)) |> ignore
                suppressPopups <- true
                try
                    Probe.record path "embark boot" |> ignore
                    invoke "bootGame" [| game |] info |> ignore
                finally
                    suppressPopups <- false
                let body =
                    match instanceValue "Player" game with
                    | null -> null
                    | player -> instanceValue "Body" player
                Probe.record path ("embark booted body=" + (if isNull body then "missing" else "present")) |> ignore
                let core = staticValue "Core" (ty "XRL.Core.XRLCore")
                Probe.record path "embark rungame" |> ignore
                invoke "RunGame" [||] core |> ignore
            with ex ->
                suppressPopups <- false
                // Release the one-shot latch. A transient UI miss must not
                // disable programmatic embark for the rest of the process.
                lock gate (fun () -> started <- false)
                Probe.record path ("embark failed " + describe ex) |> ignore

    /// Backwards-compatible entry point: prepare if needed, then boot.
    let start (path: string) =
        prepareEarly path
        boot path
