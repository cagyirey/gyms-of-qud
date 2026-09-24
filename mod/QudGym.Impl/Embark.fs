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
            let finished = TaskCompletionSource<'a>()
            context.Post(
                SendOrPostCallback(fun _ ->
                    try finished.TrySetResult(work ()) |> ignore
                    with ex -> finished.TrySetException(ex) |> ignore),
                null)
            if not (finished.Task.Wait(20000)) then failwith "ui hop timed out"
            finished.Task.GetAwaiter().GetResult()
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
        let builder =
            onUi (fun () ->
                let host = instanceValue "gameObject" manager
                let created =
                    invoke "AddComponent" [| box (ty "XRL.CharacterBuilds.EmbarkBuilder") |] host
                invoke "InitModulesFromCode" [| box code; box true |] created |> ignore
                created)
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

    let start (path: string) =
        let proceed =
            lock gate (fun () ->
                if started then false
                else
                    match staticValue "IsCoreThread" (ty "XRL.Core.XRLCore") with
                    | :? bool as onCore when onCore ->
                        started <- true
                        true
                    | _ -> false)
        if not proceed then ()
        else
            try
                Probe.record path "embark start" |> ignore
                let json = loadoutJson ()
                let code =
                    invokeStatic "Compress" [| box json |] (ty "XRL.CharacterBuilds.CodeCompressor")
                    :?> string
                let game, builder = prepare code
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
                Probe.record path ("embark failed " + describe ex) |> ignore
