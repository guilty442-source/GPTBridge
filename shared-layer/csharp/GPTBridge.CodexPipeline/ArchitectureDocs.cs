using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace GPTBridge.CodexPipeline;

/// <summary>
/// Read-only architecture-doc completeness diagnostic (A537/A538) —
/// port of execution/audit/architecture_docs.py.  Reports without
/// mutating.
/// </summary>
internal static class ArchitectureDocs
{
    private const string DocGlob = "architecture-*.md";
    private const string ToolDocPrefix = "architecture-tool-";

    private static readonly Regex IdentifierPattern = new(
        @"`([a-z][a-z0-9]*(?:-[a-z0-9]+)+)`", RegexOptions.Compiled);
    private static readonly Regex TokenPattern = new(
        @"[\w.-]+", RegexOptions.Compiled);
    private static readonly Regex TokenFull = new(
        @"[\w.-]+\z", RegexOptions.Compiled);

    private static string IsoNow() =>
        DateTime.UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ");

    private static string FirstHeading(string text)
    {
        foreach (var line in text.Split('\n'))
        {
            var stripped = line.Trim();
            if (stripped.StartsWith('#'))
                return stripped.TrimStart('#').Trim();
        }
        return "";
    }

    private static JsonObject? LoadRegistry(string auditDir)
    {
        var path = Path.Combine(auditDir, "architecture_registry.json");
        return File.Exists(path)
            ? JsonNode.Parse(File.ReadAllText(path)) as JsonObject
            : null;
    }

    public static Dictionary<string, object?> Report(string root)
    {
        var projectRoot = Path.GetFullPath(root);
        var auditDir = Path.Combine(projectRoot, "governance_rule",
            "execution", "audit");
        var codexRoot = Path.Combine(projectRoot, "governance_rule",
            "codex");
        JsonObject? registry;
        try
        {
            registry = LoadRegistry(auditDir);
            if (registry is null)
                throw new InvalidDataException(
                    "registry file missing or unreadable");
        }
        catch (Exception error)
        {
            return new Dictionary<string, object?>(
                StringComparer.Ordinal)
            {
                ["ok"] = false, ["complete"] = false,
                ["checked_at"] = IsoNow(),
                ["project_root"] = projectRoot,
                ["errors"] = new object?[]
                {
                    $"architecture registry unreadable: {error.Message}",
                },
                ["gaps"] = new object?[] { },
                ["unreferenced_canonical"] = new object?[] { },
                ["documents"] = new object?[] { },
            };
        }
        var components = Repo.Get(registry, "components") is JsonArray ca
            ? ca.OfType<JsonObject>().ToList() : new List<JsonObject>();
        var componentIds = components
            .Select(c => Repo.Str(c, "component_id"))
            .Where(id => id.Length > 0)
            .OrderBy(id => id, StringComparer.Ordinal).ToList();
        var canonical = components
            .Where(c => Repo.Get(c, "canonical") is JsonValue v
                && v.TryGetValue<bool>(out var b) && b
                && Repo.Str(c, "component_id").Trim().Length > 0)
            .ToList();
        var errors = new List<string>();
        var gaps = new List<Dictionary<string, object?>>();
        var documents = new List<Dictionary<string, object?>>();
        var referencedAny = new HashSet<string>(StringComparer.Ordinal);
        if (!Directory.Exists(codexRoot))
            errors.Add($"codex root missing: {codexRoot}");
        var docPaths = Directory.Exists(codexRoot)
            ? Directory.GetFiles(codexRoot, DocGlob)
                .OrderBy(p => p, StringComparer.Ordinal).ToList()
            : new List<string>();
        foreach (var path in docPaths)
        {
            string text;
            try
            {
                text = File.ReadAllText(path);
            }
            catch (IOException error)
            {
                errors.Add($"{Path.GetFileName(path)}: unreadable "
                    + $"({error.Message})");
                continue;
            }
            var title = FirstHeading(text);
            var hasMermaid = text.Contains("```mermaid",
                StringComparison.Ordinal);
            var tokens = TokenPattern.Matches(text)
                .Select(m => m.Value).ToHashSet(StringComparer.Ordinal);
            var referenced = componentIds
                .Where(id => TokenFull.IsMatch(id)
                    ? tokens.Any(t => t.Contains(id,
                        StringComparison.Ordinal))
                    : text.Contains(id, StringComparison.Ordinal))
                .OrderBy(id => id, StringComparer.Ordinal).ToList();
            referencedAny.UnionWith(referenced);
            var stale = IdentifierPattern.Matches(text)
                .Select(m => m.Groups[1].Value)
                .Distinct()
                .Where(id => !componentIds.Contains(id)
                    && id is not "model-dialogue" and not "xingcheng"
                    && !id.StartsWith("governance-",
                        StringComparison.Ordinal)
                    && !id.StartsWith("gptbridge-",
                        StringComparison.Ordinal))
                .OrderBy(id => id, StringComparer.Ordinal).ToList();
            if (title.Length == 0)
                errors.Add($"{Path.GetFileName(path)}: missing "
                    + "document title");
            if (!hasMermaid)
                errors.Add($"{Path.GetFileName(path)}: missing mermaid "
                    + "diagram block");
            documents.Add(new Dictionary<string, object?>(
                StringComparer.Ordinal)
            {
                ["name"] = Path.GetFileName(path),
                ["title"] = title,
                ["mermaid"] = hasMermaid,
                ["bytes"] = (long)System.Text.Encoding.UTF8
                    .GetByteCount(text),
                ["referenced_components"] =
                    referenced.Cast<object?>().ToList(),
                ["stale_identifiers"] =
                    stale.Cast<object?>().ToList(),
            });
        }
        var docNames = docPaths
            .Select(Path.GetFileName).ToHashSet(StringComparer.Ordinal);
        foreach (var component in canonical)
        {
            var identifier = Repo.Str(component, "component_id");
            var physical = Repo.Str(component, "physical_path");
            var parts = physical.Split('/');
            var isTopTool = parts.Length == 2
                && parts[0] == "Standalone tools"
                && parts[1] == identifier
                && Repo.Get(component, "independent_tool") is not
                    (JsonValue jt)
                    || !(Repo.Get(component, "independent_tool")
                        is JsonValue tv
                        && tv.TryGetValue<bool>(out var tb) && !tb);
            if (!isTopTool)
                continue;
            var expected = $"{ToolDocPrefix}{identifier}.md";
            if (!docNames.Contains(expected))
                gaps.Add(new Dictionary<string, object?>(
                    StringComparer.Ordinal)
                {
                    ["component_id"] = identifier,
                    ["physical_path"] = physical,
                    ["reason"] =
                        $"missing expected document {expected}",
                });
        }
        var unreferenced = canonical
            .Select(c => Repo.Str(c, "component_id"))
            .Where(id => !referencedAny.Contains(id)).ToList();
        return new Dictionary<string, object?>(StringComparer.Ordinal)
        {
            ["ok"] = errors.Count == 0,
            ["complete"] = gaps.Count == 0 && errors.Count == 0,
            ["checked_at"] = IsoNow(),
            ["project_root"] = projectRoot,
            ["document_count"] = (long)documents.Count,
            ["registry_components"] = (long)components.Count,
            ["canonical_components"] = (long)canonical.Count,
            ["referenced_canonical"] = referencedAny
                .OrderBy(x => x, StringComparer.Ordinal)
                .Cast<object?>().ToList(),
            ["unreferenced_canonical"] =
                unreferenced.Cast<object?>().ToList(),
            ["documents"] = documents.Cast<object?>().ToList(),
            ["errors"] = errors.Cast<object?>().ToList(),
            ["gaps"] = gaps.Cast<object?>().ToList(),
        };
    }
}
