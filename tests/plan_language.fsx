/// Tests for the plan language and for the prompt's notion of what is on offer.
///
/// These live in an fsx because the thing under test is F#, and they run from
/// `python -m pytest` through tests/test_plan_language.py so there is one
/// command rather than two.
///
/// The prompts below are the real strings from a live run, not invented ones.
/// The water-ritual conversation is the case that matters: it published six
/// options, the ritual made it publish five, and it greyed four of them because
/// they cost more reputation than the player held. A plan written against the
/// six-option list had no way to survive that, and did not: it answered with the
/// positions it remembered, the game ignored both, and the run sat there.

// Plan.fs is loaded, not referenced: it is self-contained F#, and the fsproj
// compiles it into a netstandard library the game loads, which fsi cannot see.
#load "../mod/QudGym.Impl/Plan.fs"

open System
open QudGym

let mutable failures = 0
let mutable checks = 0

let checkEq name expected actual =
    checks <- checks + 1
    let e = expected |> List.map string
    let a = actual |> List.map string
    if e = a then ()
    else
        failures <- failures + 1
        printfn "FAIL %s\n  expected %A\n  actual   %A" name e a

/// One value, not a list. checkEq maps both sides through `string`, which is
/// right for a list of action ids and wrong for a bare answer.
let checkEqOne name expected actual =
    checks <- checks + 1
    if expected = actual then ()
    else
        failures <- failures + 1
        printfn "FAIL %s\n  expected %A\n  actual   %A" name expected actual

let check name condition =
    checks <- checks + 1
    if condition then ()
    else
        failures <- failures + 1
        printfn "FAIL %s" name

// -- the live prompts --------------------------------------------------------

/// Mehmet before the ritual: six options, all of them selectable.
let beforeRitual =
    [| "{{G|I am called Jabidia.}}"
       "{{G|Can you tell me about your village, Joppa?}}"
       "{{G|I am in search of work.}}"
       "{{G|Your thirst is mine, my water is yours.}} {{g|[begin water ritual; {{C|1}} dram of {{B|water}}]}}"
       "{{G|Let's trade.}} {{g|[begin trade]}}"
       // The exit. Its trailing tag is K, its label is G, and it is selectable.
       "{{G|Live and drink.}} {{K|[End]}}" |]

/// Mehmet after the ritual: five options, four of them greyed by reputation.
let afterRitual =
    [| "{{K|Share a secret with me, water-brother.}} {{K|[{{r|-50}} reputation]}}"
       "{{K|Would you teach me your ways?}} {{K|[learn {{W|Harvestry}}: {{r|-50}} reputation]}}"
       "{{K|Would you share the favorite dish of your people?}} {{K|[learn to cook {{W|{{W|Apple Matz}}}}: {{r|-50}} reputation]}}"
       "{{K|I would ask you to join me, water-brother.}} {{K|[{{r|-380}} reputation]}}"
       "{{G|Live and drink, water-brother.}} {{g|[end the water ritual]}}" |]

/// The {{tag|...}} runs removed, so a failure reads as the game's own words.
let plain (s: string) =
    System.Text.RegularExpressions.Regex.Replace(s, "\{\{[A-Za-z]+\|", "").Replace("}}", "")

/// The rule ChoiceAvailability implements, restated so the fsx can test it
/// without loading the mod assembly: the colour is the first tag, because
/// IConversationElement.GetDisplayText prepends it, and the first '|' closes it.
/// K is the only colour the game ever assigns to mean "will not accept".
let tag (s: string) =
    if s.StartsWith "{{" then
        let bar = s.IndexOf '|'
        if bar < 0 then "" else s.Substring(2, bar - 2)
    else ""

/// A view with no world in it, so a plan's own tests never decide the outcome.
let bare = { Plan.Player = null; Plan.Nearby = []; Plan.Entities = [] }

/// Run a plan against a prompt offering `options`, and report the action the
/// first step pressed. This is the shape of the real question: a prompt exists,
/// and the plan is asked what it wants to do with it.
let press (cancellable: bool) (options: string[]) (blocked: bool list) plan =
    let onOffer =
        [ 0 .. options.Length - 1 ]
        |> List.filter (fun i -> i >= blocked.Length || not (List.item i blocked))
        |> List.map (fun i -> i + 1)
        |> fun xs -> if cancellable then 0 :: xs else xs
        |> Set.ofList
    let resolve a =
        if a = Plan.AvailableAction then
            let good = onOffer |> Set.toList |> List.filter (fun n -> n > 0)
            match good with
            | [ only ] -> Plan.Pressed("answer:" + string only)
            | [] -> Plan.Refused("no option the game will accept")
            | many ->
                Plan.Refused(
                    "the game will accept "
                    + string (List.length many)
                    + " of its options ("
                    + (many |> List.map string |> String.concat ", ")
                    + ")")
        else
            match Int32.TryParse(if a.StartsWith "answer:" then a.Substring 7 else "") with
            | true, n when Set.contains n onOffer -> Plan.Pressed a
            | _ -> Plan.Refused("the game is not offering " + a)
    match Plan.advance resolve bare plan with
    | Plan.Stepped(action, _) -> action
    | Plan.Unavailable(reason, _) -> "UNAVAILABLE: " + reason
    | Plan.Unsupported(a, _) -> "UNSUPPORTED: " + a
    | Plan.Skipped _ -> "SKIPPED"
    | Plan.Finished -> "FINISHED"
    | Plan.Exhausted _ -> "EXHAUSTED"

let private parse = Plan.parse

// -- the tag rule ------------------------------------------------------------

checkEq
    "nothing before the ritual is rendered in a colour that means blocked"
    ([]: string list)
    (beforeRitual |> Array.map tag |> Array.filter (fun t -> t <> "G" && t <> "g") |> Array.toList)

checkEq
    "the four reputation choices are rendered K"
    [ "K"; "K"; "K"; "K" ]
    (afterRitual |> Array.map tag |> Array.take 4 |> Array.toList)

checkEqOne "the exit is rendered G" "G" (afterRitual |> Array.map tag |> Array.last)

checkEqOne
    "the exit's trailing K is on the trailing tag, not on its label"
    "Live and drink. [End]"
    (plain (beforeRitual |> Array.last))

checkEqOne "a string with no leading tag yields no verdict" "" (tag "just words")

// -- the DSL -----------------------------------------------------------------

checkEq "options takes positions, avail and esc" [ "answer:3"; "answer:avail"; "answer:0" ]
         (parse "options 3, avail, esc" |> Plan.actions)

checkEq
    "avail is one named action, so a plan can be checked against it"
    [ "answer:avail" ]
    (parse "options avail" |> Plan.actions)

checkEq "the old spelling still parses" [ "answer:3"; "answer:1"; "answer:2"; "answer:0" ]
         (parse "options 3, 1, 2, esc" |> Plan.actions)

checkEq
    "a plan names every action it can press"
    [ "steer:mehmet"; "talk:mehmet"; "answer:avail" ]
    (parse "goto mehmet\ntalk:mehmet\noptions avail" |> Plan.actions)

// Names are the game's DisplayNames and several are two words. A two-token
// line used to be read as name plus step limit, and then int "farmer" threw --
// so the world's own name arrived as an arithmetic error.
checkEq
    "a two-word name is a name, not a name and a step limit"
    [ "steer:watervine farmer" ]
    (parse "goto watervine farmer" |> Plan.actions)

checkEq
    "a two-word name still takes a step limit"
    [ "steer:watervine farmer" ]
    (parse "goto watervine farmer 50" |> Plan.actions)

checkEq
    "a one-word name still takes a step limit"
    [ "steer:watervine farmer"; "steer:well" ]
    (parse "goto watervine farmer 50\ngoto well 30" |> Plan.actions)

checkEq
    "the full quest program names every action it can press"
    [ "steer:watervine farmer"; "talk:watervine farmer"; "answer:4"; "answer:avail" ]
    (parse "goto watervine farmer\ntalk:watervine farmer\noptions 4, avail" |> Plan.actions)

check "a token that is neither a position nor a keyword is rejected"
    (try
        parse "options 3, please" |> ignore
        false
     with _ -> true)

// -- avail against the real prompts ------------------------------------------

// Before the ritual every option is selectable, so "the one the game will
// accept" has no referent and the right answer is to refuse. Positions are the
// right selector here and are wrong one step later, which is the whole reason
// both exist.
check
    "avail is ambiguous before the ritual, when all six options are selectable"
    (press false beforeRitual (List.init 6 (fun _ -> false)) (parse "options avail")
     |> fun a -> a.StartsWith "UNAVAILABLE" && a.Contains "6 of its options")

checkEqOne "a position still names a selectable option before the ritual" "answer:4"
           (press false beforeRitual (List.init 6 (fun _ -> false)) (parse "options 4"))

// This is the case the change exists for: one plan, run before and after the
// ritual, resolving to different positions -- and both of them right.
checkEqOne "avail takes the exit once the ritual has greyed the rest" "answer:5"
           (press false afterRitual [ true; true; true; true; false ] (parse "options avail"))

check
    "avail is refused when the game will accept nothing"
    (press false afterRitual [ true; true; true; true; true ] (parse "options avail")
     |> fun a -> a.StartsWith "UNAVAILABLE" && a.Contains "no option")

let ambiguous = press false afterRitual (List.init 5 (fun _ -> false)) (parse "options avail")

check
    "avail is refused rather than guessing when several are acceptable"
    (ambiguous.StartsWith "UNAVAILABLE" && ambiguous.Contains "5 of its options")

// -- the two answers a plan used to be able to press and should not ----------

// A conversation cannot be cancelled, so `esc` is not on offer. Delivering it
// anyway answered the first option, which after the ritual was a greyed choice.
check
    "esc is unavailable to a conversation that cannot be cancelled"
    (press false afterRitual [ true; true; true; true; false ] (parse "options esc")
     |> fun a -> a.StartsWith "UNAVAILABLE")

check
    "a greyed option is not offered"
    (press false afterRitual [ true; true; true; true; false ] (parse "options 2")
     |> fun a -> a.StartsWith "UNAVAILABLE")

checkEqOne "the one option the game will take is offered" "answer:5"
         (press false afterRitual [ true; true; true; true; false ] (parse "options 5"))

// A menu that permits escape does offer it, and there the cancel is the game's
// own -1 rather than an index into the list.
checkEqOne "esc is offered to a menu that permits it" "answer:0"
         (press true afterRitual [ true; true; true; true; false ] (parse "options esc"))

// -- avail with no prompt ----------------------------------------------------

let noPrompt a =
    if a = Plan.AvailableAction then Plan.Refused "no prompt is open" else Plan.Pressed a

checkEqOne
    "avail is unavailable at a decision boundary, not silently skipped"
    "UNAVAILABLE: no prompt is open"
    (match Plan.advance noPrompt bare (parse "options avail") with
     | Plan.Unavailable(r, _) -> "UNAVAILABLE: " + r
     | _ -> "something else")

// -- the 1-based to 0-based conversion ---------------------------------------

// A plan names answer:3 for the third option. The game indexes from zero. This
// conversion was missing on the plan's path, so a plan's answer:3 selected the
// fourth option, and it went unnoticed because the plan had been written by
// reading a zero-based selection out of the game's log.
checkEqOne "the first option is the game's index 0" 0 (Plan.gameIndex 1)
checkEqOne "the third option is the game's index 2" 2 (Plan.gameIndex 3)
checkEqOne "the fifth option is the game's index 4" 4 (Plan.gameIndex 5)
checkEqOne "the cancel is the game's -1, not an index into the list" -1 (Plan.gameIndex 0)

// -- report ------------------------------------------------------------------

printfn "%d checks, %d failures" checks failures
if failures > 0 then
    exit 1
