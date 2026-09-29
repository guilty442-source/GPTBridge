using System.Text.Json;
using System.Text.Json.Nodes;

namespace GPTBridge.Permission;

/// <summary>
/// Read-only snapshot of the managed permission registries — the JSON
/// successor of the retired Python snapshot APIs
/// (code_rule_directory_snapshot, directory_authority_snapshot,
/// identity_group_snapshot, identity_permission_snapshot,
/// capability_boundary_snapshot, governance_policy_snapshot).
///
/// The JSON registries keep the PyLit encoding (``$call``/``$kw``) used
/// by the Python modules, so version fields live under
/// ``&lt;OBJECT&gt;.$kw.&lt;field&gt;``.
/// </summary>
public sealed class RegistrySnapshot
{
    public JsonObject? CodeRuleDirectory { get; }
    public JsonObject? DirectoryAuthority { get; }
    public JsonObject? IdentityGroups { get; }
    public JsonObject? IdentityPermissions { get; }
    public JsonObject? CapabilityBoundaries { get; }
    public JsonObject? GovernancePolicy { get; }

    /// <summary>True when every managed registry parsed — the
    /// directory-access health signal.</summary>
    public bool Complete =>
        CodeRuleDirectory is not null && DirectoryAuthority is not null
        && IdentityGroups is not null && IdentityPermissions is not null
        && CapabilityBoundaries is not null
        && GovernancePolicy is not null;

    private RegistrySnapshot(
        JsonObject? codeRules, JsonObject? authority,
        JsonObject? identities, JsonObject? permissions,
        JsonObject? boundaries, JsonObject? policy)
    {
        CodeRuleDirectory = codeRules;
        DirectoryAuthority = authority;
        IdentityGroups = identities;
        IdentityPermissions = permissions;
        CapabilityBoundaries = boundaries;
        GovernancePolicy = policy;
    }

    private static JsonObject? Load(string path)
    {
        try
        {
            return JsonNode.Parse(File.ReadAllText(path))
                as JsonObject;
        }
        catch (IOException) { return null; }
        catch (JsonException) { return null; }
    }

    /// <summary>Load all managed registries under a project root.</summary>
    public static RegistrySnapshot LoadAll(string projectRoot)
    {
        string Rel(params string[] parts) =>
            Path.Combine(new[] { projectRoot }.Concat(parts).ToArray());
        return new RegistrySnapshot(
            Load(Rel("governance_rule", "code_rule_directory.json")),
            Load(Rel("governance_rule", "permission_directory",
                     "directory_authority.json")),
            Load(Rel("governance_rule", "permission_directory",
                     "registries", "permissions", "identity_groups.json")),
            Load(Rel("governance_rule", "permission_directory",
                     "registries", "permissions",
                     "identity_permissions.json")),
            Load(Rel("governance_rule", "permission_directory",
                     "registries", "permissions",
                     "capability_boundaries.json")),
            Load(Rel("governance_rule", "governance_policy.json")));
    }

    private static string? KwField(
        JsonObject? doc, string objKey, string field)
    {
        if (doc?[objKey]?["$kw"]?[field] is JsonValue v)
            return v.ToJsonString().Trim('"');
        return null;
    }

    /// <summary>code_rule_directory.initial_code_version.</summary>
    public string? InitialCodeVersion =>
        KwField(CodeRuleDirectory, "CODE_RULE_DIRECTORY",
                "initial_code_version");

    /// <summary>directory_authority AUTHORITY_VERSION_POLICY.current_version.</summary>
    public string? AuthorityCurrentVersion =>
        KwField(DirectoryAuthority, "AUTHORITY_VERSION_POLICY",
                "current_version");

    /// <summary>directory_authority AUTHORITY_VERSION_POLICY.initial_version.</summary>
    public string? AuthorityInitialVersion =>
        KwField(DirectoryAuthority, "AUTHORITY_VERSION_POLICY",
                "initial_version");

    /// <summary>directory_authority CODE_VERSION_POLICY.initial_version.</summary>
    public string? CodePolicyInitialVersion =>
        KwField(DirectoryAuthority, "CODE_VERSION_POLICY",
                "initial_version");

    /// <summary>Sealed identity-group ids (top-level registry keys).</summary>
    public IReadOnlyList<string> IdentityGroupIds
    {
        get
        {
            if (IdentityGroups is null)
                return Array.Empty<string>();
            return IdentityGroups
                .Select(kv => kv.Key)
                .Where(k => k.StartsWith(
                    "IDENTITY_GROUP_", StringComparison.Ordinal))
                .OrderBy(k => k, StringComparer.Ordinal)
                .ToList();
        }
    }
}
