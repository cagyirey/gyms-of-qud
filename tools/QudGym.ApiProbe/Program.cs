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
        ["XRL.World.GameObject"] = ["CurrentCell", "DisplayName", "Stat", "Visible", "Render",
            "Inventory", "Body", "ActivatedAbilit", "GetPart", "HasPart", "AddPart"],
        ["XRL.World.Cell"] = ["Visible", "Explored", "Objects", "ParentZone", "get_X", "get_Y"],
        ["XRL.World.Zone"] = ["Visible", "Explored", "GetCell", "Width", "Height"],
        ["XRL.World.Parts.Body"] = ["GetPart", "GetEquipped", "GetBody"],
        ["XRL.World.Anatomy.BodyPart"] = ["Type", "Name", "Equipped", "Child", "Part"],
        ["XRL.World.Parts.ActivatedAbilities"] = ["List", "Get", "Ability"],
        ["XRL.UI.MessageQueue"] = ["Message", "Get"],
        ["ConsoleLib.Console.Keyboard"] = ["get", "Key", "Input", "Push"],
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
