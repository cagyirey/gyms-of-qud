// Resolve every Harmony patch in the mod against the installed game.
//
// Why this exists, concretely: a patch declared one type too many for
// ShowBlockSpace did not fail on its own. Qud calls Harmony.PatchAll once per mod
// assembly, Harmony threw while processing that one class, and the call aborted --
// so the embark gate was never applied and the game sat at the main menu. The
// only symptom was one MODERROR line buried in the game's Player.log, and it cost
// a three-minute boot to find.
//
// The blast radius is the problem, not the typo. PatchAll is all-or-nothing per
// assembly, so a single unresolvable target silently disables every other patch
// in the mod. So the check belongs outside the game, where it is cheap and
// total: resolve all targets, print them, and fail on any that do not bind.
//
// Reads the real game assemblies, so it also catches a signature that drifts in a
// game update -- which is the case a hardcoded Type[] cannot survive.
//
// Usage (see tests/test_harmony_bindings.py for the wrapper that sets these):
//   QUD_MANAGED=<...>/Caves of Qud/CoQ.app/Contents/Resources/Data/Managed \
//   QUD_MOD_DLL=<repo>/mod/QudGym/obj/Release/netstandard2.1/QudGym.dll \
//   dotnet fsi scripts/check_patch_bindings.fsx

open System
open System.IO
open System.Reflection

let managedDir = Environment.GetEnvironmentVariable "QUD_MANAGED"
let modDll = Environment.GetEnvironmentVariable "QUD_MOD_DLL"

if String.IsNullOrEmpty managedDir || String.IsNullOrEmpty modDll then
    eprintfn "set QUD_MANAGED and QUD_MOD_DLL"
    exit 2

// The game's assemblies reference each other freely; without this, loading
// Assembly-CSharp throws on the first type that mentions another assembly.
AppDomain.CurrentDomain.add_AssemblyResolve(
    ResolveEventHandler(fun _ args ->
        let candidate = IO.Path.Combine(managedDir, (AssemblyName(args.Name).Name) + ".dll")
        if IO.File.Exists candidate then
            try Assembly.LoadFrom candidate with _ -> null
        else null))

let harmony = Assembly.LoadFrom(IO.Path.Combine(managedDir, "0Harmony.dll"))
let game = Assembly.LoadFrom(IO.Path.Combine(managedDir, "Assembly-CSharp.dll"))
let modAssembly = Assembly.LoadFrom(modDll)

let patchAttribute = harmony.GetType("HarmonyLib.HarmonyPatch")
let accessTools = harmony.GetType("HarmonyLib.AccessTools")

// The same resolution Harmony's PatchClassProcessor performs: declaring type,
// method name, parameter types, generics.
let resolve =
    accessTools.GetMethod(
        "Method",
        BindingFlags.Public ||| BindingFlags.Static,
        null,
        [| typeof<Type>; typeof<string>; typeof<Type[]>; typeof<Type[]> |],
        null)

if isNull resolve then
    eprintfn "HarmonyLib.AccessTools.Method(4-arg) not found; Harmony layout changed"
    exit 2

let describeTypes (types: Type[]) =
    if isNull types then "(by name)"
    else String.Join(", ", Array.map (fun (t: Type) -> t.Name) types)

let unresolved = ResizeArray<string>()
let mutable resolved = 0

for patchType in modAssembly.GetTypes() do
    for attribute in patchType.GetCustomAttributes(patchAttribute, false) do
        // Harmony keeps everything on a nested info struct, and a bare
        // [HarmonyPatch] -- one that names no target because the class resolves it
        // through TargetMethod -- has no info at all.
        //
        // This dereferenced it unconditionally, so a runtime-resolved patch made the
        // whole script throw. Because the wrapper treated exit code 1 as "ran and
        // found failures", the crash was reported as a clean pass, which is how a
        // broken resolver looked bound for a whole session.
        let info =
            match attribute.GetType().GetField("info") with
            | null -> null
            | f ->
                match f.GetValue(attribute) with
                | :? obj as value -> value
                | _ -> null
        let field (name: string) : obj =
            if isNull info then null
            else
                match info.GetType().GetField(name) with
                | null -> null
                | f -> f.GetValue(info)
        let declaringType = field "declaringType" :?> Type
        let methodName = field "methodName" :?> string
        let argumentTypes = field "argumentTypes" :?> Type[]
        let generics = field "generics" :?> Type[]

        // A patch that names no target in its attribute resolves it at run time
        // through a static TargetMethod or TargetMethods, and that is the only way
        // to find out whether it will work.
        //
        // Reading the attribute alone reported these as resolved: all three fields
        // are null, AccessTools.Method(null, null, null, null) returns something
        // non-null, and the check printed "ok" for a resolver that threw on the
        // first run and took the whole mod's PatchAll down with it. A false ok is
        // worse than no check, so this calls the resolver the way Harmony does.
        let resolverMethod =
            patchType.GetMethod("TargetMethod", BindingFlags.Public ||| BindingFlags.NonPublic ||| BindingFlags.Static)

        let invokeResolver (m: MethodInfo) : MethodBase =
            let outcome : obj =
                try
                    m.Invoke(null, [||])
                with
                | :? TargetInvocationException as ex -> box ex.InnerException
                | _ -> null
            match outcome with
            | :? MethodBase as target -> target
            | _ -> null

        let resolver : MethodBase =
            if isNull resolverMethod then null else invokeResolver resolverMethod

        if not (isNull resolver) then
            if isNull (box resolver) then
                unresolved.Add(patchType.Name + " -> TargetMethod() returned null")
                printfn "FAIL  %-28s TargetMethod() returned null" patchType.Name
            else
                resolved <- resolved + 1
                printfn "ok    %-28s %s (resolved at runtime)" patchType.Name resolver.Name

        let target : MethodBase =
            let outcome : obj =
                try
                    resolve.Invoke(null, [| declaringType; methodName; argumentTypes; generics |])
                with ex -> box ex.InnerException
            match outcome with
            | :? MethodBase as found -> found
            | _ -> null

        if not (isNull resolver) then ()
        elif isNull target then
            unresolved.Add(
                sprintf "%s -> %s.%s(%s)"
                    patchType.Name
                    (if isNull declaringType then "?" else declaringType.FullName)
                    methodName
                    (describeTypes argumentTypes))
            printfn "FAIL  %-28s %s" patchType.Name (describeTypes argumentTypes)
        else
            resolved <- resolved + 1
            printfn "ok    %-28s %s(%s)" patchType.Name methodName (describeTypes argumentTypes)

printfn ""
printfn "resolved %d, unresolved %d" resolved unresolved.Count

// Nonzero exit so the test wrapper fails rather than merely printing.
exit (if unresolved.Count = 0 then 0 else 1)
