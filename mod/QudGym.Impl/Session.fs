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
        // Whether the body this slot carries was captured while a prompt was open.
        //
        // A prompt is published from inside publish, so the slot's observation is a
        // snapshot of the moment the question was asked. Once the answer is
        // delivered the question is over, but the string still says it is open, and
        // observe -- which serves this string -- would keep reporting a finished
        // dialogue as a live prompt. The flag records that the body is known to
        // describe a prompt, so observe can refuse it instead of serving a stale
        // boundary as though it were current.
        member val PromptBody: bool = false with get, set
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

    // A conversation blocks the game turn thread inside its own key loop, which
    // is how the game itself works: a human types the keys that loop is waiting
    // for. So a conversation cannot be resolved by a staged action on a
    // decision boundary, because the blocked thread is the one that would
    // publish that boundary. The options are published from inside the loop and
    // the answer comes back over the transport, which is a pool thread and does
    // not need the turn thread.
    let mutable answerArrived = TaskCompletionSource<int>()
    let mutable awaitingAnswer = false
    // The options the open popup is offering. They come from the popup call
    // itself, which is the authoritative list; CurrentChoices is still empty at
    // that point, so reading it there would publish a conversation with no
    // options and nothing to answer.
    let mutable offeredOptions : string[] = [||]
    // A menu prompt carries a title and may permit cancelling; a conversation
    // carries neither. Same mechanism, so same state, rather than a second
    // prompt path that could disagree with the first about what is being asked.
    let mutable offeredTitle : string = ""
    let mutable offerCancellable : bool = false
    // Which of those options the game will actually accept, decided by the game
    // and read from the colour it rendered the choice in. A conversation gates
    // choices on reputation and on what the journal holds, and it greys the ones
    // it will refuse; answering a greyed choice is not an error, it is a no-op
    // that leaves the player exactly where they were, having spent a turn.
    let mutable offeredAcceptable : bool[] = [||]
    /// The answer numbers a prompt genuinely offers, in the order they are shown.
    ///
    /// One definition, shared by the action list and by the guard that decides
    /// whether a plan may press an answer. When those were two decisions they
    /// disagreed: the guard asked only whether an id began with "answer:", so it
    /// refused nothing, and `esc` -- which is answer:0 -- was accepted by a
    /// conversation that cannot be cancelled, where 0 is the first *option*
    /// rather than an escape. That is how leaving a conversation pressed a
    /// reputation-gated choice instead.
    ///
    /// A choice the game will refuse is not offered. Naming one is not an error,
    /// but answering it is a no-op that spends a turn, so the list stays a
    /// description of what can actually happen.
    let private answerNumbers (options: string[]) (acceptable: bool[]) (cancellable: bool) =
        let pick =
            options
            |> Array.mapi (fun i _ -> i + 1)
            |> Array.filter (fun n ->
                let i = n - 1
                // A missing verdict is not a refusal. The game told us nothing
                // about this choice, so it stays answerable rather than quietly
                // vanishing from a list the player can see.
                i >= acceptable.Length || acceptable.[i])
        if cancellable then Array.append [| 0 |] pick else pick

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

    /// A JSON string literal.
    ///
    /// Control characters are escaped rather than replaced. Every one of them used
    /// to become a literal '?', which meant a newline -- control character 10 --
    /// silently became a question mark, and any multi-line text arrived at the
    /// client as one run-on line. It looked like the game had joined its own lines:
    /// the quest log's steps read as "Watervine??ù Travel to Red Rock?Journey two
    /// parasangs..." with the boundaries turned into the same '?' that happened to
    /// be sitting in the text. Tabs and carriage returns are escaped for the same
    /// reason; nothing in an observation is a reason to lose a character.
    let private jsonString (value: string) =
        let buf = StringBuilder(value.Length + 2)
        buf.Append('"') |> ignore
        for ch in value do
            match ch with
            | '"' -> buf.Append("\\\"") |> ignore
            | '\\' -> buf.Append("\\\\") |> ignore
            | '\n' -> buf.Append("\\n") |> ignore
            | '\r' -> buf.Append("\\r") |> ignore
            | '\t' -> buf.Append("\\t") |> ignore
            | c when int c < 32 || int c = 127 ->
                buf.Append("\\u").Append((int c).ToString("x4")) |> ignore
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

    let private say (m: string) =
        if logPath <> "" then Probe.record logPath ("talk: " + m) |> ignore
    let private gameAssembly () =
        AppDomain.CurrentDomain.GetAssemblies()
        |> Array.find (fun a -> a.GetName().Name = "Assembly-CSharp")

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

    /// The part of the form type, e.g. the Inventory on the player.
    let private partOfType (target: obj) (typeName: string) =
        let partType = (gameAssembly ()).GetType(typeName, false)
        if isNull partType then null
        else
            try
                let getter =
                    target.GetType().GetMethods(BindingFlags.Instance ||| BindingFlags.Public)
                    |> Array.tryFind (fun m ->
                        m.Name = "GetPart" && m.IsGenericMethodDefinition
                        && m.GetParameters().Length = 0)
                match getter with
                | None -> null
                | Some g -> g.MakeGenericMethod(partType).Invoke(target, [||])
            with _ -> null

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

        // Only verbs the harness can actually perform.
        //
        // quests, journal and history were advertised here while commandOf had no
        // mapping for them, so the action list offered a caller three things that
        // all resolved to a pushed window and consumed no turn. An action the
        // harness advertises but cannot perform is worse than one it omits: the
        // caller has no way to tell the two apart, and the appearance of support
        // is exactly what made the window-blocking look like a game bug rather than
        // a mismatch between two tables.
        //
        // Their content is already in the observation -- the quest list in quests,
        // the journal alongside it, the message log in messages -- so nothing is
        // lost by not offering a verb that opens a window.
        let consoleVerbs =
            [| ("look", "Look"); ("wait", "Wait one turn") |]
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
    /// A name without the game's console markup.
    ///
    /// Names arrive as "{{W|left hand}}" and "{{C|1}} wrench". The markup is how the
    /// console renders a name, not part of it, and a caller is given the text a
    /// player reads -- otherwise no name a caller could type would ever match, and
    /// every equip would be refused for a reason invisible from the outside.
    ///
    /// Markup.Strip does this, and the game already uses it for exactly this purpose
    /// when copying a name to the scrap buffer, so there is no second parser here to
    /// disagree with the console's.
    ///
    /// What does not work, having been tried: Markup.Transform renders rather than
    /// strips, so its output still contains tag-shaped runs, and two attempts to use
    /// it failed identically -- the second after correctly matching its ReadOnlySpan
    /// overload and invoking it. A hand-rolled depth-counted scan was also wrong: it
    /// dropped the entire contents of a tag rather than the tag alone, turning
    /// "{{W|left hand}}" into the empty string.
    let private stripMarkup (text: string) =
        if String.IsNullOrEmpty text then ""
        else
            try
                // Markup.Strip, not Markup.Transform. Transform renders -- it returns
                // markup with console escapes substituted, so its output still
                // contains tag-shaped runs. Strip removes the tags, which is what
                // "the text a player reads" means.
                let markup = (gameAssembly ()).GetType("ConsoleLib.Console.Markup", false)
                let strip =
                    markup.GetMethods(BindingFlags.Static ||| BindingFlags.Public)
                    |> Array.tryFind (fun m ->
                        m.Name = "Strip"
                        && m.GetParameters().Length = 1
                        && m.GetParameters().[0].ParameterType = typeof<string>
                        && m.ReturnType = typeof<string>)
                match strip with
                | Some s ->
                    let result = s.Invoke(null, [| box text |]) :?> string
                    if String.IsNullOrEmpty result then text.Trim() else result.Trim()
                | None -> text.Trim()
            with _ -> text.Trim()

    /// The player's bodypart with this player-visible name, e.g. "left hand".
    ///
    /// The name is BodyPart.GetOrdinalName, the same string the equipment screen
    /// and the game's own messages use. Deliberately not derived from the BodyPart
    /// object, its blueprint or its position hint: a slot the player can read is
    /// the only slot a caller may be given, so nothing here carries an object
    /// identity.
    let private findBodyPartByName (player: obj) (slotName: string) =
        try
            let wanted = slotName.Trim().ToLowerInvariant()
            let body = partOfType player "XRL.World.Parts.Body"
            if isNull body then
                say "no Body part; cannot name equipment slots"
                null
            else
                // Body has four GetParts overloads: two that return void and fill an
                // out-parameter, one that takes EvenIfDismembered, and the
                // parameterless List<BodyPart> this wants. Arity alone does not
                // separate them -- two of them take one argument -- so the return
                // type is tested too, otherwise the void overloads are chosen, come
                // back as null, and read as "no such slot".
                //
                // IsGenericType, not a test on the name. The name is "List`1" with
                // one backtick, and a two-backtick literal does not match it, which
                // made every lookup report that no such method existed.
                let getter =
                    body.GetType().GetMethods(BindingFlags.Instance ||| BindingFlags.Public)
                    |> Array.tryFind (fun m ->
                        m.Name = "GetParts"
                        && m.GetParameters().Length = 0
                        && m.ReturnType.IsGenericType)
                match getter with
                | None ->
                    say "no parameterless Body.GetParts()"
                    null
                | Some g ->
                    match g.Invoke(body, [||]) with
                    | :? System.Collections.IEnumerable as items ->
                        let names = ResizeArray<string>()
                        let mutable found = null
                        for part in items do
                            if not (isNull part) then
                                let ordName =
                                    try
                                        let m =
                                            part.GetType().GetMethod(
                                                "GetOrdinalName",
                                                BindingFlags.Instance ||| BindingFlags.Public)
                                        if isNull m then "" else stripMarkup (m.Invoke(part, [||]) :?> string)
                                    with _ -> ""
                                names.Add(ordName)
                                if isNull found && ordName.ToLowerInvariant() = wanted then
                                    found <- part
                        // A miss is reported with the names that do exist, so an
                        // unreachable action says which spelling would have worked
                        // instead of only that it did not.
                        if isNull found then
                            say
                                (sprintf "no slot named '%s'; the player has: %s"
                                    slotName
                                    (String.concat ", " (List.ofSeq names)))
                        found
                    | _ ->
                        say "Body.GetParts() returned nothing enumerable"
                        null
        with ex ->
            say ("slot lookup failed: " + ex.GetBaseException().Message)
            null

    /// The items that may be equipped into a slot, as the game itself filters them.
    ///
    /// RequirePossible: true is the filter the equipment screen uses, so the
    /// candidate list is exactly the list the player would have been offered. The
    /// grammar's candidates and the game's rules come from one call, which is what
    /// makes an invalid equip unrepresentable rather than merely rejected.
    let private equipCandidates (player: obj) (slotName: string) =
        let inventory = partOfType player "XRL.World.Parts.Inventory"
        let slot = findBodyPartByName player slotName
        if isNull inventory || isNull slot then None
        else
            try
                // The query takes the slot *type* ("Hand"), not the slot name, so
                // the name is resolved to a BodyPart first and its Type is passed on.
                let slotType = memberValue slot "Type" :?> string
                // Four overloads, and arity does not separate them: two take four
                // parameters and two take five, each pair split between one that
                // returns a List and one that fills an out-parameter and returns
                // void. Only the (SlotType, RequireDesirable, RequirePossible,
                // SkipSort) form both takes no ObjectList and returns the list, so
                // that is what is selected. Picking by arity alone had picked a void
                // overload, which returns null and reads as "nothing is equippable".
                let getter =
                    inventory.GetType().GetMethods(BindingFlags.Instance ||| BindingFlags.Public)
                    |> Array.tryFind (fun m ->
                        m.Name = "GetEquipmentListForSlot"
                        && m.GetParameters().Length = 4
                        && m.ReturnType.IsGenericType)
                match getter with
                | None ->
                    say "no GetEquipmentListForSlot(SlotType, RequireDesirable, RequirePossible, SkipSort)"
                    None
                | Some m ->
                    // RequireDesirable: false, RequirePossible: true -- the same
                    // filter the equipment screen applies, so "possible" is the rule
                    // and not "desirable", which would hide items the player may
                    // still legally equip.
                    match m.Invoke(inventory, [| box slotType; box false; box true; box false |]) with
                    | :? System.Collections.IEnumerable as items ->
                        [ for item in items do
                              if not (isNull item) then
                                  match memberValue item "DisplayName" with
                                  | :? string as s -> yield stripMarkup s
                                  | _ -> () ]
                        |> Some
                    | _ -> None
            with ex ->
                say ("equip candidates failed: " + ex.GetBaseException().Message)
                None

    let private messagesJson () =
        let logged = recentLog 12
        "[" + String.concat "," (Array.map jsonString logged) + "]"

    /// The player's open quests, as the quest log itself would list them.
    ///
    /// CmdQuests pushes the QuestLog screen, and a pushed screen blocks the way a
    /// popup does, waiting for a key the harness cannot deliver. So the content is
    /// read rather than drawn, from the same two sources the screen reads: the
    /// game's active Quests, minus anything in FinishedQuests. The text is
    /// QuestLog.GetLinesForQuest -- the game's own formatter, including its ordinal
    /// step ordering, its finished-step markers and its clipping -- so what the
    /// caller reads is what a player reads, rather than a summary of it that could
    /// disagree with the screen about which steps are done.
    ///
    /// Reading the screen's model instead of its pixels is the only honest option
    /// for a browsable surface: there is no call to intercept and no question to
    /// publish, so the alternative would be to open the window and try to dismiss
    /// it, which is the thing that already went wrong.
    let private questsJson () =
        try
            // XRL.The.Game, the same accessor the harness already uses for
            // XRL.The.Player. The quest log's own spelling is
            // XRLCore.Core.Game, and that path does not resolve: XRLCore's Core
            // field is typed XRLCore and exposes no Game member to reflect over, so
            // reading it raised "Specified method is not supported" and every quest
            // read returned empty. The.Game is the working route to the same object.
            let game =
                let the = (gameAssembly ()).GetType("XRL.The", false)
                if isNull the then null
                else
                    let p = the.GetProperty("Game", BindingFlags.Static ||| BindingFlags.Public)
                    if isNull p then null else p.GetValue(null, null)
            if isNull game then "[]"
            else
                // Quests and FinishedQuests are fields on XRLGame, not properties,
                // and both are StringMap<string, Quest>: index by key rather than
                // treating the map as a list.
                let active =
                    game.GetType().GetField("Quests", BindingFlags.Instance ||| BindingFlags.Public)
                        .GetValue(game)
                let finished =
                    game.GetType().GetField("FinishedQuests", BindingFlags.Instance ||| BindingFlags.Public)
                        .GetValue(game)
                let questLog = (gameAssembly ()).GetType("XRL.UI.QuestLog", false)
                // Four parameters (Quest, IncludeTitle, Clip, ClipWidth), all with
                // defaults, so they are passed positionally and explicitly rather
                // than through a binder that will not see the optionals.
                let linesOf =
                    questLog.GetMethods(BindingFlags.Static ||| BindingFlags.Public)
                    |> Array.tryFind (fun m -> m.Name = "GetLinesForQuest" && m.GetParameters().Length = 4)
                if isNull active || isNull finished || linesOf.IsNone then "[]"
                else
                    // Read Values, which returns a ValueEnumerator -- a struct whose
                    // Current is the Quest itself, not a KeyValuePair. The enumerator
                    // is driven by hand with MoveNext/Current, because it is a struct:
                    // F#'s `for` over IEnumerable boxes it and the loop body never
                    // runs, which is how the previous version returned a confident
                    // empty list while a quest was genuinely held.
                    //
                    // Five wrong answers in a row, every one of them silent, which is
                    // why nothing from this type's surface is trusted except Count and
                    // ContainsKey:
                    //
                    //   * Casting to IDictionary throws "Specified method is not
                    //     supported" -- the explicit implementation is generic.
                    //   * foreach yields nothing, because the enumerator is a struct.
                    //   * Testing each element for KeyValuePair<string, object> matched
                    //     nothing; the map yields KeyValuePair<string, Quest>.
                    //   * Item[int] is a field-reflection path and returned the key
                    //     where a Quest was expected, so Quest.ID read back as the
                    //     quest's display name.
                    //   * Quest.ID and XRLGame.Quests are fields, not properties, so
                    //     reading them through the property API found nothing.
                    //
                    // The lesson is the same one the equipment work produced: the
                    // game's types are richer than they look, and a reflection call
                    // that returns null is a question, not an answer.
                    let values (map: obj) =
                        try
                            let valuesProp =
                                map.GetType().GetProperty("Values", BindingFlags.Instance ||| BindingFlags.Public)
                            if isNull valuesProp then []
                            else
                                match valuesProp.GetValue(map, null) with
                                | null -> []
                                | enumerator ->
                                    let t = enumerator.GetType()
                                    let moveNext = t.GetMethod("MoveNext", BindingFlags.Instance ||| BindingFlags.Public)
                                    let current = t.GetProperty("Current", BindingFlags.Instance ||| BindingFlags.Public)
                                    if isNull moveNext || isNull current then []
                                    else
                                        let collected = ResizeArray<obj>()
                                        // Bounded: a map this small cannot need more,
                                        // and an unbounded loop on a game-owned type
                                        // is a hazard rather than a style question.
                                        let mutable steps = 0
                                        while steps < 256 do
                                            steps <- steps + 1
                                            let moved = moveNext.Invoke(enumerator, [||]) :?> bool
                                            if not moved then
                                                steps <- 256
                                            else
                                                let v = current.GetValue(enumerator, null)
                                                if not (isNull v) then collected.Add(v)
                                        List.ofSeq collected
                        with _ -> []
                    let containsKey (map: obj) (key: string) =
                        match map.GetType().GetMethods(BindingFlags.Instance ||| BindingFlags.Public)
                              |> Array.tryFind (fun m ->
                                  m.Name = "ContainsKey" && m.GetParameters().Length = 1
                                  && m.GetParameters().[0].ParameterType = typeof<string>) with
                        | Some m -> m.Invoke(map, [| box key |]) :?> bool
                        | None -> false
                    // Finished quests are excluded by identity, so a quest is neither
                    // listed twice nor listed after it is complete. The quest's own ID
                    // is the key the finished map is asked about, which is exactly the
                    // comparison QuestLog makes. A field, not a property.
                    let questKey (q: obj) =
                        try
                            let f = q.GetType().GetField("ID", BindingFlags.Instance ||| BindingFlags.Public)
                            if isNull f then "" else f.GetValue(q) :?> string
                        with _ -> ""
                    let entries =
                        values active
                        |> List.filter (fun q ->
                            not (isNull q)
                            && not (
                                let key = questKey q
                                not (String.IsNullOrEmpty key) && containsKey finished key))
                    let blocks = ResizeArray<string>()
                    for (q: obj) in entries do
                        match linesOf with
                        | None -> ()
                        | Some format ->
                            match format.Invoke(null, [| q; box true; box true; box 74 |]) with
                            | :? System.Collections.IEnumerable as lines ->
                                let collected = ResizeArray<string>()
                                for (line: obj) in lines do
                                    match line with
                                    | :? string as s -> collected.Add(stripMarkup s)
                                    | _ -> collected.Add("")
                                if collected |> Seq.exists (fun s -> s <> "") then
                                    blocks.Add(String.concat "\n" collected)
                            | _ -> ()
                    "[" + String.concat "," (blocks |> Seq.map jsonString) + "]"
        with ex ->
            say ("quest read failed: " + ex.GetBaseException().Message)
            "[]"

    /// Whether a name picks this item out of a list, the way the game reads a typed
    /// name.
    ///
    /// Exact equality on the visible name is not how a player selects a thing. The
    /// real name of a quest item is qualified -- "waterskin [32 drams of fresh water]",
    /// "torch x11 (unburnt)" -- so requiring the caller to reproduce the quantity and
    /// the fill level would make the grammar depend on transient state, and a name
    /// typed from one observation would stop matching after a sip of water. The
    /// unqualified stem is what identifies the item; the rest is its current state,
    /// which the player sees but does not name.
    ///
    /// A stem that is a prefix of the name is a match, so "waterskin" selects
    /// "waterskin [empty]". This is deliberately the same leniency the console uses
    /// for "get torch": a short unambiguous stem beats demanding the full string. It
    /// is not a substring search over arbitrary text -- the stem must be a leading
    /// run of the name, so "skin" does not select a waterskin.
    let private nameSelects (typed: string) (candidateName: string) =
        let wanted = typed.Trim().ToLowerInvariant()
        let actual = stripMarkup candidateName
        if wanted = "" then false
        elif actual.ToLowerInvariant() = wanted then true
        elif actual.Length > wanted.Length && actual.ToLowerInvariant().StartsWith(wanted) then
            // Only when the character after the stem closes the name, so "water"
            // does not select "waterskin" while "waterskin" still does not match
            // "waterson". Bracketed and counted suffixes both start with a
            // separator, which is what makes the boundary unambiguous.
            let boundary = actual.[wanted.Length]
            Char.IsWhiteSpace boundary || boundary = '[' || boundary = '('
        else false

    /// A carried item this name selects, or null.
    ///
    /// Only what the player is carrying. No object identity, blueprint or inventory
    /// handle is returned, and the search is over the player's own inventory rather
    /// than every object in the zone, so an item the player cannot see is never
    /// equippable here. Ambiguity is a refusal, not a coin flip: if the name selects
    /// more than one carried item the action is rejected and both are reported, the
    /// same rule extraction follows for a reason that has nothing to do with
    /// confidence scoring.
    let private findCarriedByName (player: obj) (itemName: string) =
        try
            if String.IsNullOrWhiteSpace itemName then null
            else
                let inventory = partOfType player "XRL.World.Parts.Inventory"
                if isNull inventory then null
                else
                    // GetObjectsReadonly, not GetObjects: the equipment query and
                    // this search then agree on the same set, and nothing here can
                    // mutate the inventory while resolving a name.
                    let getter =
                        inventory.GetType().GetMethods(BindingFlags.Instance ||| BindingFlags.Public)
                        |> Array.tryFind (fun m ->
                            m.Name = "GetObjectsReadonly" && m.GetParameters().Length = 0)
                    match getter with
                    | None -> null
                    | Some g ->
                        match g.Invoke(inventory, [||]) with
                        | :? System.Collections.IEnumerable as items ->
                            let hits = ResizeArray<obj * string>()
                            for item in items do
                                if not (isNull item) then
                                    let name =
                                        match memberValue item "DisplayName" with
                                        | :? string as s -> stripMarkup s
                                        | _ -> ""
                                    if nameSelects itemName name then hits.Add(item, name)
                            // Annotated as obj so each arm is a plain null rather
                            // than unifying the match into an option type.
                            match hits.Count with
                            | 1 -> box (fst hits.[0]) :> obj
                            | 0 -> null
                            | _ ->
                                say
                                    (sprintf "'%s' is ambiguous; you carry: %s"
                                        itemName
                                        (String.concat ", " [ for _, n in hits -> n ]))
                                null
                        | _ -> null
        with ex ->
            say ("carried lookup failed: " + ex.GetBaseException().Message)
            null

    let private show (v: obj) =
        if isNull v then "" else v.ToString()

    /// The live conversation, if one is open, as prompt text and its options.
    ///
    /// The player is meant to choose from the options the game offers, so they
    /// are read from ConversationUI.CurrentChoices and published as the prompt
    /// rather than summarised. The list is the game's, so a caller cannot be
    /// offered an answer the conversation does not contain.
    let private conversationPrompt () =
        try
            let ui = (gameAssembly ()).GetType("XRL.UI.ConversationUI", false)
            if isNull ui then None
            else
                let choices = ui.GetField("CurrentChoices", BindingFlags.Static ||| BindingFlags.Public)
                match choices.GetValue(null) with
                | :? System.Collections.IList as list when list.Count > 0 ->
                    let mutable options = [||]
                    for i in 0 .. list.Count - 1 do
                        let display =
                            list.[i].GetType().GetMethod("GetDisplayText", BindingFlags.Instance ||| BindingFlags.Public)
                        let text =
                            try
                                match display.GetParameters().Length with
                                | 0 -> show (display.Invoke(list.[i], [||]))
                                | _ -> show (display.Invoke(list.[i], [| box false |]))
                            with _ -> ""
                        options <- Array.append options [| text |]
                    // The conversation's own opening line, when it has one.
                    let speaker =
                        let current = ui.GetField("CurrentConversation", BindingFlags.Static ||| BindingFlags.Public)
                        match current.GetValue(null) with
                        | null -> ""
                        | c ->
                            let nm = c.GetType().GetProperty("Speaker", BindingFlags.Instance ||| BindingFlags.Public)
                            try show (nm.GetValue(c, null)) with _ -> ""
                    Some(speaker, options)
                | _ -> None
        with _ -> None

    /// Answer a conversation by feeding its own key loop.
    ///
    /// A conversation reads keys through Keyboard.getvk and only then calls
    /// ConversationUI.Select. So the reply is a pushed key, not a direct call:
    /// the game receives it exactly as it receives a human's, and the option is
    /// chosen by the game's own code. Calling Select from here instead would
    /// race the loop that is already blocked waiting for that key.
    ///
    /// The first nine options are the digit keys, which is how the game labels
    /// them; beyond that it switches to letters.
    /// Push a named key through the game's own input queue.
    let private pressNamedKey (name: string) =
        try
            let keyboard = (gameAssembly ()).GetType("ConsoleLib.Console.Keyboard", false)
            let keyCode = (gameAssembly ()).GetType("UnityEngine.KeyCode", false)
            let pushKey = keyboard.GetMethod("PushKey", [| keyCode |])
            let field = keyCode.GetField(name, Reflection.BindingFlags.Static ||| Reflection.BindingFlags.Public)
            if isNull pushKey || isNull field then
                say ("no key named " + name)
                false
            else
                pushKey.Invoke(null, [| field.GetValue(null) |]) |> ignore
                true
        with ex ->
            say ("press " + name + " failed: " + ex.GetBaseException().Message)
            false

    let private pressAnswerKey (index: int) =
        try
            let keyboard = (gameAssembly ()).GetType("ConsoleLib.Console.Keyboard", false)
            if isNull keyboard then false
            else
                let keyCode = (gameAssembly ()).GetType("UnityEngine.KeyCode", false)
                let pushKey = keyboard.GetMethod("PushKey", [| keyCode |])
                if isNull pushKey || isNull keyCode then false
                else
                    // D1..D9 for the first nine, then A.. as the game does.
                    let name =
                        if index < 9 then "D" + string (index + 1)
                        else "A" + string (char (int 'A' + index - 9))
                    let field = keyCode.GetField(name, Reflection.BindingFlags.Static ||| Reflection.BindingFlags.Public)
                    if isNull field then
                        say ("no key for option " + string (index + 1))
                        false
                    else
                        pushKey.Invoke(null, [| field.GetValue(null) |]) |> ignore
                        true
        with ex ->
            say ("press failed: " + ex.GetBaseException().Message)
            false

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
        // A conversation is the one place the player must choose from a list the
        // game builds, so the options are published verbatim as a prompt and the
        // only actions offered are the answers to it. A caller is therefore never
        // offered an answer the conversation does not contain.
        let conversation =
            let fromPopup =
                lock gate (fun () ->
                    if offeredOptions.Length > 0 then
                        let speaker =
                            let ui = (gameAssembly ()).GetType("XRL.UI.ConversationUI", false)
                            let current =
                                if isNull ui then null
                                else ui.GetField("CurrentConversation", BindingFlags.Static ||| BindingFlags.Public).GetValue(null)
                            if isNull current then ""
                            else
                                let nm = current.GetType().GetProperty("Speaker", BindingFlags.Instance ||| BindingFlags.Public)
                                try show (nm.GetValue(current, null)) with _ -> ""
                        Some(speaker, offeredTitle, offeredOptions, offeredAcceptable, offerCancellable)
                    else None)
            match fromPopup with
            | some when some.IsSome -> some
            | _ ->
                // The legacy path reads the open conversation's choices rather
                // than the call that raised it. A conversation has no title and
                // cannot be cancelled, so it is normalised into the same shape.
                // It carries no verdicts either, because the colours were read from
                // the call and this path does not have it.
                match conversationPrompt () with
                | Some(speaker, options) -> Some(speaker, "", options, [||], false)
                | None -> None
        let promptJson =
            match conversation with
            | None -> "null"
            | Some (speaker, title, options, acceptable, cancellable) ->
                let body =
                    if options.Length > 0 then String.concat " / " options else "(no options yet)"
                let heading =
                    if title = "" then speaker
                    elif speaker = "" then title
                    else title + " -- " + speaker
                // Which positions the game will refuse, 1-based to match the
                // numbering of the options above. Published because "the game will
                // not accept this" is part of what was asked: a conversation greys
                // the choices that cost more reputation than the player holds, and
                // answering one is not a refusal but a no-op that spends a turn.
                let refused =
                    options
                    |> Array.mapi (fun i _ -> i + 1)
                    |> Array.filter (fun n -> n - 1 < acceptable.Length && not acceptable.[n - 1])
                sprintf
                    "{\"kind\":\"choice\",\"text\":%s,\"options\":[%s],\"allow_cancel\":%b,\"unavailable\":[%s]}"
                    (jsonString (if heading = "" then body else heading + ": " + body))
                    (String.concat "," (Array.map jsonString options))
                    cancellable
                    (String.concat "," (Array.map string refused))
        let actions =
            match conversation with
            | Some (_, _, options, acceptable, cancellable) when options.Length > 0 ->
                // Only what answerNumbers says is on offer, so the list cannot
                // offer a choice the game will refuse or a cancel it forbids.
                let answers =
                    answerNumbers options acceptable cancellable
                    |> Array.map (fun n ->
                        if n = 0 then
                            "{\"id\":\"answer:0\",\"kind\":\"answer\",\"label\":\"(cancel)\",\"arguments\":{\"option\":0}}"
                        else
                            sprintf
                                "{\"id\":%s,\"kind\":\"answer\",\"label\":%s,\"arguments\":{\"option\":%d}}"
                                (jsonString ("answer:" + string n))
                                (jsonString options.[n - 1])
                                n)
                "[" + (String.concat "," answers) + "]"
            | _ -> actionsJsonSafe player
        let phase = if conversation.IsSome then "prompt" else "command"
        id, sprintf
            "{\"episode_id\":%s,\"decision_id\":%s,\"turn\":%d,\"phase\":%s,\"player\":{\"x\":%d,\"y\":%d,\"hp\":%d,\"max_hp\":%d},\"view\":{\"radius\":%d,\"zone_width\":%d,\"zone_height\":%d},\"tiles\":[%s],\"entities\":%s,\"messages\":%s,\"prompt\":%s,\"actions\":%s,\"quests\":%s}"
            (jsonString episode) (jsonString id) turn (jsonString phase) x y hp maxHp radius width height tiles entities messages promptJson actions (questsJson ())

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
        // Recorded from the state that produced the body, not parsed back out of
        // it: the body is a string, and re-reading it to decide whether it is
        // current is how the staleness happened in the first place.
        slot.PromptBody <- lock gate (fun () -> awaitingAnswer)
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
        else if action = "space" || action = "continue" then
            // Dismiss a modal that is waiting for a keypress.
            //
            // Granting a quest shows a dialog that blocks on the keyboard until
            // something is pressed, and a player presses space. Nothing in the
            // harness sent one, so the game sat waiting and the quest looked
            // unfinished -- the dialog, not the quest, was the blocker. Keyed
            // actions go through the game's own input queue for the same reason
            // conversation answers do.
            if pressNamedKey "Space" then Some("CmdNone", box null) else None
        else if action.StartsWith("answer:") then
            let raw = action.Substring(7).Trim()
            match Int32.TryParse raw with
            | false, _ -> None
            | true, n when n >= 1 ->
                // Refuse an answer the open prompt is not offering, for the same
                // reason the plan's own answers are checked: a choice the game will
                // refuse is not an error, it is a no-op that spends a turn.
                //
                // Only while a prompt is actually open. An answer that arrives
                // between prompts has no list to be checked against, and the
                // game's own key path is still the right thing to try -- that is
                // how an answer is delivered when the wait has not started yet.
                let onOffer =
                    lock gate (fun () ->
                        not (offeredOptions.Length > 0)
                        || (answerNumbers offeredOptions offeredAcceptable offerCancellable
                            |> Array.contains n))
                if not onOffer then
                    say ("answer:" + string n + " is not on offer; refusing")
                    None
                else
                    let delivered =
                        lock gate (fun () ->
                            if awaitingAnswer then
                                answerArrived.TrySetResult (n - 1) |> ignore
                                true
                            else false)
                    if delivered then Some("CmdNone", box null)
                    elif pressAnswerKey (n - 1) then Some("CmdNone", box null)
                    else None
            // answer:0 is the cancel, and the game's own value for it is -1.
            | true, 0 ->
                let cancellable = lock gate (fun () -> offerCancellable || offeredOptions.Length = 0)
                if not cancellable then
                    say "answer:0 asked for a cancel this prompt does not offer; refusing"
                    None
                else
                    let delivered =
                        lock gate (fun () ->
                            if awaitingAnswer then
                                answerArrived.TrySetResult -1 |> ignore
                                true
                            else false)
                    if delivered then Some("CmdNone", box null) else None
            | true, _ -> None
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
        // 'quests' is deliberately absent. It used to map to CmdQuests, which pushes
        // the QuestLog screen, and a pushed screen blocks on a keypress exactly as a
        // popup does -- so asking what quest the player was on left a window open and
        // consumed no turn. The quest list is part of the observation now, so the
        // question is answered by looking rather than by opening something.
        // 'journal' and 'history' are absent for the same reason 'quests' is.
        //
        // Both push a window: CmdJournal sets Screens.CurrentScreen and calls
        // Screens.Show, and CmdMessageHistory calls Messages.Show. A pushed window
        // blocks exactly as a popup does, and the turn thread parks inside it, so
        // every later action stops being consumed. Verified rather than assumed:
        // after 'journal' a following 'west' consumed 0, and after 'history' the
        // same. The journal is a tab of the screen quests now comes from, and the
        // message log is already in the observation's messages, so in both cases
        // the content was already readable and only the blocking was left over.
        elif action.StartsWith("wield:") then
            // wield:<item> -- equip without naming a slot, which is what makes the
            // game ask which slot.
            //
            // EquipmentScreen always names the slot, because the player picked it on
            // the screen. The harness can name it too, so this fires
            // CommandEquipObject with no BodyPart, and the game takes the branch at
            // Inventory.cs:1751: with the slot unspecified it asks, via
            // Popup.PickOption, which slot the item should go in. That question is
            // published rather than drawn, by the menu gate.
            //
            // So the two features are one loop: equip with a slot needs no menu, and
            // equip without one is exactly what produces a menu to answer. This is
            // also the honest way to reach the gate -- the case is provoked by the
            // game's own logic, not by a test-only entry point.
            let itemName = action.Substring(6).Trim()
            match findCarriedByName player itemName with
            | null -> None
            | item ->
                try
                    let eventType = (gameAssembly ()).GetType("XRL.World.Event", false)
                    let ctor =
                        eventType.GetConstructor(
                            [| typeof<string>; typeof<string>; typeof<obj> |])
                    // The three-argument form sets a single parameter. BodyPart is
                    // deliberately absent: supplying it would skip the question.
                    let evt = ctor.Invoke([| box "CommandEquipObject"; box "Object"; box item |])
                    let fire =
                        player.GetType().GetMethod(
                            "FireEvent",
                            BindingFlags.Instance ||| BindingFlags.Public,
                            null,
                            [| eventType |],
                            null)
                    fire.Invoke(player, [| evt |]) |> ignore
                    Some("CmdNone", box null)
                with ex ->
                    say ("wield failed: " + ex.GetBaseException().Message)
                    None
        elif action.StartsWith("equip:") then
            // equip:<slot>:<item>, both named the way the player reads them.
            //
            // The three steps are the game's own, in the order EquipmentScreen
            // uses them: ask the game which items may go in this slot, then fire
            // the event it fires when the player picks one. No picker is opened and
            // no keystroke is synthesized, so this cannot be refused by
            // TutorialManager.AllowPushKey the way a key into a live popup is.
            //
            // Both names are resolved before anything is fired, and a name the
            // player could not have read is a refusal rather than a guess: the
            // candidate list comes from GetEquipmentListForSlot, so the item named
            // here is either in the set the game would have offered or it is not
            // equippable and the action is rejected as such.
            let rest = action.Substring(6)
            let split = rest.IndexOf ':'
            if split <= 0 then
                say "equip needs equip:<slot>:<item>"
                None
            else
                let slotName = rest.Substring(0, split).Trim()
                let itemName = rest.Substring(split + 1).Trim()
                match equipCandidates player slotName with
                | None ->
                    say ("no slot '" + slotName + "' you can read, or it takes nothing")
                    None
                | Some candidates ->
                    let selectable =
                        candidates |> List.filter (fun c -> nameSelects itemName c)
                    match selectable with
                    | [] ->
                        say
                            (sprintf "'%s' is not equippable to '%s'; the game offers: %s"
                                itemName slotName
                                (if candidates.IsEmpty then "(nothing)"
                                 else String.concat ", " candidates))
                        None
                    // Two candidates the name selects is ambiguous, not first-wins.
                    // Picking one would make the outcome depend on the order the
                    // game's query happened to return, which is not information a
                    // caller can see or reason about. The rule matches extraction:
                    // two readings of the same intent are a refusal.
                    | [ _; _ ] as several ->
                        say
                            (sprintf "'%s' is ambiguous for '%s'; it could mean: %s"
                                itemName slotName (String.concat ", " several))
                        None
                    | [ chosen ] ->
                        try
                            let slot = findBodyPartByName player slotName
                            // Matched on the same stripped text the candidate list was
                            // matched on, so the item found here is the item validated
                            // there rather than a second, subtly different lookup.
                            let item = findCarriedByName player (stripMarkup chosen)
                            if isNull item then
                                say ("not carrying '" + chosen + "'")
                                None
                            elif isNull slot then
                                say ("no slot '" + slotName + "'")
                                None
                            else
                                // CommandEquipObject with a BodyPart named, which
                                // is the branch that does not open a menu. The
                                // five-argument constructor takes both parameters
                                // directly, which is how the game itself builds
                                // this event, so nothing is set afterwards and
                                // there is no partially-built event to fire.
                                let eventType = (gameAssembly ()).GetType("XRL.World.Event", false)
                                let ctor =
                                    eventType.GetConstructor(
                                        [| typeof<string>; typeof<string>; typeof<obj>;
                                           typeof<string>; typeof<obj> |])
                                let evt =
                                    ctor.Invoke(
                                        [| box "CommandEquipObject"; box "Object"; box item
                                           box "BodyPart"; box slot |])
                                // FireEvent(Event) is chosen by parameter type rather
                                // than arity: FireEvent has six overloads, five of
                                // them taking two arguments.
                                let fire =
                                    player.GetType().GetMethod(
                                        "FireEvent",
                                        BindingFlags.Instance ||| BindingFlags.Public,
                                        null,
                                        [| eventType |],
                                        null)
                                fire.Invoke(player, [| evt |]) |> ignore
                                say (sprintf "equipped %s to %s" (stripMarkup chosen) (stripMarkup slotName))
                                Some("CmdNone", box null)
                        with ex ->
                            say ("equip failed: " + ex.GetBaseException().Message)
                            None
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

    /// One step of a conversation, run in place of ConversationUI.Input.
    ///
    /// Publishes the options the game is offering as a decision boundary, then
    /// waits, bounded, for the answer to arrive over the transport. Returns the
    /// chosen index, or -1 to let the game handle input itself.
    /// Publish a prompt and wait for the answer.
    ///
    /// One mechanism for every prompt the game raises through a call: a
    /// conversation (no title, no cancelling) and a PickOption menu (title, and
    /// cancelling exactly when the game would allow it). `fallback` is the answer
    /// used if nobody replies, which is the value the game itself would have
    /// started from: -1 for a conversation, and the menu's own default selection
    /// for a menu, because -1 is not a legal answer to a menu that forbids escape.
    let conversationTurn (player: obj) (turn: int) (timeoutMilliseconds: int) (title: string)
        (intro: string) (allowEscape: bool) (fallback: int) (options: string[]) (acceptable: bool[]) =
        let signal = TaskCompletionSource<int>()
        lock gate (fun () ->
            answerArrived <- signal
            awaitingAnswer <- true
            offeredOptions <- options
            offeredTitle <- title
            offerCancellable <- allowEscape
            offeredAcceptable <- acceptable)
        try
            // Publish what the player is being asked. The observation carries
            // the options and offers only the answers to them.
            publish player turn |> ignore
            // Let a plan answer its own conversation.
            //
            // A prompt is published from inside this function, not from supply, so
            // the plan -- which is consulted only by supply -- never saw it. A plan
            // could walk to a target, talk, and then sit at the first question
            // forever, which is why conversations were being driven from outside by
            // hand.
            //
            // So when the plan's next step is an answer, it is taken here, from the
            // same thread that is already blocked waiting for one. An answer is
            // delivered by setting the result, not by pushing a key, so this does
            // not re-enter the transport.
            let answered =
                let plan = lock gate (fun () -> currentPlan)
                match plan with
                | Some p ->
                    // The answers this prompt is actually offering, so a plan naming
                    // one that is not on offer does not get to press it.
                    //
                    // This used to test only that the id began with "answer:", which
                    // every answer does, so it refused nothing. A plan's `esc` is
                    // answer:0, and this path parsed the number without the n >= 1
                    // rule the transport path enforces, so 0 became index 0 -- the
                    // first option of a conversation that cannot be cancelled. The
                    // plan asked to leave and the game was handed a choice.
                    let offered a =
                        let options, acceptable, cancellable =
                            lock gate (fun () ->
                                offeredOptions, offeredAcceptable, offerCancellable)
                        let onOffer = answerNumbers options acceptable cancellable
                        if a = Plan.AvailableAction then
                            // The one the game will take. Resolved against the
                            // options it just published, and refused when that is
                            // not a single answer: two candidates is ambiguous,
                            // and choosing between them silently would be
                            // indistinguishable from knowing which was right.
                            let acceptable =
                                onOffer |> Array.filter (fun n -> n > 0)
                            match acceptable with
                            | [| only |] -> Plan.Pressed("answer:" + string only)
                            | [||] ->
                                Plan.Refused(
                                    "no option the game will accept; it offered "
                                    + string options.Length
                                    + (if options.Length = 1 then " option" else " options")
                                    + " and refused all of them")
                            | many ->
                                Plan.Refused(
                                    "the game will accept "
                                    + string many.Length
                                    + " of its options ("
                                    + (many |> Array.map string |> String.concat ", ")
                                    + ") and the plan does not say which")
                        else
                            match Int32.TryParse(if a.StartsWith("answer:") then a.Substring(7) else "") with
                            | true, n when Array.contains n onOffer -> Plan.Pressed a
                            | true, _ -> Plan.Refused("the game is not offering " + a)
                            | _ -> Plan.Refused("the game is not offering " + a)
                    let view: Plan.View =
                        { Plan.Player = player
                          Plan.Nearby = []
                          Plan.Entities = [] }
                    match Plan.advance offered view p with
                    | Plan.Stepped(action, rest) when action.StartsWith("answer:") ->
                        let index = Plan.gameIndex (Int32.Parse(action.Substring(7)))
                        say ("plan answers " + action)
                        lock gate (fun () -> currentPlan <- Some rest)
                        Some index
                    | _ -> None
                | None -> None
            match answered with
            // The index is returned directly rather than through the signal: the
            // transport is the only other writer, and waiting on a task this thread
            // is itself about to satisfy is a race with a thread that is not running.
            | Some index -> index
            | None ->
                if signal.Task.Wait timeoutMilliseconds then
                    signal.Task.GetAwaiter().GetResult()
                else
                    say "prompt answer timed out"
                    fallback
        finally
            lock gate (fun () ->
                awaitingAnswer <- false
                offeredOptions <- [||]
                offeredTitle <- ""
                offerCancellable <- false)

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
                        // Resolved, not merely tested: "answer:avail" has no answer
                        // until a prompt is open, and the command phase is not one.
                        // Naming it here reports the step as unavailable rather
                        // than stepping an id the game cannot use.
                        if a = Plan.AvailableAction then
                            Plan.Refused(
                                "no prompt is open, so there is no option to take; 'avail' answers a conversation")
                        elif (match tryDirect player a with
                              | Some _ -> true
                              | None -> commandOf a player |> Option.isSome) then
                            Plan.Pressed a
                        else
                            Plan.Refused("the action space is not offering " + a)
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
                    // The plan asked for something the world does not offer, and the
                    // reason says which names it does. Recorded rather than dropped,
                    // because a goto whose target is not published used to end the
                    // whole program quietly and look like a plan that had finished.
                    | Plan.Unavailable(reason, _) ->
                        lock gate (fun () ->
                            currentPlan <- None
                            planTrace <- "unavailable " + reason
                            planNote <- reason)
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
        // Rejected means staged and then unresolvable: the action reached a
        // boundary and no command came of it.
        //
        // This counter was declared and never incremented, so every script reported
        // rejected=0 and a caller could not distinguish an action that happened
        // from one that was silently dropped. "journal" reported consumed=1
        // rejected=0 while opening a window and taking no turn, and an unknown
        // action like "zzz-not-an-action" reported exactly the same. A number that
        // is always zero is worse than no number, because it looks like a fact.
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
                | Some slot when action.StartsWith("talk:") ->
                    // A conversation is a question, and the answer cannot come from
                    // here.
                    //
                    // Everything else is claimed, executed and then waited for: the
                    // next boundary means the action happened. A conversation is not
                    // like that. tryDirect runs on the game turn thread, the game's
                    // own HaveConversation is synchronous, and it reaches the popup
                    // hook, which blocks waiting for an answer. The only way to answer
                    // is the transport thread's answer op -- and this thread is the
                    // one that would deliver it.
                    //
                    // So waiting here is a deadlock with a 120 second fuse: run never
                    // returns, the caller never gets to answer, the conversation times
                    // out, and afterwards the log reads "no conversation started" for a
                    // conversation that started, ran, published three options and
                    // finished. Then the fallthrough starts a second one.
                    //
                    // The action is claimed and the script returns immediately. The
                    // conversation publishes its own boundary from inside itself, which
                    // the caller observes and answers, so the loop is caller-driven
                    // exactly as it should be for a prompt.
                    doneCount <- doneCount + 1
                    left <- left - 1
                | Some slot ->
                    // Bounded. No progress expires the script rather than
                    // parking on it, which is what the old scheduler did when
                    // its queue was non-empty and nothing was being consumed.
                    if slot.Onward.Task.Wait(stallMilliseconds) then
                        // The next boundary means the action was taken, not that it
                        // was understood. An action neither tryDirect nor commandOf
                        // recognises consumes a boundary and does nothing, so the
                        // difference is read back off the slot here rather than
                        // inferred from the fact that the turn advanced.
                        let resolved =
                            match slot.Action with
                            | Some a -> tryDirect (livePlayer ()) a |> Option.isSome
                                          || commandOf a (livePlayer ()) |> Option.isSome
                            | None -> true
                        if resolved then
                            doneCount <- doneCount + 1
                        else
                            rejectedCount <- rejectedCount + 1
                        left <- left - 1
                    else
                        left <- 0
                        halted <- true
        (doneCount, left, rejectedCount, halted)

    let private handleOp requestId op body =
        match op with
        | "answer" ->
            // Answer an open conversation directly, from the transport thread.
            //
            // This cannot go through the step path. A conversation is blocking
            // the game turn thread inside its own wait, so supply -- which is
            // what every other action is delivered through -- cannot run to pick
            // it up. The waiting conversation is signalled here instead, and the
            // game's own code then advances the conversation with that choice.
            let choice =
                match findInt "option" body with
                | Some n -> Some n
                | None ->
                    match findString "action_id" body with
                    | Some a when a.StartsWith("answer:") ->
                        match Int32.TryParse(a.Substring(7).Trim()) with
                        | true, n -> Some n
                        | _ -> None
                    | _ -> None
            match choice with
            | None -> fail requestId "no_option" "answer requires an option number"
            | Some n ->
                let delivered =
                    lock gate (fun () ->
                        if awaitingAnswer then
                            // 0 and below mean cancel. Only offered when the game
                            // permits it, so a prompt that forbids escape cannot be
                            // talked out of; -1 is the value the game itself returns
                            // for a cancelled menu.
                            if n <= 0 then
                                if offerCancellable then
                                    answerArrived.TrySetResult -1 |> ignore
                                    true
                                else false
                            else
                                answerArrived.TrySetResult (n - 1) |> ignore
                                true
                        else false)
                if delivered then ok requestId (sprintf "{\"answered\":%d}" n)
                else
                    fail requestId
                        "no_prompt"
                        (if n <= 0 then "This prompt cannot be cancelled" else "No prompt is waiting for an answer")
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
                let promptIsStale (slot: Slot) =
                    slot.PromptBody && not (lock gate (fun () -> awaitingAnswer))
                let rec awaitBoundary (slot: Slot) (budget: int) =
                    // A prompt body is served only while the prompt is open.
                    //
                    // The body is a snapshot from when the question was asked, so
                    // once the answer is delivered it describes a dialogue that has
                    // already advanced. Serving it then is what reported a finished
                    // conversation as a live prompt, five times over.
                    //
                    // The fix is to wait for the boundary that is genuinely coming
                    // rather than to refuse. After an answer the game's turn
                    // resumes, supply publishes the next boundary, and that boundary
                    // carries the state after the answer -- so refusing sent callers
                    // into a poll loop to fetch something the harness could simply
                    // have handed them. Bounded, because "the next boundary" is a
                    // promise about the game's own loop and not a licence to hang:
                    // if the game is not going to produce one, this must say so
                    // rather than wait forever.
                    if not (promptIsStale slot) then Some slot
                    elif budget <= 0 then None
                    elif slot.Onward.Task.Wait(50) then
                        awaitBoundary slot.Onward.Task.Result (budget - 50)
                    else None
                match currentWaiting () with
                | Some slot ->
                    match awaitBoundary slot 5000 with
                    | Some fresh -> ok requestId fresh.Observation
                    | None ->
                        fail requestId
                            "stale_decision"
                            "That prompt has been answered and no new boundary has arrived"
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
