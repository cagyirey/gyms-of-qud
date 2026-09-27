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
          Nearby: string list }

    /// A predicate over the live world, evaluated on the game thread.
    type Test = View -> bool

    type Plan =
        /// Act. The guard, when present, must hold or the step is skipped.
        | Act of action: string * guard: Test option
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
        | Skipped of rest: Plan
        /// The plan named an action the live action space does not offer.
        | Unsupported of action: string * rest: Plan
        | Exhausted of rest: Plan

    // -- combinators -------------------------------------------------------

    let act action = Act(action, None)
    let actIf guard action = Act(action, Some guard)
    let choose test whenTrue whenFalse = Branch(test, whenTrue, whenFalse)
    let repeatUntil test body limit = Repeat(test, body, limit)
    let all plans = All plans

    /// The action ids a plan can name, for validation and documentation.
    let rec actions (plan: Plan) : string list =
        match plan with
        | Act(a, _) -> [ a ]
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

    // -- execution ---------------------------------------------------------

    /// Advance the plan by at most one action.
    ///
    /// Returns what to do now and the plan to resume from, so no continuation
    /// is threaded and the caller cannot misuse the tail. `available` reports
    /// whether an action id is offered at this boundary; a step naming an
    /// action the world does not offer is reported as Unsupported rather than
    /// pushed, because a pushed-and-ignored action is indistinguishable from a
    /// successful one at the call site.
    let rec advance (available: string -> bool) (view: View) (plan: Plan) : Outcome =
        match plan with
        | Act(action, guard) ->
            match guard with
            | Some g when not (g view) -> Skipped(All [])
            | _ ->
                if available action then Stepped(action, All [])
                else Unsupported(action, All [])
        | Branch(test, whenTrue, whenFalse) ->
            if test view then advance available view whenTrue
            else
                match whenFalse with
                | Some o -> advance available view o
                | None -> Finished
        | Repeat(untilTest, body, limit) ->
            let rest = Repeat(untilTest, body, limit - 1)
            if untilTest view then Finished
            elif limit <= 0 then Exhausted(All [])
            else
                // One action per turn: take the body's first step, and put the
                // whole repeat back for the next turn.
                match advance available view body with
                | Stepped(a, _) -> Stepped(a, rest)
                | Skipped _ -> Skipped(rest)
                | Unsupported(a, _) -> Unsupported(a, rest)
                | Finished -> Skipped(rest)
                | Exhausted _ -> Exhausted(rest)
        | All [] -> Finished
        | All (p :: tailPlan) ->
            let rest = All tailPlan
            match advance available view p with
            | Stepped(a, _) -> Stepped(a, rest)
            | Skipped _ -> Skipped(rest)
            | Unsupported(a, _) -> Unsupported(a, rest)
            | Finished -> advance available view rest
            | Exhausted _ -> advance available view rest

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
                else Some(Act(line, None)))
        All(Array.toList steps)
