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
let resolved = 0

for patchType in modAssembly.GetTypes() do
    for attribute in patchType.GetCustomAttributes(patchAttribute, false) do
        // Harmony keeps everything on a nested info struct.
        let info = attribute.GetType().GetField("info").GetValue(attribute)
        let field (name: string) : obj = info.GetType().GetField(name).GetValue(info)
        let declaringType = field "declaringType" :?> Type
        let methodName = field "methodName" :?> string
        let argumentTypes = field "argumentTypes" :?> Type[]
        let generics = field "generics" :?> Type[]

        let target =
            try
                resolve.Invoke(null, [| declaringType; methodName; argumentTypes; generics |])
            with ex ->
                ex.InnerException :?> MethodBase

        if isNull target then
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
