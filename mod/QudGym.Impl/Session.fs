namespace QudGym

open System
open System.Collections.Generic
open System.IO
open System.Net
open System.Net.Sockets
open System.Reflection
open Newtonsoft.Json
open Newtonsoft.Json.Linq
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
    // A plan, when set, supplies the action instead of the client queue. It is
    // advanced from inside supply, i.e. on the game turn thread, so the tests
    // it evaluates read the same live world the observation is built from.
    let mutable private currentPlan : Plan.Plan option = None
    let mutable private planNote = ""
    // Which branch advance took last, and what it was asked to do. A plan
    // that stops early is otherwise indistinguishable from a plan that
    // finished, which is how a one-step run reported itself as finished.
    let mutable private planTrace = ""
    // The game's own message log, captured as text.
    //
    // Scraping TextConsole.CurrentBuffer gave "????": ScreenBuffer.ToString
    // does walk the cells, but the log is drawn through a glyph table, so the
    // cell characters are not the message. XRLCore hands out the real string
    // through RegisterNewMessageLogEntryCallback, and only for entries the game
    // actually logged, which is also the perception boundary we want.
    let logGate = obj ()
    let logMessages = ResizeArray<string>()
    let mutable logHooked = false
    let mutable private planSteps = 0
    // A reset that is waiting for its first boundary holds a claim but has not
    // consumed the episode, so a bounded timeout leaves the episode resettable.
    let mutable private resetInFlight = false
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
    // Actions dequeued but not resolvable against the live world. Tracked so
    // a batch never reports success for an action that did nothing.
    let mutable private rejected = 0

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
    /// Read a boolean instance property, distinguishing "absent" from "false".
    ///
    /// A predicate the build does not expose must report unknown rather than
    /// false, or a caller will read "not an actor" out of "this build cannot say".
    let private boolOfMember (target: obj) (name: string) =
        try
            let p = target.GetType().GetProperty(name, BindingFlags.Instance ||| BindingFlags.Public ||| BindingFlags.FlattenHierarchy)
            if isNull p then None
            else
                match p.GetValue(target, null) with
                | (:? bool as b) -> Some b
                | _ -> None
        with _ -> None

    let private entityEntries (player: obj) (zone: obj) (x0: int) (y0: int) =
        let acc = ResizeArray<string * string * int * int * bool * string * bool option>()
        // The player is reported once, anchored, and never mixed into the
        // surrounding cells.
        let playerName =
            try
                match memberValue player "DisplayName" with
                | :? string as s when s <> "" -> s
                | _ -> "you"
            with _ -> "you"
        acc.Add((playerName, "self", 0, 0, true, "", Some true)) |> ignore
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
                                    // The game's own predicate for "this is a
                                    // person or creature". Guessing from names put
                                    // a watervine in the talk list, and talking to a
                                    // watervine is not an action.
                                    let isActor = boolOfMember o "IsActor"
                                    let key = sprintf "%s|%d|%d|%s" name dx dy glyphChar
                                    // Bounded: a wide window must not be able to
                                    // flood the decision payload.
                                    if acc.Count < MaxEntities && seen.Add key then
                                        acc.Add((name, "object", dx, dy, false, glyphChar, isActor)) |> ignore
        acc |> Seq.toArray

    let private entitiesJson (player: obj) (zone: obj) (x0: int) (y0: int) =
        entityEntries player zone x0 y0
        |> Array.mapi (fun i (name, kind, dx, dy, isSelf, glyphChar, isActor) ->
            // Absolute coordinates, matching PerceivedEntity. Relative offsets
            // are derivable from player x/y, so they are not sent twice.
            let role = if isSelf then "self" else kind
            let status = sprintf "%s|%+d,%+d" role dx dy
            sprintf
                "{\"id\":\"e%d\",\"name\":%s,\"x\":%d,\"y\":%d,\"perceived_status\":%s,\"glyph\":%s,\"is_actor\":%s}"
                i (jsonString name) (x0 + dx) (y0 + dy) (jsonString status)
                (jsonString glyphChar)
                (match isActor with
                 | Some true -> "true"
                 | Some false -> "false"
                 | None -> "null"))
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
                "{\"id\":%s,\"kind\":\"move\",\"label\":%s,\"arguments\":{\"steps\":\"far\"}}"
                (jsonString ("far:" + name)) (jsonString ("Move far " + name)))

        // Interaction, one entry per distinct visible interactable name.
        let seenNames = HashSet<string>()
        let zone = zoneForPlayer player
        let entries = entityEntries player zone x0 y0
        for e in entries do
            let (name, kind, _, _, _, _, isActor) = e
            if kind = "object" && name <> "" && name <> "something" && seenNames.Add name then
                // Talking is only offered to things the game says are actors.
                // is_actor is reported but not used to gate: the predicate does
                // not resolve on this build (false even for NPCs), and gating on
                // it silently removed talking as a capability. An agent picks an
                // NPC by name; the game decides whether the talk does anything.
                // Verbs map onto the contract's semantic categories.
                let verbs =
                    [| ("talk", "interact"); ("use", "interact"); ("get", "inventory") |]
                for (verb, kind) in verbs do
                    parts.Add(sprintf
                        "{\"id\":%s,\"kind\":%s,\"label\":%s,\"arguments\":{\"target\":%s}}"
                        (jsonString (verb + ":" + name))
                        (jsonString kind)
                        (jsonString (verb + " " + name))
                        (jsonString name))

        let consoleVerbs =
            [| ("look", "Look"); ("quests", "Quests"); ("journal", "Journal")
               ("history", "Message history"); ("wait", "Wait one turn") |]
        for (cid, label) in consoleVerbs do
            parts.Add(sprintf
                "{\"id\":%s,\"kind\":\"info\",\"label\":%s,\"arguments\":{}}"
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

    /// Subscribe to the game's message log, once.
    let private ensureLogHook () =
        if logHooked then ()
        else
            try
                let asm =
                    AppDomain.CurrentDomain.GetAssemblies()
                    |> Array.find (fun a -> a.GetName().Name = "Assembly-CSharp")
                let core = asm.GetType("XRL.Core.XRLCore", false)
                let m =
                    core.GetMethod(
                        "RegisterNewMessageLogEntryCallback",
                        BindingFlags.Static ||| BindingFlags.Public
                        ||| BindingFlags.NonPublic)
                if isNull m then failwith "no RegisterNewMessageLogEntryCallback"
                let callback: Action<string> =
                    Action<string>(fun (text: string) ->
                        if not (String.IsNullOrWhiteSpace text) then
                            lock logGate (fun () ->
                                logMessages.Add(text.Trim())
                                if logMessages.Count = 1 && logPath <> "" then
                                    Probe.record logPath ("message log first entry: " + text.Trim())
                                    |> ignore
                                // Bound it: an unattended game would otherwise
                                // accumulate without limit.
                                if logMessages.Count > 200 then
                                    logMessages.RemoveAt 0))
                m.Invoke(null, [| box callback |]) |> ignore
                logHooked <- true
                if logPath <> "" then
                    Probe.record logPath "message log hooked" |> ignore
            with ex ->
                // Never swallow this silently. A hook that failed to install
                // leaves the projection reading a glyph table, which looks like
                // "no messages" rather than like a broken hook, and that is how
                // a whole session of ??? went unquestioned.
                if logPath <> "" then
                    Probe.record logPath
                        ("message log hook FAILED " + ex.GetBaseException().Message)
                    |> ignore

    /// The most recent messages the game logged, oldest first.
    ///
    /// This is a rolling window, not a per-boundary delta, and the difference
    /// matters. Draining cleared the buffer, so a message logged between two
    /// decision boundaries was destroyed whenever no client observed in
    /// between -- which is the normal case, since supply runs continuously. The
    /// log the game builds is a history, and the observation should carry the
    /// recent history, so nothing is dropped for being logged quickly.
    let private recentLog (take: int) =
        ensureLogHook ()
        lock logGate (fun () ->
            let all = logMessages.Count
            let start = max 0 (all - take)
            logMessages |> Seq.skip start |> Seq.toArray)

    /// What the player was shown, as text.
    ///
    /// The log hook is the source. The buffer scrape is kept only as a fallback
    /// for a boundary that arrives before the hook is installed, and it is
    /// labelled as such by returning it only when the log is empty.
    let private messagesJson () =
        let logged = recentLog 12
        "[" + String.concat "," (Array.map jsonString logged) + "]"

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

    /// Parse a JSON array of strings, e.g. ["move:E","wait"].
    ///
    /// findString only matches a quoted string, so a caller sending an array --
    /// which is the natural thing to send for a list of actions -- silently
    /// produced an empty list. The empty list then satisfied
    /// `doneCount = items.Length` and the run reported success having done
    /// nothing. This reads the array properly, and a non-array is a hard error
    /// rather than an empty plan.
    // -- request body parsing ---------------------------------------------
    //
    // These used to locate a field with IndexOf and read forward to the next
    // quote. A value that was not a plain string -- an array, an escaped
    // string, a nested object -- then produced nothing at all, silently, and
    // an empty action list satisfied `doneCount = items.Length`, so a batched
    // run reported success having executed nothing. A hand-rolled reader cannot
    // be trusted to fail loudly, so the mod borrows the JSON parser the game
    // already loads.

    let private bodyOf (json: string) =
        try JObject.Parse json with _ -> null

    let private findString name (json: string) =
        let o = bodyOf json
        if isNull o then None
        else
            match o.[name] with
            | :? JValue as v when v.Type = JTokenType.String -> Some(v.Value.ToString())
            | _ -> None

    let private findInt name (json: string) =
        let o = bodyOf json
        if isNull o then None
        else
            match o.[name] with
            | :? JValue as v when v.Type = JTokenType.Integer -> Some(Convert.ToInt32 v.Value)
            | _ -> None

    /// A JSON array of strings, decoded. Escapes are handled by the parser, so
    /// a multi-line program arrives with its newlines intact.
    let private findStringArray name (json: string) =
        let o = bodyOf json
        if isNull o then [||]
        else
            match o.[name] with
            | :? JArray as arr ->
                arr
                |> Seq.map (fun t ->
                    match t with
                    | :? JValue as v when v.Type = JTokenType.String -> v.Value.ToString()
                    | _ -> failwithf "%s must be an array of strings" name)
                |> Seq.toArray
            | _ -> failwithf "%s must be a JSON array of strings" name
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

    // The player's cell, or Unknown when it cannot be read. A sentinel rather
    // than an option keeps the arithmetic below free of nested tuple patterns.
    let private unknownPos = -9999

    let private playerPosition () =
        try
            let player = livePlayer ()
            if isNull player then (unknownPos, unknownPos)
            else
                let cell = memberValue player "CurrentCell"
                if isNull cell then (unknownPos, unknownPos)
                else
                    let read name =
                        match memberValue cell name with
                        | :? int as v -> v
                        | _ -> unknownPos
                    read "X", read "Y"
        with _ -> (unknownPos, unknownPos)


    /// Find a named object within `maxRing` cells, ring 0 being the player's own
    /// cell. findEntityNamed walks the whole view radius, which is right for
    /// targeting but wrong for conversation: CmdTalk only speaks to an object in
    /// an adjacent cell, so a target three cells away was found and then
    /// correctly refused by AttemptConversation.
    let private findEntityWithin (player: obj) (name: string) (maxRing: int) =
        let cell = memberValue player "CurrentCell"
        let zone = memberValue cell "ParentZone"
        let x0 = asInt (memberValue cell "X")
        let y0 = asInt (memberValue cell "Y")
        let mutable found = null
        let mutable ring = 0
        while isNull found && ring <= maxRing do
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

    let private gameAssembly () =
        AppDomain.CurrentDomain.GetAssemblies()
        |> Array.find (fun a -> a.GetName().Name = "Assembly-CSharp")

    /// Start a conversation with a target, using the game's own entry point.
    ///
    /// CmdTalk cannot be driven for this. It ignores its argument entirely and
    /// calls PickDirection.ShowPicker, which loops on Keyboard.getvk waiting for
    /// a human keypress; everything it does after the picker is the game's own
    /// conversation path, reached through ConversationScript.AttemptConversation.
    /// So this resolves the target the picker would have asked for and calls that
    /// same method.
    ///
    /// Not silent, deliberately. CmdTalk tries AttemptConversation(Silent: true)
    /// first and only shows the conversation when that returns false, so asking
    /// the watervine farmer a question produced no output at all and looked
    /// like a no-op rather than a suppressed conversation.
    let private say (m: string) =
        if logPath <> "" then Probe.record logPath ("talk: " + m) |> ignore

    let private show (v: obj) =
        if isNull v then "(null)" else v.ToString()

    let private attemptConversation (target: obj) =
        try
            let partType = (gameAssembly ()).GetType("XRL.World.Parts.ConversationScript", false)
            if isNull partType then say "no ConversationScript type"; false
            else
                let getter =
                    target.GetType().GetMethods(BindingFlags.Instance ||| BindingFlags.Public)
                    |> Array.tryFind (fun m ->
                        m.Name = "GetPart" && m.IsGenericMethodDefinition
                        && m.GetParameters().Length = 0)
                match getter with
                | None -> say "no GetPart<T>() on GameObject"; false
                | Some g ->
                    let part = g.MakeGenericMethod(partType).Invoke(target, [||])
                    if isNull part then
                        say ("target has no ConversationScript: " + show (memberValue target "DisplayName"))
                        false
                    else
                        let talk =
                            part.GetType().GetMethod("AttemptConversation", BindingFlags.Instance ||| BindingFlags.Public)
                        if isNull talk then say "no AttemptConversation"; false
                        else
                            // AttemptConversation(bool Silent, bool? Mental, IEvent ParentEvent)
                            let args =
                                [| box false; (box null :> obj); (box null :> obj) |]
                            if talk.GetParameters().Length <> 3 then
                                say ("AttemptConversation arity " + string (talk.GetParameters().Length))
                                false
                            else
                                let outcome = talk.Invoke(part, args)
                                say ("AttemptConversation returned " + show outcome)
                                match outcome with
                                | :? bool as ok -> ok
                                | _ -> true
        with ex ->
            say ("threw " + ex.GetBaseException().Message)
            false

    /// Actions the harness performs itself rather than pushing as a key command.
    ///
    /// Returns Some verb when the action was carried out here, so the caller
    /// pushes that verb instead of the action's own. Only conversations need
    /// this: every other intent verb resolves to a real Cmd* the game already
    /// accepts.
    /// Ask the game for the next step toward a cell, using its own pathfinder.
    ///
    /// AutoAct.TryFindPathStep is the game's real navigation: it uses nav
    /// weights, doors and terrain, and it caches the path. Setting
    /// AutoAct.Setting instead -- which is what CmdMoveTo does -- only marks the
    /// autopilot as walking, and a non-empty PlayerWalking actually *blocks*
    /// player movement, so nothing happened and the plan sat there re-issuing the
    /// request. Steering one cell at a time by dominant axis was the opposite
    /// mistake: a second, worse pathfinder next to the game's.
    ///
    /// Returns the game's own direction name, or None when it has no route.
    let private pathStepTo (player: obj) (tx: int) (ty: int) =
        try
            let cell = memberValue player "CurrentCell"
            let zone = memberValue cell "ParentZone"
            let target = call zone "GetCell" [| box tx; box ty |]
            if isNull target then None
            else
                let autoAct =
                    (gameAssembly ()).GetType("XRL.World.Capabilities.AutoAct", false)
                if isNull autoAct then None
                else
                    let m =
                        autoAct.GetMethod(
                            "TryFindPathStep",
                            BindingFlags.Static ||| BindingFlags.Public)
                    if isNull m then None
                    else
                        let args = [| box target; box null |]
                        let found = m.Invoke(null, args)
                        match found, args.[1] with
                        | :? bool as ok, (:? string as step) when ok && step <> "." && step <> "" ->
                            Some step
                        | _ -> None
        with _ -> None

    /// The game's direction name to the CmdMove verb that performs it.
    /// The game's step code to the CmdMove verb that performs it.
    ///
    /// The codes are the short uppercase forms the pathfinder actually emits --
    /// "NE", not "northeast". Matching only the long names made every real step
    /// unresolvable, and the plan sat there reporting "no verb" for a step the
    /// game had already computed correctly.
    let private moveVerbFor (step: string) =
        let code = step.Trim().ToUpperInvariant()
        let direct =
            match code with
            | "N" -> "CmdMoveN"
            | "S" -> "CmdMoveS"
            | "E" -> "CmdMoveE"
            | "W" -> "CmdMoveW"
            | "NE" -> "CmdMoveNE"
            | "NW" -> "CmdMoveNW"
            | "SE" -> "CmdMoveSE"
            | "SW" -> "CmdMoveSW"
            | _ -> ""
        if direct <> "" then Some direct
        else
        let s = step.ToLowerInvariant()
        if s.Contains("north") && s.Contains("east") then Some "CmdMoveNE"
        elif s.Contains("north") && s.Contains("west") then Some "CmdMoveNW"
        elif s.Contains("south") && s.Contains("east") then Some "CmdMoveSE"
        elif s.Contains("south") && s.Contains("west") then Some "CmdMoveSW"
        elif s.Contains("north") then Some "CmdMoveN"
        elif s.Contains("south") then Some "CmdMoveS"
        elif s.Contains("east") then Some "CmdMoveE"
        elif s.Contains("west") then Some "CmdMoveW"
        else None

    let private tryDirect (player: obj) (action: string) =
        if action.StartsWith("move_to:") then
            // One step of the game's own route, requested fresh each turn.
            let parts = action.Substring(8).Split(',')
            if parts.Length <> 2 then None
            else
                try
                    let tx, ty = int (parts.[0].Trim()), int (parts.[1].Trim())
                    match pathStepTo player tx ty with
                    | Some step ->
                        match moveVerbFor step with
                        | Some verb -> Some(verb, box null)
                        | None ->
                            say ("no verb for step '" + step + "'")
                            None
                    | None -> say ("no route to " + string tx + "," + string ty); None
                with _ -> None
        else if action.StartsWith("talk:") then
            let name = action.Substring(5)
            // Adjacent only, matching CmdTalk's own rule via GetCellFromDirection.
            match findEntityWithin player name 1 with
            | null ->
                say ("no '" + name + "' in an adjacent cell")
                None
            | target ->
                if attemptConversation target then Some("CmdNone", box null)
                else
                    // Report the failure rather than falling through silently:
                    // a no-op conversation is indistinguishable from a no-op move.
                    say ("no conversation started for " + name + "; falling through to CmdTalk")
                    None
        else None

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
        // A bounded sleep does not bound a loop. Without a deadline this spins
        // on the game turn thread until an action arrives, and a client that
        // disconnected -- or a game that will never offer another boundary --
        // left it spinning forever. On expiry the turn is handed back to the
        // game, which waits for real input, and the fact is recorded rather than
        // left as a silent stall.
        let deadline = DateTime.UtcNow.AddSeconds 900.0
        while running && result.IsNone do
            if DateTime.UtcNow > deadline then
                running <- false
                if logPath <> "" then
                    Probe.record logPath
                        (sprintf "supply deadline reached at turn %d; handing the turn back" turn)
                    |> ignore
            let staged =
                lock gate (fun () ->
                    match waiting with
                    | Some slot when slot.Action.IsSome && not slot.Consumed -> Some slot
                    | _ -> None)
            // A plan, if one is running, decides this turn's action. It is
            // consulted before the client queue so a plan is not starved by a
            // waiting client, and it is advanced exactly one action per turn so
            // the world is observed between steps.
            let fromPlan =
                match currentPlan with
                | None -> None
                | Some p ->
                    let available a =
                        (match tryDirect player a with
                         | Some _ -> true
                         | None -> commandOf a player |> Option.isSome)
                    // Gather the view the plan's tests read, from the same
                    // entity walk the observation publishes, so a plan and the
                    // observation beside it can never disagree about what is
                    // nearby.
                    let view =
                        try
                            let cell = memberValue player "CurrentCell"
                            if isNull cell then
                                { Plan.Player = player; Plan.Nearby = []; Plan.Entities = [] }
                            else
                                let cx, cy, _, zone, _, _ = window cell
                                // Absolute positions, exactly as the
                                // observation publishes them, so a plan
                                // targets from the same coordinates a reader
                                // sees rather than from a fresh sweep.
                                let located =
                                    entityEntries player zone cx cy
                                    |> Seq.map (fun (name, _, dx, dy, _, _, _) ->
                                        (name, cx + dx, cy + dy))
                                    |> List.ofSeq
                                let names = located |> List.map (fun (n, _, _) -> n) |> List.distinct
                                { Plan.Player = player
                                  Plan.Nearby = names
                                  Plan.Entities = located }
                        with _ ->
                            { Plan.Player = player
                              Plan.Nearby = []
                              Plan.Entities = [] }
                    // Trace the decision, including what the available check
                    // said about each action, before advancing.
                    let offered =
                        match p with
                        | Plan.All (Plan.Act(a, _) :: _) -> a
                        | Plan.All (Plan.Branch(_, Plan.Act(a, _), _) :: _) -> a
                        | _ -> "(non-Act head)"
                    let av = available offered
                    match Plan.advanceWith say available view p with
                    | Plan.Stepped(action, rest) ->
                        lock gate (fun () ->
                            planSteps <- planSteps + 1
                            currentPlan <- Some rest
                            planTrace <- "stepped " + action)
                        Some action
                    | Plan.Skipped rest ->
                        lock gate (fun () ->
                            currentPlan <- Some rest
                            planTrace <- "skipped")
                        None
                    | Plan.Unsupported(action, rest) ->
                        lock gate (fun () ->
                            currentPlan <- None
                            planTrace <- "unsupported " + action
                            planNote <- "unsupported action: " + action)
                        None
                    | Plan.Exhausted _ ->
                        lock gate (fun () ->
                            currentPlan <- None
                            planTrace <- "exhausted"
                            planNote <- "repeat limit reached")
                        None
                    | Plan.Finished ->
                        lock gate (fun () ->
                            currentPlan <- None
                            planTrace <- "finished"
                            planNote <- "plan finished")
                        None
            let fromQueue =
                if staged.IsSome || fromPlan.IsSome then None
                else
                    lock gate (fun () ->
                        if pending.Count > 0 then
                            let a = pending.Dequeue()
                            consumed <- consumed + 1
                            Some a
                        else None)
            match staged, fromPlan, fromQueue with
            | Some slot, _, _ ->
                lock gate (fun () -> slot.Consumed <- true)
                let actionId = slot.Action.Value
                result <-
                    match tryDirect player actionId with
                    | Some v -> Some v
                    | None -> commandOf actionId player
                if result.IsNone then lock gate (fun () -> rejected <- rejected + 1)
            | None, Some actionId, _ ->
                result <-
                    match tryDirect player actionId with
                    | Some v -> Some v
                    | None -> commandOf actionId player
                if result.IsNone then lock gate (fun () -> rejected <- rejected + 1)
            | None, None, Some actionId ->
                result <-
                    match tryDirect player actionId with
                    | Some v -> Some v
                    | None -> commandOf actionId player
                if result.IsNone then lock gate (fun () -> rejected <- rejected + 1)
            | None, None, None ->
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
        | Some(name, arg) ->
            // A ValueTuple, explicitly. F#'s ordinary tuple is System.Tuple and
            // C# tests for ValueTuple<string, object>, so the two are not
            // interchangeable: every argument-bearing command -- talk, use, get,
            // move_to -- was dropped here and never reached PushCommand, while
            // argument-free movement worked because it returns a bare string.
            // Counters had already advanced, so the reply looked successful.
            box (ValueTuple<string, obj>(name, arg))
        | None -> box null

    let private currentWaiting () =
        lock gate (fun () ->
            match waiting with
            | Some slot when slot.Action.IsNone && not slot.Consumed -> Some slot
            | _ -> None)

    /// Run a script of actions through the primitive step path.
    ///
    /// There is no separate batch scheduler. This used to exist as its own
    /// scheduler with its own wait, and that was the review's central point:
    /// two owners of the action lifecycle, one of which reported completion
    /// from its own queue rather than from the game. A script is now just
    /// repeated steps, so there is exactly one thing that can be true about
    /// whether an action happened.
    let runScript (actions: string[]) (stallMilliseconds: int) =
        let mutable doneCount = 0
        let mutable rejectedCount = 0
        let mutable left = actions.Length
        let mutable halted = false
        for action in actions do
            if left = 0 then
                halted <- true
            else
                // Claim the current boundary under the same lock every other
                // mutation uses. The claim is what owns the action; there is no
                // separate queue for a batch to drain.
                let claimed =
                    lock gate (fun () ->
                        match waiting with
                        | Some slot when not slot.Consumed && slot.Action.IsNone ->
                            slot.Action <- Some action
                            Some slot
                        | _ -> None)
                match claimed with
                | None ->
                    left <- 0
                    halted <- true
                | Some slot ->
                    // Bounded. No progress expires the script rather than
                    // parking on it, which is what the old scheduler did when
                    // its queue was non-empty and nothing was being consumed.
                    if slot.Onward.Task.Wait(stallMilliseconds) then
                        doneCount <- doneCount + 1
                        left <- left - 1
                    else
                        left <- 0
                        halted <- true
        (doneCount, left, rejectedCount, halted)

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
                // Take a claim, but do not consume the episode yet.
                //
                // The first boundary is published by the game thread once boot
                // finishes, which can be minutes. Blocking on it unbounded, as
                // this did, wedged the whole RPC surface: the wait happened
                // under rpcLock, so a client that timed out and disconnected
                // left a work item parked on `first` and every later request,
                // hello included, blocked behind it. Consuming resetUsed before
                // the wait made it worse -- the retry could not even claim.
                let claimed =
                    lock gate (fun () ->
                        if resetUsed || resetInFlight then false
                        else
                            resetInFlight <- true
                            true)
                if not claimed then
                    fail requestId "unsupported" "This process holds one episode; start another game to reset"
                else
                    let acquired = try first.Task.Wait 30000 with _ -> false
                    let slot =
                        if acquired then
                            try Some(first.Task.GetAwaiter().GetResult()) with _ -> None
                        else None
                    match slot with
                    | Some s ->
                        lock gate (fun () ->
                            resetInFlight <- false
                            resetUsed <- true)
                        ok requestId (transition s.Index s.Turn s.Observation)
                    | None ->
                        lock gate (fun () -> resetInFlight <- false)
                        fail requestId "no_boundary" "No decision boundary published yet; retry reset"
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
            // A script is a sequence of steps, not a second scheduler. The
            // displacement is reported because "consumed" is not "moved": an
            // action the game refused still gets consumed.
            let items =
                if isNull body then failwith "run requires a body"
                else findStringArray "actions" body
            // Validate before any mutation, not after attempting one.
            if items.Length = 0 then
                fail requestId "no_actions" "run requires a non-empty actions array"
            else
                let beforeX, beforeY = playerPosition ()
                let doneCount, left, rejectedCount, halted = runScript items 400
                let afterX, afterY = playerPosition ()
                let completed = left = 0 && doneCount = items.Length && rejectedCount = 0
                let slot = lock gate (fun () -> waiting)
                let obs =
                    match slot with
                    | Some sl when not sl.Consumed -> sl.Observation
                    | _ -> "null"
                ok requestId (sprintf
                    "{\"actions_submitted\":%d,\"actions_consumed\":%d,\"actions_remaining\":%d,\"actions_rejected\":%d,\"completed\":%s,\"interrupted\":%s,\"player_dx\":%s,\"player_dy\":%s,\"observation\":%s}"
                    items.Length doneCount left rejectedCount
                    (if completed then "true" else "false")
                    (if not completed && halted then "true" else "false")
                    (if beforeX <> unknownPos && afterX <> unknownPos then string (afterX - beforeX) else "null")
                    (if beforeY <> unknownPos && afterY <> unknownPos then string (afterY - beforeY) else "null")
                    obs)
        | "plan" ->
            // Submit a program. It runs on the game turn thread from the next
            // boundary, one action per turn, and reports how it ended rather
            // than assuming success: a plan that names an action the world does
            // not offer stops with unsupported_action, because a pushed and
            // ignored action is indistinguishable from one that took effect.
            match findString "program" body with
            | None -> fail requestId "no_program" "plan requires a program string"
            | Some text ->
                try
                    let parsed = Plan.parse text
                    let named = Plan.actions parsed |> List.distinct
                    lock gate (fun () ->
                        currentPlan <- Some parsed
                        planNote <- ""
                        planTrace <- "submitted"
                        planSteps <- 0)
                    ok requestId
                        (sprintf
                            "{\"plan_steps\":%d,\"plan_actions\":%s}"
                            (List.length named)
                            (jsonString (String.concat "," named)))
                with ex ->
                    fail requestId "bad_program" (ex.GetBaseException().Message)
        | "plan_status" ->
            let running, steps, note, trace, pending =
                lock gate (fun () ->
                    currentPlan.IsSome, planSteps, planNote, planTrace,
                    (match currentPlan with
                     | Some p -> String.concat " | " (Plan.actions p)
                     | None -> ""))
            ok requestId
                (sprintf
                    "{\"running\":%s,\"steps_taken\":%d,\"note\":%s,\"trace\":%s,\"pending\":%s}"
                    (if running then "true" else "false")
                    steps
                    (jsonString note)
                    (jsonString trace)
                    (jsonString pending))
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

    /// Release the turn thread from a wait. Called only from the transport
    /// thread; never touches game state.
    let cancel () = lock gate (fun () -> cancelled <- true)

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
                        // No lock around the dispatch. Each mutation is already
                        // claimed under `gate` -- resetUsed for reset,
                        // current.Action for a decision -- so the global lock
                        // added nothing, while a step waiting on the next
                        // boundary held it for the whole wait and blocked
                        // observation and reconciliation behind it.
                        Threading.ThreadPool.QueueUserWorkItem(fun _ ->
                            let response = dispatch text
                            match response with
                            | None -> ()
                            | Some text' ->
                                let bytes = Encoding.UTF8.GetBytes text'
                                try
                                    webSocket.send Text (ArraySegment bytes) true
                                    |> Async.RunSynchronously
                                    |> ignore
                                with _ -> ())
                        |> ignore
                | (Ping, data, _) ->
                    do! webSocket.send Pong (ArraySegment data) true
                | (Close, _, _) ->
                    // A departing client must release the turn thread, or the
                    // patched IdleWait keeps polling for an action nobody will
                    // ever send.
                    cancel ()
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
            // Subscribe now, not at the first decision boundary. The game logs
            // its opening text and the quest prompt before any boundary is
            // published, so a lazy hook misses exactly the lines that matter.
            ensureLogHook ()
            Probe.record diagnosticPath "control listening" |> ignore
