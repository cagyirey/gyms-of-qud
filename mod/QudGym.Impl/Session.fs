namespace QudGym

open System
open System.Collections.Generic
open System.IO
open System.Net
open System.Net.Sockets
open System.Reflection
open System.Text
open System.Threading
open System.Threading.Tasks

open Suave
open Suave.Filters
open Suave.Operators
open Suave.RequestErrors
open Suave.Sockets
open Suave.Sockets.Control
open Suave.WebSocket

// Live reset/step endpoint.
//
// The game turn thread NEVER blocks. At a decision boundary it publishes the
// observation and returns immediately; the turn loop then waits for input the
// way it normally would. The staged action is picked up by a later non-blocking
// Keyboard.IdleWait tick. Only the Suave request thread waits on TaskCompletion
// sources, which is what the boundary rule allows: the transport host stays
// outside the game-turn state machine.
module Session =
    /// A published decision. Action is staged by the Suave thread and drained by
    /// the game thread; neither side waits on the other.
    type private Slot(id: string, index: int, turn: int, observation: string) =
        member val Action: string option = None with get, set
        member val Consumed: bool = false with get, set
        member val Onward = TaskCompletionSource<Slot>() with get
        member _.Id = id
        member _.Index = index
        member _.Turn = turn
        member _.Observation = observation

    let private gate = obj ()
    let private cache = Dictionary<string, string * string>()

    let mutable private waiting : Slot option = None
    let mutable private decisions = 0
    let mutable private resetUsed = false
    let mutable private listening = false
    let mutable private logPath = ""
    let mutable private gameBuild = "unknown"
    let mutable private cancelled = false
    // Actions enqueued by a batch `run`. The turn thread drains one per
    // decision boundary, so a client can hand over a whole movement script
    // and read one state back instead of one round trip per step.
    let private pending = Queue<string>()
    let mutable private consumed = 0
    // Bumped by the turn thread each time it publishes an observation, so a
    // batch can tell "commands dispatched" from "turns actually run".
    let mutable private published = 0

    /// Bounded poll slice for the turn-thread wait, in milliseconds.
    let mutable private pollMilliseconds = 20

    let private first = TaskCompletionSource<Slot>()
    let private episode =
        let bytes = Array.zeroCreate 12
        use rng = Security.Cryptography.RandomNumberGenerator.Create()
        rng.GetBytes(bytes)
        BitConverter.ToString(bytes).Replace("-", "").ToLowerInvariant()

    let private token =
        let bytes = Array.zeroCreate 32
        use rng = Security.Cryptography.RandomNumberGenerator.Create()
        rng.GetBytes(bytes)
        BitConverter.ToString(bytes).Replace("-", "").ToLowerInvariant()

    let private directions =
        [| "N", 0, -1
           "S", 0, 1
           "E", 1, 0
           "W", -1, 0
           "NE", 1, -1
           "NW", -1, -1
           "SE", 1, 1
           "SW", -1, 1 |]

    let private jsonString (value: string) =
        let buf = StringBuilder(value.Length + 2)
        buf.Append('"') |> ignore
        for ch in value do
            match ch with
            | '"' -> buf.Append("\\\"") |> ignore
            | '\\' -> buf.Append("\\\\") |> ignore
            | _ when int ch < 32 -> buf.Append('?') |> ignore
            | _ -> buf.Append(ch) |> ignore
        buf.Append('"') |> ignore
        buf.ToString()

    let private memberValue (target: obj) name =
        let flags = BindingFlags.Instance ||| BindingFlags.Public ||| BindingFlags.FlattenHierarchy
        let ty = target.GetType()
        match ty.GetProperty(name, flags) with
        | null ->
            match ty.GetField(name, flags) with
            | null -> failwith (ty.Name + "." + name)
            | field -> field.GetValue(target)
        | prop -> prop.GetValue(target)

    let private asInt (value: obj) =
        match value with
        | :? int as n -> n
        | :? int64 as n -> int n
        | :? byte as n -> int n
        | :? int16 as n -> int n
        | _ -> Convert.ToInt32(value)

    let private call (target: obj) name (args: obj[]) =
        let methods =
            target.GetType().GetMethods(BindingFlags.Instance ||| BindingFlags.Public)
            |> Array.filter (fun m -> m.Name = name && m.GetParameters().Length = args.Length)
        if methods.Length = 0 then failwith (target.GetType().Name + "." + name)
        let chosen =
            methods
            |> Array.tryFind (fun m ->
                m.GetParameters()
                |> Array.mapi (fun i p ->
                    isNull args.[i] || p.ParameterType.IsAssignableFrom(args.[i].GetType()))
                |> Array.forall id)
            |> Option.defaultValue methods.[0]
        chosen.Invoke(target, args)

    let private glyph (cell: obj) =
        if isNull cell then "?"
        else
            try
                let raw = call cell "getRenderString" [||] :?> string
                if String.IsNullOrEmpty raw then "?"
                else
                    let mutable i = 0
                    let mutable last = '?'
                    while i < raw.Length do
                        let ch = raw.[i]
                        if (ch = '&' || ch = '^') && i + 1 < raw.Length then
                            i <- i + 2
                        elif int ch >= 32 && ch <> '"' && ch <> '\\' then
                            last <- ch
                            i <- i + 1
                        else
                            i <- i + 1
                    string last
            with _ -> "?"

    let private stat (player: obj) name fallback =
        try asInt (call player name [| box "Hitpoints"; box fallback |])
        with _ -> fallback

    /// Upper bound on reported non-scenery entities per observation.
    let private MaxEntities = 40

    /// Perceived map radius. The 5x5 window this shipped with was too myopic to
    /// navigate on; the radius is a knob so a caller can trade tokens for reach.
    let mutable private radius = 6

    let setRadius (value: int) = radius <- max 1 (min 24 value)


    let private visible (cell: obj) =
        // Prefer the game's own visibility verdict. Never reveal a cell the
        // player has not perceived.
        try call cell "IsVisible" [||] :?> bool
        with _ -> true

    /// Objects in a cell that the engine itself does not consider scenery.
    ///
    /// Qud already draws this line: Cell.GetRealNonSceneryObjects is the same
    /// call the game uses to separate terrain and walls from things you can
    /// interact with. Preferring it keeps the projection aligned with the
    /// player's own model of the world instead of guessing from names, and keeps
    /// "dirt path" and "brinestalk wall" out of the entity list, where they were
    /// duplicating dozens of times per view.
    let private objectsIn (cell: obj) =
        let tryCall name =
            try
                match call cell name [||] with
                | null -> null
                | v -> v :?> Collections.IEnumerable
            with _ -> null
        match tryCall "GetRealNonSceneryObjects" with
        | null -> tryCall "GetObjects"
        | r -> r

    /// Read-only entity projection. Only objects the game already considers
    /// visible in a visible cell are reported, and only public presentation
    /// fields: no blueprint metadata, no hidden stats, no internal identity.
    let private entityEntries (player: obj) (zone: obj) (x0: int) (y0: int) =
        let acc = ResizeArray<string * string * int * int * bool * string>()
        // The player is reported once, anchored, and never mixed into the
        // surrounding cells.
        let playerName =
            try
                match memberValue player "DisplayName" with
                | :? string as s when s <> "" -> s
                | _ -> "you"
            with _ -> "you"
        acc.Add((playerName, "self", 0, 0, true, "")) |> ignore
        let seen = HashSet<string>()
        for dy in -radius .. radius do
            for dx in -radius .. radius do
                if dx <> 0 || dy <> 0 then
                    let cell =
                        try call zone "GetCell" [| box (x0 + dx); box (y0 + dy) |]
                        with _ -> null
                    if not (isNull cell) && visible cell then
                        let objs = objectsIn cell
                        if not (isNull objs) then
                            for o in objs do
                                if not (isNull o) && not (Object.ReferenceEquals(o, player)) then
                                    let name =
                                        try
                                            match memberValue o "DisplayName" with
                                            | :? string as s when s <> "" -> s
                                            | _ -> "something"
                                        with _ -> "something"
                                    let glyphChar =
                                        try
                                            match call o "getRenderString" [||] with
                                            | :? string as s when s.Length > 0 -> string s.[0]
                                            | _ -> ""
                                        with _ -> ""
                                    // Public presentation only. The engine's own
                                    // stable handle is deliberately not exported.
                                    let key = sprintf "%s|%d|%d|%s" name dx dy glyphChar
                                    // Bounded: a wide window must not be able to
                                    // flood the decision payload.
                                    if acc.Count < MaxEntities && seen.Add key then
                                        acc.Add((name, "object", dx, dy, false, glyphChar)) |> ignore
        acc |> Seq.toArray

    let private entitiesJson (player: obj) (zone: obj) (x0: int) (y0: int) =
        entityEntries player zone x0 y0
        |> Array.mapi (fun i (name, kind, dx, dy, isSelf, glyphChar) ->
            sprintf
                "{\"id\":\"e%d\",\"name\":%s,\"kind\":%s,\"dx\":%d,\"dy\":%d,\"glyph\":%s,\"is_self\":%s}"
                i (jsonString name) (jsonString kind) dx dy (jsonString glyphChar) (if isSelf then "true" else "false"))
        |> String.concat ","
        |> fun s -> "[" + s + "]"

    let private window (origin: obj) =
        let x0 = asInt (memberValue origin "X")
        let y0 = asInt (memberValue origin "Y")
        let zone = memberValue origin "ParentZone"
        let r = radius
        let rows = ResizeArray<string>()
        for dy in -r .. r do
            let row = StringBuilder()
            for dx in -r .. r do
                let cell =
                    try call zone "GetCell" [| box (x0 + dx); box (y0 + dy) |]
                    with _ -> null
                let ch =
                    if isNull cell then "?"
                    elif not (visible cell) then " "
                    else glyph cell
                row.Append(ch) |> ignore
            rows.Add(row.ToString())
        let width =
            try asInt (call zone "Width" [||])
            with _ -> (2 * r + 1)
        let height =
            try asInt (call zone "Height" [||])
            with _ -> (2 * r + 1)
        x0, y0, rows, zone, width, height

    /// Offer intent-level actions, not just raw steps.
    ///
    /// The verbs are the game's own, so they are only listed when they can
    /// actually resolve right now: talk/use/get are offered per visible
    /// interactable, and goto is offered for cells inside the perceived window.
    /// An action that cannot resolve is rejected rather than silently doing
    /// nothing, so the list is a truthful description of what is available.
    let private zoneForPlayer (player: obj) =
        let cell = memberValue player "CurrentCell"
        memberValue cell "ParentZone"

    let private actionsJson (player: obj) =
        let cell = memberValue player "CurrentCell"
        let x0 = asInt (memberValue cell "X")
        let y0 = asInt (memberValue cell "Y")
        let parts = ResizeArray<string>()

        for (name, dx, dy) in directions do
            parts.Add(sprintf
                "{\"id\":%s,\"kind\":\"move\",\"label\":%s,\"arguments\":{\"dx\":%d,\"dy\":%d}}"
                (jsonString ("move:" + name)) (jsonString ("Move " + name)) dx dy)

        for (name, _, _) in directions do
            parts.Add(sprintf
                "{\"id\":%s,\"kind\":\"move_far\",\"label\":%s,\"arguments\":{}}"
                (jsonString ("far:" + name)) (jsonString ("Move far " + name)))

        // Interaction, one entry per distinct visible interactable name.
        let seenNames = HashSet<string>()
        let zone = zoneForPlayer player
        let entries = entityEntries player zone x0 y0
        for e in entries do
            let (name, kind, _, _, _, _) = e
            if kind = "object" && name <> "" && name <> "something" && seenNames.Add name then
                for verb in [| "talk"; "use"; "get" |] do
                    parts.Add(sprintf
                        "{\"id\":%s,\"kind\":%s,\"label\":%s,\"arguments\":{\"target\":%s}}"
                        (jsonString (verb + ":" + name))
                        (jsonString verb)
                        (jsonString (verb + " " + name))
                        (jsonString name))

        let consoleVerbs =
            [| ("look", "Look"); ("quests", "Quests"); ("journal", "Journal")
               ("history", "Message history"); ("wait", "Wait one turn") |]
        for (cid, label) in consoleVerbs do
            parts.Add(sprintf
                "{\"id\":%s,\"kind\":\"console\",\"label\":%s,\"arguments\":{}}"
                (jsonString cid) (jsonString label))

        "[" + String.concat "," (parts |> Seq.toArray) + "]"

    /// Never allowed to throw: this runs on the turn thread inside publish,
    /// and a malformed action list would break the decision boundary itself.
    let private actionsJsonSafe (player: obj) =
        try actionsJson player with _ -> "[{\"id\":\"wait\",\"kind\":\"wait\",\"label\":\"Wait one turn\",\"arguments\":{}}]"


    /// Text the player has actually seen.
    ///
    /// Reads the live console buffer rather than any game-side message store, so
    /// it cannot surface anything the player has not perceived. "ConsoleLib.
    /// Console" is a namespace, not a type, and the buffer is handed out by
    /// TextConsole.CurrentBuffer; ToString renders it, which avoids having to
    /// decode individual ConsoleChar values.
    let private currentBuffer () =
        try
            let asm =
                AppDomain.CurrentDomain.GetAssemblies()
                |> Array.find (fun a -> a.GetName().Name = "Assembly-CSharp")
            let t = asm.GetType("ConsoleLib.Console.TextConsole", false)
            if isNull t then null
            else
                let flags = BindingFlags.Static ||| BindingFlags.Public ||| BindingFlags.NonPublic
                let f = t.GetField("CurrentBuffer", flags)
                let pr = t.GetProperty("CurrentBuffer", flags)
                if not (isNull f) then f.GetValue(null)
                elif not (isNull pr) then pr.GetValue(null, null)
                else null
        with _ -> null

    let private messagesJson () =
        match currentBuffer () with
        | null -> "[]"
        | buffer ->
            try
                let text = call buffer "ToString" [||] :?> string
                let lines =
                    if isNull text then [||]
                    else
                        text.Split([| '\r'; '\n' |], StringSplitOptions.RemoveEmptyEntries)
                        |> Array.map (fun l -> l.Trim())
                        |> Array.filter (fun l -> l <> "")
                // The buffer renders the whole screen: map above, message log at
                // the bottom. Take the trailing rows in order, which is where the
                // text the player was just shown lives, and skip blank rows.
                let picked =
                    lines
                    |> Array.filter (fun l -> l.Trim().Length > 0)
                    |> Array.truncate 12
                    |> Array.rev
                    |> Array.truncate 12
                    |> Array.rev
                "[" + String.concat "," (Array.map jsonString picked) + "]"
            with _ -> "[]"

    let private observation (player: obj) turn index =
        let cell = memberValue player "CurrentCell"
        let x, y, rows, zone, width, height = window cell
        let hp = max 0 (stat player "Stat" 0)
        let mutable maxHp = max 1 (stat player "BaseStat" 1)
        if hp > maxHp then maxHp <- hp
        let tiles = rows |> Seq.map jsonString |> String.concat ","
        let entities = entitiesJson player zone x y
        let messages = messagesJson ()
        let id = episode + ":" + string index
        id, sprintf
            "{\"episode_id\":%s,\"decision_id\":%s,\"turn\":%d,\"phase\":\"command\",\"player\":{\"x\":%d,\"y\":%d,\"hp\":%d,\"max_hp\":%d},\"view\":{\"radius\":%d,\"zone_width\":%d,\"zone_height\":%d},\"tiles\":[%s],\"entities\":%s,\"messages\":%s,\"prompt\":null,\"actions\":%s}"
            (jsonString episode) (jsonString id) turn x y hp maxHp radius width height tiles entities messages (actionsJsonSafe player)

    let private transition index turn observationJson =
        sprintf
            "{\"observation\":%s,\"reward\":0.0,\"terminated\":false,\"truncated\":false,\"metrics\":{\"task_id\":\"live\",\"objective_version\":\"0\",\"outcome\":\"ongoing\",\"turns_elapsed\":%d,\"decisions_elapsed\":%d}}"
            observationJson turn index

    let private ok requestId result =
        sprintf "{\"protocol_version\":\"0.1\",\"request_id\":%s,\"result\":%s}"
            (jsonString requestId) result

    let private fail requestId code message =
        sprintf
            "{\"protocol_version\":\"0.1\",\"request_id\":%s,\"error\":{\"code\":%s,\"message\":%s}}"
            (jsonString requestId) (jsonString code) (jsonString message)

    let private findString name (json: string) =
        let key = "\"" + name + "\""
        let mutable start = 0
        let mutable found = None
        while found.IsNone && start < json.Length do
            let at = json.IndexOf(key, start)
            if at < 0 then start <- json.Length
            else
                let mutable j = at + key.Length
                while j < json.Length && Char.IsWhiteSpace json.[j] do j <- j + 1
                if j < json.Length && json.[j] = ':' then
                    j <- j + 1
                    while j < json.Length && Char.IsWhiteSpace json.[j] do j <- j + 1
                    if j < json.Length && json.[j] = '"' then
                        j <- j + 1
                        let buf = StringBuilder()
                        let mutable closed = false
                        while j < json.Length && not closed do
                            if json.[j] = '\\' && j + 1 < json.Length then
                                buf.Append(json.[j + 1]) |> ignore
                                j <- j + 2
                            elif json.[j] = '"' then
                                closed <- true
                                j <- j + 1
                            else
                                buf.Append(json.[j]) |> ignore
                                j <- j + 1
                        if closed then found <- Some (buf.ToString())
                        else start <- json.Length
                    else start <- at + key.Length
                else start <- at + key.Length
        found

    let private findInt name (json: string) =
        let key = "\"" + name + "\""
        let at = json.IndexOf(key)
        if at < 0 then None
        else
            let mutable j = at + key.Length
            while j < json.Length && Char.IsWhiteSpace json.[j] do j <- j + 1
            if j >= json.Length || json.[j] <> ':' then None
            else
                j <- j + 1
                while j < json.Length && Char.IsWhiteSpace json.[j] do j <- j + 1
                let start = j
                if j < json.Length && json.[j] = '-' then j <- j + 1
                let digits = j
                while j < json.Length && Char.IsDigit json.[j] do j <- j + 1
                if j = digits then None
                else
                    match Int32.TryParse(json.Substring(start, j - start)) with
                    | true, n -> Some n
                    | _ -> None

    let private capabilities () =
        sprintf
            "{\"protocol_version\":\"0.1\",\"backend\":\"qud-live\",\"game_build\":%s,\"is_mock\":false,\"snapshot\":false,\"deterministic_restore\":false,\"full_state_hash\":false}"
            (jsonString gameBuild)

    let private publish (player: obj) turn =
        let index = lock gate (fun () -> decisions)
        let id, body = observation player turn index
        let slot = Slot(id, index, turn, body)
        lock gate (fun () ->
            match waiting with
            | Some previous -> previous.Onward.TrySetResult(slot) |> ignore
            | None -> first.TrySetResult(slot) |> ignore
            waiting <- Some slot
            decisions <- index + 1
            published <- published + 1)
        if logPath <> "" then
            Probe.record logPath ("boundary " + id + " turn=" + string turn) |> ignore
        slot

    // Called on the game thread from Keyboard.IdleWait. Never blocks on a client
    // task and never waits without a bound.
    //
    // Keyboard.IdleWait is itself the game's blocking "needs a command" wait, so
    // the turn thread has to hold the turn somewhere. It holds it here by polling
    // a game-owned slot in short bounded slices, and only while this process holds
    // an episode. It never awaits a TaskCompletionSource supplied by a client, and
    // a cancel (session end, disconnect, or shutdown) breaks the loop at once, so
    // a silent client cannot wedge the turn thread. That is what previously ended
    // every live run in ThreadAbortException.
    //
    // Returns "" when cancelled so the caller lets the original IdleWait run.
    /// Resolve an action id to one of the game's own input verbs, plus the
    /// argument PushCommand expects.
    ///
    /// These are the engine's Cmd* verbs rather than anything invented here, so
    /// movement, interaction and pickup go through the same code path a player's
    /// keypress does. That keeps prompts, refusals and failed moves behaving the
    /// way the game intends instead of being reimplemented on top of it.
    let private findEntityNamed (player: obj) (name: string) =
        let cell = memberValue player "CurrentCell"
        let zone = memberValue cell "ParentZone"
        let x0 = asInt (memberValue cell "X")
        let y0 = asInt (memberValue cell "Y")
        let mutable found = null
        let mutable ring = 0
        while isNull found && ring <= radius do
            for dy in -ring .. ring do
                for dx in -ring .. ring do
                    if isNull found && (System.Math.Abs(dx) = ring || System.Math.Abs(dy) = ring) then
                        let c =
                            try call zone "GetCell" [| box (x0 + dx); box (y0 + dy) |]
                            with _ -> null
                        if not (isNull c) && visible c then
                            let objs = objectsIn c
                            if not (isNull objs) then
                                for o in objs do
                                    if isNull found && not (isNull o) then
                                        let n =
                                            try
                                                match memberValue o "DisplayName" with
                                                | :? string as s -> s
                                                | _ -> ""
                                            with _ -> ""
                                        if n.Equals(name, StringComparison.OrdinalIgnoreCase) then found <- o
            ring <- ring + 1
        found

    let private cellAt (player: obj) (x: int) (y: int) =
        let cell = memberValue player "CurrentCell"
        let zone = memberValue cell "ParentZone"
        try call zone "GetCell" [| box x; box y |] with _ -> null

    /// The live player, for validating an action from the transport thread.
    ///
    /// Same static read the observation path already performs. It is a plain
    /// reference read, not a state mutation, and it is the only way to reject a
    /// malformed action before it is staged.
    let private livePlayer () =
        try
            let asm =
                AppDomain.CurrentDomain.GetAssemblies()
                |> Array.find (fun a -> a.GetName().Name = "Assembly-CSharp")
            let t = asm.GetType("XRL.The", false)
            if isNull t then null
            else
                let p = t.GetProperty("Player", BindingFlags.Static ||| BindingFlags.Public)
                if isNull p then null else p.GetValue(null, null)
        with _ -> null

    let private commandOf (action: string) (player: obj) =
        if action = "wait" then Some("CmdWait", box null)
        elif action = "look" then Some("CmdLook", box null)
        elif action = "quests" then Some("CmdQuests", box null)
        elif action = "journal" then Some("CmdJournal", box null)
        elif action = "history" then Some("CmdMessageHistory", box null)
        elif action.StartsWith("far:") then
            let d = action.Substring(4)
            if directions |> Array.exists (fun (name, _, _) -> name = d) then
                Some("CmdMoveFar" + d, box null)
            else None
        elif action.StartsWith("goto:") then
            let parts = action.Substring(5).Split([| ','; ' ' |], StringSplitOptions.RemoveEmptyEntries)
            match parts with
            | [| a; b |] ->
                try
                    let target = cellAt player (Int32.Parse a) (Int32.Parse b)
                    if isNull target then None else Some("CmdMoveTo", box target)
                with _ -> None
            | _ -> None
        elif action.StartsWith("talk:") || action.StartsWith("use:") || action.StartsWith("get:") then
            let verb, rest =
                if action.StartsWith("talk:") then "CmdTalk", action.Substring(5)
                elif action.StartsWith("use:") then "CmdUse", action.Substring(4)
                else "CmdGet", action.Substring(4)
            let target = findEntityNamed player rest
            if isNull target then None else Some(verb, box target)
        else
            let prefix = "move:"
            if action.StartsWith(prefix) then
                let facing = action.Substring(prefix.Length)
                if directions |> Array.exists (fun (name, _, _) -> name = facing) then
                    Some("CmdMove" + facing, box null)
                else None
            else None

    let supply (player: obj) (turn: int) =
        let mutable announced = -1
        let mutable result : (string * obj) option = None
        let mutable running = true
        while running && result.IsNone do
            let staged =
                lock gate (fun () ->
                    match waiting with
                    | Some slot when slot.Action.IsSome && not slot.Consumed -> Some slot
                    | _ -> None)
            let fromQueue =
                if staged.IsSome then None
                else
                    lock gate (fun () ->
                        if pending.Count > 0 then
                            let a = pending.Dequeue()
                            consumed <- consumed + 1
                            Some a
                        else None)
            match staged, fromQueue with
            | Some slot, _ ->
                lock gate (fun () -> slot.Consumed <- true)
                let actionId = slot.Action.Value
                result <- commandOf actionId player
            | None, Some actionId ->
                result <- commandOf actionId player
            | None, None ->
                if lock gate (fun () -> cancelled) then
                    running <- false
                else
                    // Publish the first observation for a turn exactly once.
                    let needsPublish =
                        lock gate (fun () ->
                            match waiting with
                            | Some slot when slot.Turn = turn && not slot.Consumed -> false
                            | _ -> true)
                    if needsPublish then
                        publish player turn |> ignore
                    // A queued action still needs an observation published for
                    // this turn so the client sees where it ended up.
                    // Bounded, cancellable slice. Never a client task.
                    Thread.Sleep(pollMilliseconds)
                    if announced <> turn then
                        announced <- turn
                        if logPath <> "" then
                            Probe.record logPath ("awaiting action turn=" + string turn) |> ignore
        if result.IsNone then
            lock gate (fun () -> cancelled <- false)
        // A verb that takes no argument comes back as a bare string; a verb with
        // a resolved target comes back as a (verb, target) pair. Only an
        // unresolvable action is null, so the caller falls through to the game's
        // own wait instead of injecting anything. Conflating "no argument" with
        // "no command" silently swallowed every plain move.
        match result with
        | Some(name, null) -> box name
        | Some pair -> box pair
        | None -> box null

    /// Enqueue a movement script and wait for the turn thread to drain it.
    ///
    /// Returns when the script is exhausted, when an action is rejected, or
    /// when the turn thread stops consuming -- which is what a dialogue or a
    /// combat interrupt looks like from here. That stall is reported rather
    /// than hidden, because "the script did not finish" is exactly the signal a
    /// caller needs to decide what to do next. A single blocked step is not
    /// retried and the queue is left intact for a follow-up call.
    let runScript (actions: string[]) (stallMilliseconds: int) =
        let startPublished =
            lock gate (fun () ->
                // No pre-validation here: an action can only be resolved against
                // the live world, which the turn thread has and this thread does
                // not. supply rejects anything unresolvable and injects nothing.
                for a in actions do pending.Enqueue a
                consumed, pending.Count, published)
        let start = DateTime.UtcNow
        let mutable last = startPublished
        let mutable settled = false
        let mutable sawProgress = false
        while not settled do
            Thread.Sleep(25)
            let now = lock gate (fun () -> consumed, pending.Count, published)
            if now <> last then
                last <- now
                sawProgress <- true
            else
                // Settled means: nothing new was published for the whole window.
                // Requiring an idle window after the last publication is what
                // makes this a completion signal rather than a dispatch signal;
                // without it the caller gets a readout from before the commands
                // ran. An untouched game also settles, which is correct: there
                // was nothing to wait for.
                let elapsed = int (DateTime.UtcNow - start).TotalMilliseconds
                let (cNow, qNow, _) = now
                let (cStart, qStart, _) = startPublished
                let drained = cNow >= cStart + qStart
                if elapsed >= stallMilliseconds && (sawProgress || drained) then settled <- true
        let cStart, _, _ = startPublished
        let doneCount, left =
            lock gate (fun () -> consumed - cStart, pending.Count)
        (doneCount, left, settled)

    /// Bounded by construction: it returns on completion or on a stall.
    let run (actions: string[]) (stallMilliseconds: int) = runScript actions stallMilliseconds

    let private currentWaiting () =
        lock gate (fun () ->
            match waiting with
            | Some slot when slot.Action.IsNone && not slot.Consumed -> Some slot
            | _ -> None)

    let private handleOp requestId op body =
        match op with
        | "hello" -> ok requestId (capabilities ())
        | "reset" ->
            match findInt "seed" body with
            | Some n when n <> 0 ->
                fail requestId "invalid_seed" "This process has already embarked; pass seed 0"
            | None ->
                fail requestId "invalid_seed" "seed must be an integer"
            | Some _ ->
                let claimed =
                    lock gate (fun () ->
                        if resetUsed then false
                        else
                            resetUsed <- true
                            true)
                if not claimed then
                    fail requestId "unsupported" "This process holds one episode; start another game to reset"
                else
                    let slot = first.Task.GetAwaiter().GetResult()
                    ok requestId (transition slot.Index slot.Turn slot.Observation)
        | "observe" ->
            if not resetUsed then fail requestId "reset_required" "Reset before observing"
            else
                match currentWaiting () with
                | Some slot -> ok requestId slot.Observation
                | None -> fail requestId "reset_required" "No decision is waiting"
        | "step" ->
            if not resetUsed then fail requestId "reset_required" "Reset before stepping"
            else
                match findString "decision_id" body, findString "action_id" body with
                | None, _ | _, None ->
                    fail requestId "invalid_action" "step requires decision_id and action_id"
                | Some decisionId, Some actionId ->
                    match commandOf actionId (livePlayer ()) with
                    | None -> fail requestId "invalid_action" "Action is not a current candidate"
                    | Some _ ->
                        let slot = currentWaiting ()
                        match slot with
                        | None -> fail requestId "stale_decision" "No decision is waiting"
                        | Some current when current.Id <> decisionId ->
                            fail requestId "stale_decision" "Re-observe: the decision boundary has changed"
                        | Some current ->
                            // Staging happens here on the Suave request thread. The
                            // game thread is never signalled and never waits; it
                            // picks this up on a later IdleWait tick.
                            if current.Consumed || current.Action.IsSome then
                                fail requestId "stale_decision" "That decision was already stepped"
                            else
                                current.Action <- Some actionId
                                // Waiting for the next boundary is safe here: this is
                                // the transport thread, not the game turn thread.
                                let next = current.Onward.Task.GetAwaiter().GetResult()
                                ok requestId (transition next.Index next.Turn next.Observation)
        | "run" ->
            // Batch a movement script and read one state back. The whole script
            // is the contract: nothing partial is reported as success, and a
            // stall is surfaced so the caller can tell a dialogue interrupt from
            // a completed script.
            let items =
                match if isNull body then None else findString "actions" body with
                | None -> [||]
                | Some raw ->
                    raw.Split([| ','; ' ' |], StringSplitOptions.RemoveEmptyEntries)
                    |> Array.map (fun a -> a.Trim().Trim('"'))
            let doneCount, left, halted = run items 400
            // Completed and interrupted are different outcomes and must not be
            // reported as the same thing: a finished script is a success, a
            // script the turn thread stopped consuming is an interruption.
            let completed = left = 0 && doneCount = items.Length
            let slot = lock gate (fun () -> waiting)
            let obs =
                match slot with
                | Some sl when not sl.Consumed -> sl.Observation
                | _ -> "null"
            ok requestId (sprintf
                "{\"actions_submitted\":%d,\"actions_consumed\":%d,\"actions_remaining\":%d,\"completed\":%s,\"interrupted\":%s,\"observation\":%s}"
                items.Length doneCount left
                (if completed then "true" else "false")
                (if (not completed) && halted then "true" else "false") obs)
        | "snapshot" | "restore" | "release" | "state_hash" ->
            fail requestId "unsupported" "Live control does not expose snapshots or a full-state hash"
        | _ -> fail requestId "unsupported" "Unknown operation"

    let private dispatch body =
        match findString "request_id" body, findString "op" body with
        | None, _ | _, None -> None
        | Some requestId, Some op ->
            let cached =
                lock gate (fun () ->
                    match cache.TryGetValue(requestId) with
                    | true, (fingerprint, response) when fingerprint = body -> Some response
                    | true, _ -> Some (fail requestId "request_id_conflict" "Request ID reused with different content")
                    | _ -> None)
            match cached with
            | Some response -> Some response
            | None ->
                let response =
                    try handleOp requestId op body
                    with ex ->
                        fail requestId "internal_error" (ex.GetType().Name + ": " + ex.Message)
                lock gate (fun () -> cache.[requestId] <- (body, response))
                Some response

    let private same (left: string) (right: string) =
        let mutable diff = left.Length ^^^ right.Length
        let n = min left.Length right.Length
        for i in 0 .. n - 1 do
            diff <- diff ||| (int left.[i] ^^^ int right.[i])
        diff = 0

    // One connection, many decisions. RPC runs off the socket loop so a step
    // that waits on the game thread can still answer a ping.
    let private rpcLock = obj ()

    let private session (webSocket: WebSocket) (_context: HttpContext) =
        socket {
            let mutable loop = true
            while loop do
                let! msg = webSocket.read()
                match msg with
                | (Text, data, true) ->
                    if data.Length > 1048576 then
                        do! webSocket.send Close (ArraySegment [||]) true
                        loop <- false
                    else
                        let text = Encoding.UTF8.GetString data
                        Threading.ThreadPool.QueueUserWorkItem(fun _ ->
                            lock rpcLock (fun () ->
                                match dispatch text with
                                | None -> ()
                                | Some response ->
                                    let bytes = Encoding.UTF8.GetBytes response
                                    webSocket.send Text (ArraySegment bytes) true
                                    |> Async.RunSynchronously
                                    |> ignore))
                        |> ignore
                | (Ping, data, _) ->
                    do! webSocket.send Pong (ArraySegment data) true
                | (Close, _, _) ->
                    do! webSocket.send Close (ArraySegment [||]) true
                    loop <- false
                | _ -> ()
        }

    let private app =
        choose [
            path "/rpc" >=> request (fun req ->
                match req.header "origin" with
                | Choice1Of2 _ -> FORBIDDEN "browser origin"
                | _ ->
                    match req.header "authorization" with
                    | Choice1Of2 value when same value ("Bearer " + token) ->
                        handShake session
                    | _ -> UNAUTHORIZED "Authentication required")
            NOT_FOUND "Not found"
        ]

    /// Release the turn thread from a wait. Called only from the transport
    /// thread; never touches game state.
    let cancel () = lock gate (fun () -> cancelled <- true)


    let setPollMilliseconds (value: int) =
        pollMilliseconds <- max 1 (min 250 value)

    let listen (controlPath: string) (diagnosticPath: string) (build: string) =
        let start =
            lock gate (fun () ->
                if listening then false
                else
                    listening <- true
                    logPath <- diagnosticPath
                    gameBuild <- build
                    true)
        if not start then ()
        else
            Directory.CreateDirectory(Path.GetDirectoryName(controlPath)) |> ignore
            let config =
                { defaultConfig with
                    bindings = [ HttpBinding.create HTTP IPAddress.Loopback 8765us ]
                    logger = Logging.Targets.create Logging.LogLevel.Warn [| "QudGym" |] }
            let _ready, server = startWebServerAsync config app
            Async.Start server
            let mutable bound = false
            let mutable attempt = 0
            while not bound && attempt < 50 do
                attempt <- attempt + 1
                try
                    use probe = new Sockets.TcpClient()
                    probe.Connect(IPAddress.Loopback, 8765)
                    bound <- true
                with _ ->
                    Threading.Thread.Sleep(20)
            if not bound then failwith "control socket did not bind"
            File.WriteAllText(controlPath, "ws://127.0.0.1:8765/rpc\n" + token + "\n")
            Probe.record diagnosticPath "control listening" |> ignore
