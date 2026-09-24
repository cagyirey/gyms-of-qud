namespace QudGym

open System
open System.Collections.Generic
open System.IO
open System.Net
open System.Net.Sockets
open System.Reflection
open System.Text
open System.Threading.Tasks

open Suave
open Suave.Filters
open Suave.Operators
open Suave.RequestErrors
open Suave.Sockets
open Suave.Sockets.Control
open Suave.WebSocket

// Live reset/step endpoint. The game thread blocks in Keyboard.IdleWait, which is
// the command read. This publishes one observation and waits for a single action.
module Session =
    type private Slot = {
        id: string
        index: int
        turn: int
        observation: string
        action: TaskCompletionSource<string>
        onward: TaskCompletionSource<Slot>
    }

    let private gate = obj ()
    let private cache = Dictionary<string, string * string>()

    let mutable private waiting : Slot option = None
    let mutable private decisions = 0
    let mutable private resetUsed = false
    let mutable private listening = false
    let mutable private logPath = ""
    let mutable private gameBuild = "unknown"

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

    let private commandOf action =
        if action = "wait" then Some "CmdWait"
        else
            let prefix = "move:"
            if action.StartsWith(prefix) then
                let facing = action.Substring(prefix.Length)
                if directions |> Array.exists (fun (name, _, _) -> name = facing) then
                    Some ("CmdMove" + facing)
                else None
            else None

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

    let private window (origin: obj) =
        let x0 = asInt (memberValue origin "X")
        let y0 = asInt (memberValue origin "Y")
        let zone = memberValue origin "ParentZone"
        let radius = 2
        let rows = ResizeArray<string>()
        for dy in -radius .. radius do
            let row = StringBuilder()
            for dx in -radius .. radius do
                let cell =
                    try call zone "GetCell" [| box (x0 + dx); box (y0 + dy) |]
                    with _ -> null
                row.Append(if isNull cell then "?" else glyph cell) |> ignore
            rows.Add(row.ToString())
        x0, y0, rows

    let private actionsJson () =
        let moves =
            directions
            |> Array.map (fun (name, dx, dy) ->
                sprintf
                    "{\"id\":%s,\"kind\":\"move\",\"label\":%s,\"arguments\":{\"dx\":%d,\"dy\":%d}}"
                    (jsonString ("move:" + name))
                    (jsonString ("Move " + name))
                    dx
                    dy)
        let wait = "{\"id\":\"wait\",\"kind\":\"wait\",\"label\":\"Wait one turn\",\"arguments\":{}}"
        "[" + String.Join(",", Array.append moves [| wait |]) + "]"

    let private observation (player: obj) turn index =
        let cell = memberValue player "CurrentCell"
        let x, y, rows = window cell
        let hp = max 0 (stat player "Stat" 0)
        let mutable maxHp = max 1 (stat player "BaseStat" 1)
        if hp > maxHp then maxHp <- hp
        let tiles = rows |> Seq.map jsonString |> String.concat ","
        let id = episode + ":" + string index
        id, sprintf
            "{\"episode_id\":%s,\"decision_id\":%s,\"turn\":%d,\"phase\":\"command\",\"player\":{\"x\":%d,\"y\":%d,\"hp\":%d,\"max_hp\":%d},\"tiles\":[%s],\"entities\":[],\"messages\":[],\"prompt\":null,\"actions\":%s}"
            (jsonString episode) (jsonString id) turn x y hp maxHp tiles (actionsJson ())

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
        let slot = { id = id; index = index; turn = turn; observation = body
                     action = TaskCompletionSource<string>()
                     onward = TaskCompletionSource<Slot>() }
        lock gate (fun () ->
            match waiting with
            | Some previous -> previous.onward.TrySetResult(slot) |> ignore
            | None -> first.TrySetResult(slot) |> ignore
            waiting <- Some slot
            decisions <- index + 1)
        if logPath <> "" then
            Probe.record logPath ("boundary " + id + " turn=" + string turn) |> ignore
        slot

    // Blocks the game thread until an agent steps this decision.
    let supply (player: obj) (turn: int) =
        let slot = publish player turn
        let actionId = slot.action.Task.GetAwaiter().GetResult()
        match commandOf actionId with
        | Some command -> command
        | None -> failwith ("rejected action " + actionId)

    let private currentWaiting () =
        lock gate (fun () ->
            match waiting with
            | Some slot when not slot.action.Task.IsCompleted -> Some slot
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
                    ok requestId (transition slot.index slot.turn slot.observation)
        | "observe" ->
            if not resetUsed then fail requestId "reset_required" "Reset before observing"
            else
                match currentWaiting () with
                | Some slot -> ok requestId slot.observation
                | None -> fail requestId "reset_required" "No decision is waiting"
        | "step" ->
            if not resetUsed then fail requestId "reset_required" "Reset before stepping"
            else
                match findString "decision_id" body, findString "action_id" body with
                | None, _ | _, None ->
                    fail requestId "invalid_action" "step requires decision_id and action_id"
                | Some decisionId, Some actionId ->
                    match commandOf actionId with
                    | None -> fail requestId "invalid_action" "Action is not a current candidate"
                    | Some _ ->
                        let slot = currentWaiting ()
                        match slot with
                        | None -> fail requestId "stale_decision" "No decision is waiting"
                        | Some current when current.id <> decisionId ->
                            fail requestId "stale_decision" "Re-observe: the decision boundary has changed"
                        | Some current ->
                            if not (current.action.TrySetResult(actionId)) then
                                fail requestId "stale_decision" "That decision was already stepped"
                            else
                                let next = current.onward.Task.GetAwaiter().GetResult()
                                ok requestId (transition next.index next.turn next.observation)
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
