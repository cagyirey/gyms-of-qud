using System.Collections.Immutable;
using System.Reflection;
using System.Reflection.Metadata;
using System.Reflection.PortableExecutable;
using System.Security.Cryptography;
using System.Text.Json;

// A metadata reader, NOT Assembly.Load, reflection invocation or a Qud mod.
// No game constructors, property getters, module initializers or method bodies run.
if (args.Length != 3)
{
    Console.Error.WriteLine("Usage: QudGym.ApiProbe ASSEMBLY EXPECTED_SHA256 NEW_REPORT.json");
    return 2;
}
try
{
    var input = Path.GetFullPath(args[0]);
    var output = Path.GetFullPath(args[2]);
    var inputDirectory = Path.GetDirectoryName(input)!;
    if (output == input || output.StartsWith(inputDirectory + Path.DirectorySeparatorChar,
            StringComparison.OrdinalIgnoreCase))
        throw new InvalidOperationException("Report must be outside the assembly directory.");
    if (File.Exists(output)) throw new InvalidOperationException("Report already exists.");
    if (args[1].Length != 64 || args[1].Any(c => !Uri.IsHexDigit(c)))
        throw new InvalidOperationException("Expected hash must contain 64 hexadecimal characters.");
    byte[] image;
    using (var file = new FileStream(input, FileMode.Open, FileAccess.Read, FileShare.Read))
    {
        if (file.Length <= 0 || file.Length > 64 * 1024 * 1024)
            throw new InvalidOperationException("Assembly exceeds the 64 MiB inspection budget.");
        image = new byte[checked((int)file.Length)];
        file.ReadExactly(image);
    }
    var hash = Convert.ToHexString(SHA256.HashData(image)).ToLowerInvariant();
    if (!hash.Equals(args[1], StringComparison.OrdinalIgnoreCase))
        throw new InvalidOperationException("Assembly hash differs from the reviewed manifest.");
    using var stream = new MemoryStream(image, writable: false);
    using var pe = new PEReader(stream);
    if (!pe.HasMetadata) throw new InvalidOperationException("File has no managed metadata.");
    var reader = pe.GetMetadataReader();
    if (!reader.IsAssembly) throw new InvalidOperationException("File is not a managed assembly.");
    var names = new SignatureNames();
    var queries = Queries.Create();
    var byName = reader.TypeDefinitions.ToDictionary(h => names.GetTypeFromDefinition(reader, h, 0));
    var results = new List<object>();
    foreach (var (typeName, filters) in queries)
    {
        if (!byName.TryGetValue(typeName, out var handle))
        {
            results.Add(new { type_name = typeName, found = false });
            continue;
        }
        var type = reader.GetTypeDefinition(handle);
        bool Include(string name) => filters.Length == 0 || filters.Any(
            filter => name.Contains(filter, StringComparison.OrdinalIgnoreCase));
        var members = new List<object>();
        var matched = 0;
        foreach (var methodHandle in type.GetMethods())
        {
            var method = reader.GetMethodDefinition(methodHandle);
            var name = reader.GetString(method.Name);
            if (!Include(name)) continue;
            matched++;
            if (members.Count >= 96) continue;
            var sig = method.DecodeSignature(names, (object?)null);
            members.Add(new { kind = "method", name, attributes = method.Attributes.ToString(),
                return_type = sig.ReturnType, parameters = sig.ParameterTypes,
                generic_parameters = sig.GenericParameterCount });
        }
        foreach (var fieldHandle in type.GetFields())
        {
            var field = reader.GetFieldDefinition(fieldHandle);
            var name = reader.GetString(field.Name);
            if (!Include(name)) continue;
            matched++;
            if (members.Count >= 96) continue;
            members.Add(new { kind = "field", name, attributes = field.Attributes.ToString(),
                field_type = field.DecodeSignature(names, (object?)null) });
        }
        // Property accessor method signatures above also reveal static/instance and accessibility.
        // No property values or constant values are read.
        results.Add(new { type_name = typeName, found = true,
            attributes = type.Attributes.ToString(), base_type = names.Handle(reader, type.BaseType),
            interfaces = type.GetInterfaceImplementations().Select(h =>
                names.Handle(reader, reader.GetInterfaceImplementation(h).Interface)).ToArray(),
            member_name_filters = filters, declared_members_only = true,
            members_truncated = matched > members.Count, matched_members = matched, members });
    }
    var assembly = reader.GetAssemblyDefinition();
    var references = reader.AssemblyReferences.Select(h => reader.GetAssemblyReference(h)).Select(a =>
        new { name = reader.GetString(a.Name), version = a.Version.ToString() }).ToArray();
    var report = new {
        schema_version = "qud-api-metadata/1",
        assembly = new { name = reader.GetString(assembly.Name), version = assembly.Version.ToString(),
            bytes = image.Length, sha256 = hash,
            mvid = reader.GetGuid(reader.GetModuleDefinition().Mvid).ToString() },
        metadata_version = reader.MetadataVersion, references, queries = results,
        runtime_hooks_verified = false, game_code_executed = false,
        notes = new[] {
            "Presence of a signature is not evidence of a working hook or thread safety.",
            "Missing types are missing from this assembly, not necessarily from the whole installation.",
            "Only selected declared method/field signatures; no IL, strings, assets, saves or runtime values.",
            "Reported game release, export gameversion and assembly version are separate declarations."
        }
    };
    var json = JsonSerializer.Serialize(report, new JsonSerializerOptions { WriteIndented = true });
    if (System.Text.Encoding.UTF8.GetByteCount(json) > 1024 * 1024)
        throw new InvalidOperationException("Report exceeds 1 MiB; narrow the metadata queries.");
    using var destination = new FileStream(output, FileMode.CreateNew, FileAccess.Write);
    using var writer = new StreamWriter(destination);
    writer.Write(json);
    writer.WriteLine();
    return 0;
}
catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or BadImageFormatException
                          or InvalidOperationException or ArgumentException or OverflowException)
{
    // Exceptions may contain a local install path. Do not copy their message into a shareable report.
    Console.Error.WriteLine($"Metadata inspection failed ({ex.GetType().Name}). Check input/hash, output and SDK.");
    return 1;
}

internal static class Queries
{
    public static Dictionary<string, string[]> Create() => new(StringComparer.Ordinal)
    {
        ["XRL.IPlayerMutator"] = [],
        ["XRL.PlayerMutator"] = [],
        ["XRL.PlayerMutatorAttribute"] = [],
        ["XRL.World.IPart"] = ["WantEvent", "HandleEvent", "ParentObject", "Register"],
        ["XRL.World.EndTurnEvent"] = [],
        ["XRL.World.BeforeRenderEvent"] = [],
        ["XRL.World.BeginTakeActionEvent"] = [],
        ["XRL.The"] = ["Player", "Game"],
        ["XRL.Core.XRLCore"] = [],
        ["XRL.World.AI.Pathfinding"] = [],
        ["XRL.World.Parts.Interactable"] = [],
        ["XRL.CharacterBuilds.EmbarkBuilder"] = [],
        ["XRL.CharacterBuilds.AbstractEmbarkBuilderModule"] = [],
                ["ConsoleLib.Console+TextConsole"] = ["Get", "Row", "Buffer", "Scroll", "Lines"],
        ["XRL.World.GameObject"] = ["CurrentCell", "DisplayName", "Stat", "Visible", "Render",
            "Inventory", "Body", "ActivatedAbilit", "GetPart", "HasPart", "AddPart"],
        ["XRL.World.Cell"] = ["Visible", "Explored", "Objects", "ParentZone", "get_X", "get_Y"],
        ["XRL.World.Zone"] = ["Visible", "Explored", "GetCell", "Width", "Height"],
        ["XRL.World.Parts.Body"] = ["GetPart", "GetEquipped", "GetBody"],
        ["XRL.World.Anatomy.BodyPart"] = ["Type", "Name", "Equipped", "Child", "Part"],
        ["XRL.World.Parts.ActivatedAbilities"] = ["List", "Get", "Ability"],
        ["XRL.UI.MessageQueue"] = ["Message", "Get"],
        ["ConsoleLib.Console.Keyboard"] = [],
        // Types a play-capable read-only projection needs. XRL.UI.MessageQueue
        // does not exist on this build; the message log is XRL.Messages.
        ["XRL.Messages"] = ["Message", "Get", "Log", "Add", "Clear"],
        ["Qud.UI.MessageLogLineData"] = [],
        ["Qud.UI.MessageLogWindow"] = [],
        ["ConsoleLib.Console.ScreenBuffer"] = [],
        ["XRL.UI.Popup"] = ["Show", "Text", "Options", "Menu"],
        ["XRL.World.Zone"] = ["Width", "Height", "GetCell", "Visible", "Explored", "ID", "Name"],
        ["XRL.World.GameObject"] = ["DisplayName", "IsPlayer", "CurrentCell", "GetRenderString",
            "Visible", "IsVisibleTo", "Blueprint", "GetBlueprint",
            // Firing the game's own verb is how an action is performed, so the
            // event surface belongs next to the read surface.
            "FireEvent", "GetPart", "HasPart", "AddPart", "GetRenderString"],
        ["XRL.The"] = ["Player", "Game"],
        ["XRL.Core.XRLCore"] = ["Update", "LateUpdate", "Main", "RunGame", "Tick",
            "Idle", "Wait", "Step", "WriteConsoleLine", "NewGame", "IsCoreThread",
            "RegisterNewMessageLogEntryCallback", "CallNewMessageLogEntryCallbacks"],

        // The blocking surfaces. Every method here waits for a key, which is why
        // the mod suppresses rather than drives them. Kept together and named,
        // because the set is the thing that has to stay complete: two of these
        // were ungated for a long time and the game sat on a dialog nobody could
        // dismiss. The list of waiting methods is the fact worth publishing.
        ["XRL.UI.Popup"] = ["Show", "ShowFail", "ShowBlock", "ShowBlockPrompt",
            "ShowBlockSpace", "ShowBlockWithCopy", "ShowSpace", "PickOption",
            "Suppress", "Transform", "WaitNewPopupMessage", "NewPopupMessageAsync"],

        // Conversations and menus. PickOption is options-in, index-out, so it is
        // the generic menu shape; the observation publishes its options verbatim
        // and the answer supplies the index.
        ["XRL.UI.ConversationUI"] = ["HaveConversation", "Select", "CurrentChoices",
            "CurrentConversation", "Input", "Render"],
        ["XRL.World.Conversations.Conversation"] = ["GetDisplayText", "Value"],
        ["XRL.World.Conversations.ConversationChoice"] = ["GetDisplayText", "Value"],

        // Quest state, read rather than drawn. CmdQuests pushes the QuestLog
        // screen and a pushed screen blocks on a keypress.
        ["XRL.World.Quest"] = ["ID", "DisplayName", "StepsByID", "ShowStartPopup",
            "ShowFailPopup", "ShowFailStepPopup", "ShowFinishPopup",
            "ShowFinishStepPopup", "BonusAtLevel", "ReadyToTurnIn"],
        ["XRL.World.QuestStep"] = ["ID", "Name", "Text", "Finished", "Ordinal", "Collapse"],
        ["XRL.UI.QuestLog"] = ["GetLinesForQuest"],
        ["XRL.XRLGame"] = ["Quests", "FinishedQuests", "Turns", "Messages"],

        // A backed-array map, not a dictionary. It declares IDictionary<string,T>
        // but enumerates through a struct enumerator, so a plain foreach yields
        // nothing and a cast to the non-generic interface throws. Reading it
        // correctly is the difference between an empty list and the real one.
        ["XRL.Collections.StringMap`1"] = ["Count", "Item", "ContainsKey", "GetEnumerator",
            "Values", "Keys", "TryGetValue"],

        // Equipment: a query and a verb, with no picker in between.
        ["XRL.World.Parts.Inventory"] = ["GetEquipmentListForSlot", "GetObjectsReadonly",
            "GetObjects", "GetInventoryObjectList"],
        ["XRL.World.Anatomy.BodyPart"] = ["Type", "Name", "Equipped", "Primary",
            "GetOrdinalName", "Child", "Part"],
        ["XRL.World.Anatomy.BodyPartType"] = ["Type", "Name", "Ordinal"],
        ["XRL.World.Parts.Body"] = ["GetParts", "GetPart", "GetBody"],

        // Console markup. Strip is what removes tags; Transform renders them and
        // is not the same operation, which is worth recording because the two read
        // alike and only one of them answers "what does the player see".
        ["ConsoleLib.Console.Markup"] = ["Strip", "Transform"],
        ["ConsoleLib.Console.MarkupNode"] = ["Name"],

        // The browsable windows. A third shape: not a question, and nothing to
        // intercept, so the honest route is reading the model they would draw.
        ["XRL.UI.Screens"] = ["Show", "ShowPopup", "CurrentScreen"],
        ["XRL.UI.InventoryScreen"] = ["Show", "EquipmentList"],
        ["XRL.UI.EquipmentScreen"] = ["Show", "ShowBodypartEquipUI", "EquipmentList"],

        // Events, because firing the game's own verb is how an action is performed
        // rather than simulated.
        ["XRL.World.Event"] = ["SetParameter", "GetParameter", "GetStringParameter"],
        ["XRL.World.Parts.ConversationScript"] = ["AttemptConversation", "GetActiveConversationBlueprint"],

        // The embark gate. Skipping this screen is what keeps world generation off
        // the UI thread; its base type is worth publishing alongside it because the
        // skip is only safe while Show is cosmetic.
        ["Qud.UI.WorldGenerationScreen"] = ["Show"],
        ["Qud.UI.SingletonWindowBase"] = ["Show"],

        // Types that appear in a patch's own parameter list rather than as its
        // target. A reader reconstructing a HarmonyPatch needs these: choosing the
        // wrong one of two same-named types is how a signature silently fails to
        // bind, and the whole assembly's patches then go unapplied.
        ["XRL.UI.DialogResult"] = ["Yes", "No", "Cancel"],
        ["Genkit.Location2D"] = [],
        ["XRL.World.Anatomy.BodyPartType"] = ["Type", "Name", "Ordinal"],
        ["ConsoleLib.Console.KeyCode"] = [],
        ["ConsoleLib.Console.Keys"] = ["Space", "Enter", "Escape"],
        // Two same-named types in different namespaces, which is precisely why
        // they are here: a patch binding IRenderable when the game declared
        // ConsoleLib.Console.IRenderable fails to resolve, and takes every other
        // patch in the assembly with it.
        ["ConsoleLib.Console.IRenderable"] = [],
        ["ConsoleLib.Console.Renderable"] = ["GetRenderString"],
    };
}

internal sealed class SignatureNames : ISignatureTypeProvider<string, object?>
{
    private static string Join(string ns, string name) => ns.Length == 0 ? name : ns + "." + name;
    public string Handle(MetadataReader r, EntityHandle h) => h.IsNil ? "" : h.Kind switch {
        HandleKind.TypeDefinition => GetTypeFromDefinition(r, (TypeDefinitionHandle)h, 0),
        HandleKind.TypeReference => GetTypeFromReference(r, (TypeReferenceHandle)h, 0),
        HandleKind.TypeSpecification => GetTypeFromSpecification(r, null, (TypeSpecificationHandle)h, 0),
        _ => "<" + h.Kind + ">"
    };
    public string GetTypeFromDefinition(MetadataReader r, TypeDefinitionHandle h, byte rawTypeKind) {
        var t = r.GetTypeDefinition(h);
        var parent = t.GetDeclaringType();
        return parent.IsNil ? Join(r.GetString(t.Namespace), r.GetString(t.Name))
            : GetTypeFromDefinition(r, parent, 0) + "+" + r.GetString(t.Name);
    }
    public string GetTypeFromReference(MetadataReader r, TypeReferenceHandle h, byte rawTypeKind) {
        var t = r.GetTypeReference(h);
        return t.ResolutionScope.Kind == HandleKind.TypeReference
            ? GetTypeFromReference(r, (TypeReferenceHandle)t.ResolutionScope, 0) + "+" + r.GetString(t.Name)
            : Join(r.GetString(t.Namespace), r.GetString(t.Name));
    }
    public string GetTypeFromSpecification(MetadataReader r, object? context,
        TypeSpecificationHandle h, byte rawTypeKind) => r.GetTypeSpecification(h).DecodeSignature(this, context);
    public string GetPrimitiveType(PrimitiveTypeCode code) => code.ToString();
    public string GetSZArrayType(string elementType) => elementType + "[]";
    public string GetArrayType(string elementType, ArrayShape shape) => elementType + "[" + new string(',', shape.Rank - 1) + "]";
    public string GetByReferenceType(string elementType) => elementType + "&";
    public string GetPointerType(string elementType) => elementType + "*";
    public string GetPinnedType(string elementType) => elementType + " pinned";
    public string GetGenericInstantiation(string genericType, ImmutableArray<string> typeArguments)
        => genericType + "<" + string.Join(",", typeArguments) + ">";
    public string GetGenericMethodParameter(object? context, int index) => "!!" + index;
    public string GetGenericTypeParameter(object? context, int index) => "!" + index;
    public string GetModifiedType(string modifier, string unmodifiedType, bool isRequired)
        => unmodifiedType + (isRequired ? " modreq(" : " modopt(") + modifier + ")";
    public string GetFunctionPointerType(MethodSignature<string> signature)
        => "fn(" + string.Join(",", signature.ParameterTypes) + ")->" + signature.ReturnType;
}
