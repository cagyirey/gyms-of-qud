namespace QudGym

open System

/// A plan language for driving Caves of Qud without a model in the loop.
///
/// Most play is conditional, not creative: walk somewhere, look, take what is
/// present, talk, hand something over. That is a program. Running it costs no
/// tokens, is deterministic, and replays exactly. A model is only needed where
/// a program cannot anticipate what it finds -- chiefly, choosing which
/// objective to pursue next.
///
/// Two properties matter more than expressiveness.
///
/// Every primitive is an action id the live action space actually offers. A
/// step naming an action the world does not offer is reported as Unsupported
/// rather than pushed and silently dropped, which is the failure that made a
/// non-moving player look like a success.
///
/// Execution and observation share the game turn thread. `advance` is called
/// from inside the existing supply boundary and reads the live world through
/// the same reflection the observation uses. No plan state is read from another
/// thread.
module Plan =

    /// What a test may look at, gathered once per decision boundary.
    ///
    /// Nearby is the same list the observation publishes, not a second walk of
    /// the zone. A plan that re-derived "what is near me" by its own reflection
    /// returned an empty list, so every guarded step was permanently false and
    /// the plan could never advance past it -- while the observation beside it
    /// listed the very object the guard was looking for. One source, gathered
    /// once, is the fix.
    type View =
        { Player: obj
          Nearby: string list
          /// Published absolute positions, as the observation reports them.
          Entities: (string * int * int) list }

    /// A predicate over the live world, evaluated on the game thread.
    type Test = View -> bool

    type Plan =
        /// Act. The guard, when present, must hold or the step is skipped.
        | Act of action: string * guard: Test option
        /// Ask the game to walk to a named object's published position.
        ///
        /// The game's own pathfinder does the walking, via AutoAct. Re-issued
        /// each turn until adjacent, so an interrupted autopilot resumes rather
        /// than leaving the plan stuck.
        | Steer of name: string
        /// Answer the open prompt with the option at this position.
        ///
        /// Positional, by index, and deliberately not by phrase. A conversation
        /// offers options whose wording the game varies between runs -- the same
        /// node has been seen as "I'm looking for work.", "Do you have work that
        /// needs doing?" and "My services are available if you have work to
        /// offer." A selector that matched on text therefore broke every time the
        /// phrasing changed, and the only reason to write one is that answering
        /// could not be written down at all. It can now.
        ///
        /// The index is the number the observation publishes, so a plan and the
        /// prompt it answers refer to the same list the game built. 0 is the
        /// game's escape, offered only when it permits one.
        | Answer of index: int
        /// Answer the open prompt with the one option the game will accept.
        ///
        /// Positions are the right selector for a node the game always offers the
        /// same way. They are the wrong selector for a choice the world gates: a
        /// conversation that has begun the water ritual publishes five options
        /// where it published six, and four of the five are greyed because they
        /// cost more reputation than the player holds. The right answer was
        /// position 4 before the ritual and position 5 after it, so a plan naming
        /// a position is naming something that moved.
        ///
        /// What did not move is the game's own verdict, so this names that
        /// instead. It resolves to the single option the game will take, and when
        /// there is more than one it refuses rather than picking: two candidates
        /// is ambiguous, and a plan that silently chose between them would be
        /// indistinguishable from one that knew.
        | Available
        /// If the test holds take the first plan, otherwise the second.
        | Branch of test: Test * whenTrue: Plan * whenFalse: Plan option
        /// Repeat the body until the test holds, or the limit is reached.
        | Repeat of until: Test * body: Plan * limit: int
        /// Run each plan in order.
        | All of Plan list

    type Outcome =
        /// A step ran; the remainder is what to advance next.
        | Stepped of action: string * rest: Plan
        | Finished
        /// Not now; the same step is worth trying again next turn.
        | Skipped of rest: Plan
        /// The plan named an action the live action space does not offer.
        | Unsupported of action: string * rest: Plan
        /// The plan named something the world does not offer at all -- a target that
        /// is not in the published list, or an answer the game is not asking for.
        ///
        /// Distinct from Skipped because Skipped means "not yet" and this means
        /// "never", so the reason is more useful than going quiet. A goto whose
        /// target is not published used to end the program silently, which was
        /// indistinguishable from a plan that had finished on purpose.
        | Unavailable of reason: string * rest: Plan
        | Exhausted of rest: Plan

    // -- combinators -------------------------------------------------------

    let act action = Act(action, None)
    let actIf guard action = Act(action, Some guard)
    /// Answer the open prompt with the option at this position. 0 is the game's
    /// own escape, offered only when it permits one.
    let answer index = Answer index
    /// Answer with a sequence of positions, one per turn.
    ///
    /// A set rather than a single choice, because a conversation is a sequence of
    /// questions and writing them out one `answer` per line is the same work with
    /// more punctuation. The conversation at the watervine farmer is
    /// `options [3; 1; 2; 0; 0]` -- ask for work, say you are looking for it,
    /// accept, then leave and cancel out.
    let answers (indices: int list) = All(List.map Answer indices)
    /// Answer with the one option the game will accept.
    let available = Available

    /// The action id a plan uses to ask for "the one the game will accept".
    ///
    /// Not a number, because there is no number to write until the prompt is in
    /// front of the harness. The session resolves it against the options the game
    /// actually published, which is the only place that list exists.
    [<Literal>]
    let AvailableAction = "answer:avail"

    /// The game's own index for an answer the observation numbered from one.
    ///
    /// The prompt publishes answer:1 for the first option because a list a human
    /// reads starts at one; the game indexes the same list from zero. This is the
    /// only place that conversion happens, which matters because it once did not
    /// happen at all on the plan's path -- a plan's answer:3 selected the fourth
    /// option, and the only reason that was not caught at once is that the plan
    /// had been written by reading the game's own zero-based answer out of the
    /// log, so the two mistakes cancelled.
    ///
    /// Zero is the cancel, and the game's value for a cancel is -1. Delivering a
    /// zero there answers a different question than the one that was asked, which
    /// is what made `esc` press the first option of a conversation.
    let gameIndex (n: int) = if n = 0 then -1 else n - 1

    /// What asking for an action turned into.
    ///
    /// Usually the action itself, so a plain availability check needs no ceremony.
    /// The substitution is for the one action that names no position of its own:
    /// "answer:avail" becomes whatever answer the live prompt resolves it to, or
    /// nothing at all, and the reason travels with it so the failure says which of
    /// the two went wrong -- no acceptable option, or more than one.
    type Resolution =
        | Pressed of action: string
        | Refused of reason: string
    let choose test whenTrue whenFalse = Branch(test, whenTrue, whenFalse)
    let repeatUntil test body limit = Repeat(test, body, limit)
    let all plans = All plans

    /// The action ids a plan can name, for validation and documentation.
    let rec actions (plan: Plan) : string list =
        match plan with
        | Act(a, _) -> [ a ]
        | Steer n -> [ "steer:" + n ]
        | Answer i -> [ "answer:" + string i ]
        | Available -> [ AvailableAction ]
        | Branch(_, a, b) -> actions a @ (match b with Some x -> actions x | None -> [])
        | Repeat(_, body, _) -> actions body
        | All ps -> List.concat (List.map actions ps)

    // -- live world access, self-contained --------------------------------

    let private prop (target: obj) (name: string) =
        if isNull target then null
        else
            let flags = Reflection.BindingFlags.Instance
                       ||| Reflection.BindingFlags.Public
                       ||| Reflection.BindingFlags.FlattenHierarchy
            let t = target.GetType()
            let p = t.GetProperty(name, flags)
            if not (isNull p) then p.GetValue(target, null)
            else
                let f = t.GetField(name, flags)
                if isNull f then null else f.GetValue(target)

    let private intAt (target: obj) (name: string) =
        match prop target name with
        | :? int as v -> v
        | _ -> -9999

    let cellOf (player: obj) = prop player "CurrentCell"
    let px (player: obj) = intAt (cellOf player) "X"
    let py (player: obj) = intAt (cellOf player) "Y"

    /// Real, named, non-scenery object names within `r` cells.
    ///
    /// Uses the engine's own GetRealNonSceneryObjects, so a visible but
    /// uninteractable wall does not satisfy a test for a target. That is the
    /// difference between "there is a watervine here" and "there are
    /// brinestalk walls here", which a name heuristic would confuse.
    // -- tests -------------------------------------------------------------

    /// The player stands on this cell.
    let at (cx: int) (cy: int) : Test = fun v -> px v.Player = cx && py v.Player = cy

    /// The player is within `r` cells, Chebyshev distance.
    let near (cx: int) (cy: int) (r: int) : Test =
        fun v ->
            let d = max (abs (px v.Player - cx)) (abs (py v.Player - cy))
            d >= 0 && d <= r

    /// A named object is in the published nearby list.
    ///
    /// The radius is accepted for readability at the call site but the list is
    /// already bounded by the observation's own view, so it is not re-applied
    /// here. Ignoring it would be wrong; re-deriving it is what broke.
    let sees (_r: int) (name: string) : Test =
        fun v ->
            v.Nearby
            |> List.exists (fun n -> not (String.IsNullOrEmpty n)
                                   && n.IndexOf(name, StringComparison.OrdinalIgnoreCase) >= 0)

    /// A named object is in the same cell as the player.
    let seesHere name : Test =
        fun v -> sees 0 name v

    /// The named object is within one cell, i.e. close enough to interact.
    ///
    /// This is the rule CmdTalk itself applies through GetCellFromDirection, and
    /// getting it wrong is silent: a target three cells away is found, offered
    /// as a conversation, and then correctly refused.
    let adjacentTo (name: string) : Test =
        fun v ->
            let px, py = px v.Player, py v.Player
            v.Entities
            |> List.exists (fun (n, ex, ey) ->
                not (String.IsNullOrEmpty n)
                && n.IndexOf(name, StringComparison.OrdinalIgnoreCase) >= 0
                && System.Math.Max(System.Math.Abs(ex - px), System.Math.Abs(ey - py)) <= 1)

    /// The first published position matching a name.
    let private findEntity (v: View) (name: string) =
        v.Entities
        |> List.tryFind (fun (n, _, _) ->
            not (String.IsNullOrEmpty n)
            && n.IndexOf(name, StringComparison.OrdinalIgnoreCase) >= 0)

    /// The rendered console has mentioned this text.
    ///
    /// Reads the live buffer, so a test cannot pass on a message the player
    /// never actually saw.
    let said (text: string) : Test =
        fun _ ->
            try
                let asm =
                    AppDomain.CurrentDomain.GetAssemblies()
                    |> Array.find (fun a -> a.GetName().Name = "Assembly-CSharp")
                let tc = asm.GetType("ConsoleLib.Console.TextConsole", false)
                if isNull tc then false
                else
                    let f =
                        tc.GetField("CurrentBuffer", Reflection.BindingFlags.Static
                                                       ||| Reflection.BindingFlags.Public
                                                       ||| Reflection.BindingFlags.NonPublic)
                    match f.GetValue(null) with
                    | null -> false
                    | buf ->
                        let t = buf.GetType()
                        let flags = Reflection.BindingFlags.Instance
                                   ||| Reflection.BindingFlags.Public
                        // Render the buffer, then search the text.
                        let render = t.GetMethod("ToString", Type.EmptyTypes)
                        let s =
                            if isNull render then buf.ToString()
                            else render.Invoke(buf, [||]).ToString()
                        s.IndexOf(text, StringComparison.OrdinalIgnoreCase) >= 0
            with _ -> false

    /// Never true: an unconditional placeholder.
    let never : Test = fun _ -> false

    /// Dismiss a modal that is waiting for a keypress.
    let continue () = act "space"

    /// Walk to a named object and stop next to it.
    let goto (name: string) (limit: int) = Repeat(adjacentTo name, Steer name, limit)

    // -- execution ---------------------------------------------------------

    /// Advance the plan by at most one action.
    ///
    /// Returns what to do now and the plan to resume from, so no continuation
    /// is threaded and the caller cannot misuse the tail. `available` reports
    /// whether an action id is offered at this boundary; a step naming an
    /// action the world does not offer is reported as Unsupported rather than
    /// pushed, because a pushed-and-ignored action is indistinguishable from a
    /// successful one at the call site.
    let rec advance (available: string -> Resolution) (view: View) (plan: Plan) : Outcome =
        advanceWith (fun _ -> ()) available view plan

    and advanceWith (trace: string -> unit) (available: string -> Resolution) (view: View) (plan: Plan) : Outcome =
        match plan with
        | Steer name ->
            // Hand the walk to the game's own pathfinder.
            //
            // Steering one cell at a time was a second, worse pathing
            // implementation sitting next to the game's: it walked into walls,
            // could not use doors, and needed a step per cell. CmdMoveTo does
            // this properly by setting AutoAct to "M<x>,<y>" and letting the
            // game autopilot there, so that is what a plan asks for.
            // A target that is not in the published list is Exhausted, not Skipped.
            //
            // Skipped carried All [], and All [] is Finished -- so a goto that could
            // not find its target ended the entire program, silently, reporting
            // success. A plan naming a name the world does not publish therefore
            // stopped at that step and every later step was dropped, which is
            // indistinguishable from a plan that had finished on purpose.
            //
            // The name is also not a guess: the walk publishes an object's
            // DisplayName, so "mehmet" is not a name the world offers at all, and
            // saying so is the useful answer. "watervine farmer" is.
            match findEntity view name with
            | None ->
                let known =
                    view.Entities
                    |> List.map (fun (n, _, _) -> n)
                    |> List.distinct
                    |> String.concat ", "
                trace ("goto " + name + " is not published; the world offers: " + known)
                Unavailable("no target named " + name + "; published: " + known, All [])
            | Some (_, ex, ey) ->
                let move = "move_to:" + string ex + "," + string ey
                trace ("goto " + name + " -> " + move)
                match available move with
                | Pressed a -> Stepped(a, All [])
                | Refused _ -> Skipped(All [])
        | Act(action, guard) ->
            match guard with
            | Some g when not (g view) -> Skipped(All [])
            | _ ->
                match available action with
                | Pressed a -> Stepped(a, All [])
                | Refused _ -> Unsupported(action, All [])
        | Answer index ->
            // Answering is delivered as an action id, so it travels the one path
            // every other action does and needs no second mechanism.
            //
            // The observation publishes an "answer:N" action for exactly the
            // options the game is offering, so the id is offered only when the
            // game permits it. An answer the game is not asking for is Unavailable
            // rather than skipped: the game asked something else, and going quiet
            // reads as "waiting" when the truth is that this plan has drifted out of
            // step with the conversation.
            let action = "answer:" + string index
            trace ("answer " + string index)
            match available action with
            | Pressed a -> Stepped(a, All [])
            | Refused reason -> Unavailable(reason, All [])
        | Available ->
            // Resolved here rather than in the parser because the answer is a
            // property of the prompt, and the prompt does not exist until the
            // game raises it. A position written in a plan is fixed; this is not.
            trace "answer the one option the game will accept"
            match available AvailableAction with
            | Pressed a -> Stepped(a, All [])
            | Refused reason -> Unavailable(reason, All [])
        | Branch(test, whenTrue, whenFalse) ->
            if test view then advanceWith trace available view whenTrue
            else
                match whenFalse with
                | Some o -> advanceWith trace available view o
                | None -> Finished
        | Repeat(untilTest, body, limit) ->
            let rest = Repeat(untilTest, body, limit - 1)
            trace ("repeat until=" + string (untilTest view) + " limit=" + string limit)
            if untilTest view then Finished
            elif limit <= 0 then Exhausted(All [])
            else
                // One action per turn: take the body's first step, and put the
                // whole repeat back for the next turn.
                match advanceWith trace available view body with
                | Stepped(a, _) -> Stepped(a, rest)
                | Skipped _ -> Skipped(rest)
                | Unsupported(a, _) -> Unsupported(a, rest)
                // A body that cannot be done is reported, not retried: repeating a
                // step against a world that does not offer it is how a loop turns
                // into a hang.
                | Unavailable(r, _) -> Unavailable(r, rest)
                | Finished -> Skipped(rest)
                | Exhausted _ -> Exhausted(rest)
        | All [] -> Finished
        | All (p :: tailPlan) ->
            // The continuation a sub-plan returns must be spliced back in front
            // of the tail. Dropping it abandoned a Repeat after one iteration,
            // because the repeat hands back "the whole repeat again" and that
            // was being thrown away in favour of the tail -- so a goto walked
            // one step and then ran whatever came next.
            let resume cont = All(cont :: tailPlan)
            match advanceWith trace available view p with
            | Stepped(a, cont) -> Stepped(a, resume cont)
            | Skipped cont -> Skipped(resume cont)
            | Unsupported(a, cont) -> Unsupported(a, resume cont)
            | Unavailable(r, cont) -> Unavailable(r, resume cont)
            | Finished -> advanceWith trace available view (All tailPlan)
            | Exhausted _ -> advanceWith trace available view (All tailPlan)

    // -- a writable syntax ------------------------------------------------

    /// Parse a test: at X Y | near X Y R | sees R <name> | says <text>
    let private parseTest (text: string) : Test =
        let parts = text.Split([| ' ' |], StringSplitOptions.RemoveEmptyEntries)
        match parts with
        | [| "at"; x; y |] -> at (int x) (int y)
        | [| "near"; x; y; r |] -> near (int x) (int y) (int r)
        | [| "sees"; r |] -> failwith "sees needs a radius and a name: sees 2 watervine"
        | [| "says" |] -> failwith "says needs text"
        | _ when parts.Length > 1 && parts.[0] = "sees" ->
            let name = parts |> Array.skip 2 |> String.concat " "
            sees (int parts.[1]) name
        | _ when parts.Length > 0 && parts.[0] = "says" ->
            said (parts |> Array.skip 1 |> String.concat " ")
        | _ -> failwithf "unknown test: %s" text

    /// Parse a plan from text, one step per line.
    ///
    /// Line based on purpose: a program someone can read and edit is the point,
    /// and a line-per-step form is diffable and cannot hide structure in
    /// indentation. Blank lines and `#` comments are ignored.
    ///
    ///     move:E
    ///     if sees 2 watervine farmer then talk:watervine farmer
    ///     options 3, 1, 2, esc
    ///
    /// An action is named, never invented: a step naming something the world
    /// does not offer comes back Unsupported at run time rather than being
    /// pushed and ignored, which is indistinguishable from success.
    let parse (text: string) : Plan =
        let steps =
            text.Split([| char 10; char 13 |], StringSplitOptions.RemoveEmptyEntries)
            |> Array.choose (fun raw ->
                let line = raw.Trim()
                if line = "" || line.StartsWith("#") then None
                elif line.StartsWith("goto ", StringComparison.Ordinal) then
                    // Walk to a named object and stop beside it. The step is a
                    // Steer resolved from the observation's published
                    // coordinates, so the walk uses no pathing of its own.
                    let rest = line.Substring(5).Trim()
                    if rest = "" then failwith "goto needs a name"
                    // A trailing number is the step limit; everything before it is
                    // the name.
                    //
                    // Names are the game's own DisplayNames and several of them
                    // are two words -- "watervine farmer" -- so a two-token line
                    // used to be read as name plus limit and then fail on
                    // int "farmer". The world's name arrived as an arithmetic
                    // error, which reads as a broken parser rather than a name it
                    // did not expect. A number cannot be a name, so asking the
                    // last token which it is settles both cases.
                    let tokens =
                        rest.Split([| ' ' |], StringSplitOptions.RemoveEmptyEntries) |> Array.toList
                    let name, limit =
                        match List.rev tokens with
                        | k :: others when others <> [] ->
                            match Int32.TryParse k with
                            | true, n -> List.rev others |> String.concat " ", n
                            | _ -> rest, 200
                        | _ -> rest, 200
                    if name = "" then failwithf "goto takes a name: %s" line
                    Some(goto name limit)
                elif line.StartsWith("if ", StringComparison.Ordinal) then
                    let body = line.Substring(3).Trim()
                    let marker = " then "
                    let at = body.IndexOf(marker, StringComparison.Ordinal)
                    if at < 0 then
                        failwithf "step needs 'if <test> then <action>': %s" line
                    let testText = body.Substring(0, at).Trim()
                    let action = body.Substring(at + marker.Length).Trim()
                    if action = "" then failwithf "step has no action: %s" line
                    Some(Act(action, Some(parseTest testText)))
                elif line.StartsWith("options ", StringComparison.Ordinal) then
                    // Answer a conversation: one position per turn.
                    //
                    //     options 3, 1, 2, esc
                    //
                    // Positions, because the game's wording for the same node
                    // changes between runs and a phrase selector would break on
                    // ordinary variation. `esc` is the game's own escape, index 0,
                    // which it offers only when it permits one.
                    //
                    // `avail` is the other half of that: a position names a place
                    // in a list, and a list the world gates is a list that changes
                    // length. The water ritual turned Mehmet's six options into
                    // five and greyed four of them, so the right answer moved from
                    // position 4 to position 5. `avail` names the game's own verdict
                    // instead, and is the one token that survives that.
                    let rest = line.Substring(8).Trim()
                    if rest = "" then failwith "options needs at least one position"
                    let pick (token: string) =
                        if token.Equals("esc", StringComparison.OrdinalIgnoreCase) then Answer 0
                        elif token.Equals("avail", StringComparison.OrdinalIgnoreCase) then Available
                        else
                            match Int32.TryParse token with
                            | true, n -> Answer n
                            | _ -> failwithf "options takes a position, 'esc' or 'avail': %s" token
                    let picks =
                        rest.Split([| ','; ' ' |], StringSplitOptions.RemoveEmptyEntries)
                        |> Array.map pick
                        |> Array.toList
                    Some(All picks)
                else Some(Act(line, None)))
        All(Array.toList steps)
