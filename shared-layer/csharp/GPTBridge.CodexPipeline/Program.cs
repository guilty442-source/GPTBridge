using System.Text.Json.Nodes;
using GPTBridge.CodexPipeline;

/// <summary>
/// GPTBridge Codex amendment pipeline CLI — the C# entry point that
/// replaces ``python -m governance_rule.execution.codex_amendment_driver``
/// and ``codex_amendment_executor``.
///
/// Driver mode:
///   --scan                     discover staged request artifacts
///   --request &lt;path&gt; [--source &lt;sql&gt;] [--auto-execute]
///   --all [--source &lt;sql&gt;] [--auto-execute]
///
/// Executor mode (governor-invoked):
///   --execute --request &lt;path&gt; [--prepared &lt;sql&gt;]
///       [--staging &lt;dir&gt;] [--audit-result &lt;json&gt;] [--apply]
///
/// Parity probes (read-only, used by the oracle-parity harness):
///   --canonical &lt;json&gt;         canonical JSON text
///   --hash-content &lt;json&gt;      content_hash of a JSON payload
///   --hash-revision &lt;json&gt;     revision_entry_hash of a field map
///   --hash-doc &lt;text-file&gt;     search_document_hash of raw text
///   --seal-preview &lt;sql&gt;       compute_seal_preview for an artifact
///   --artifact-tables &lt;sql&gt;    artifact_table_names
///   --artifact-version &lt;sql&gt;   artifact_version
///   --roundtrip &lt;sql&gt; &lt;out&gt;    materialize + dump (byte parity)
///   --export &lt;target&gt;          export_postgresql_codex
///   --authority-state          codex_authority_state row
///   --capture-schema-snapshot &lt;schema&gt; &lt;out&gt;
///                              read-only gptbridge_* schema capture
///                              to a pinned snapshot artifact
///                              (PostgreSQL retirement route Phase 2+)
///   --capture-codex-snapshot &lt;generation&gt; &lt;out&gt;
///                              generation-pinned read-only codex
///                              capture via NativeCodexMigration
///                              (fails closed on generation drift)
///   --verify-parity &lt;sql&gt;      verify_sql_parity
///
/// Governed migration executor (sql_migration_executor_contract):
///   --migration-status         registry↔source↔receipt reconciliation
///                              report (read-only, runtime DSN)
///   --migration-apply          governed single-migration apply
///       --sequence &lt;n&gt;         sequence to apply (fail-closed)
///       [--migration-db &lt;db&gt;]    scratch-db test hook (never the
///                              governed DB by accident)
///   --schema-parity            machine-schema parity probe, recipe
///                              SEAL_CANONICAL_V1 (read-only; C# port of
///                              the retired semantic_hash_toolchain.py)
///
/// Maintenance:
///   --repair-projections       rebuild live gptbridge_codex derived
///                              search/index/manifest projections
///   --arch-projections         refresh autogen blocks inside the
///                              governance_rule/codex/architecture-*.md
///                              view documents (autogen-scanner/v1)
///   --mirror-zh                re-render the five zh-TW mirror parts
///                              from the live authority into the
///                              canonical codex root (read-only output)
///   --mirror-check             validate the existing five zh-TW mirror
///                              parts (chain/hash/parity/damage)
///                              read-only; never re-renders
///   --arch-docs                architecture registry × docs
///                              completeness report (read-only)
///
/// Official read entry (A113/A435, port of codex_session/dual_key/
/// official):
///   --codex-read --actor &lt;a&gt; --purpose &lt;p&gt; --scope &lt;csv&gt;
///       [--class &lt;c&gt;] [--ttl &lt;s&gt;] [--grant &lt;nonce&gt;]
///       --op &lt;op&gt; [--arg &lt;v&gt;]
///   --mint-dual-key --operation &lt;op&gt; --primary &lt;a&gt;
///       --secondary &lt;s&gt; --purpose &lt;p&gt; --scope &lt;csv&gt;
///       [--class &lt;c&gt;] [--ttl &lt;s&gt;]
///   --revoke-codex-reads       bump the revocation generation
///   --official-sovereign &lt;sid&gt;  self-declaration read
///
/// Global:
///   --root &lt;dir&gt;             repository root override
/// </summary>
internal static partial class Program
{
    private static void Usage()
    {
        Console.Error.WriteLine(
            "usage: GPTBridge.CodexPipeline --scan | --request <path> |"
            + " --all [--source <sql>] [--auto-execute]\n"
            + "       GPTBridge.CodexPipeline --execute --request <path>"
            + " [--prepared <sql>] [--staging <dir>]"
            + " [--audit-result <json>] [--apply]\n"
            + "       [--root <dir>]");
    }

    private static int Emit(object? payload)
    {
        Console.WriteLine(PrettyJson.Serialize(payload));
        return 0;
    }

    private static string[] SplitScope(string csv) =>
        csv.Split(',', StringSplitOptions.RemoveEmptyEntries
            | StringSplitOptions.TrimEntries);

    private static Dictionary<string, object?>? SovereignJson(
        CodexSovereign? sovereign) => sovereign is null ? null
        : new Dictionary<string, object?>(StringComparer.Ordinal)
        {
            ["id"] = sovereign.Id,
            ["name"] = sovereign.Name,
            ["area"] = sovereign.Area,
            ["rank"] = sovereign.Rank,
            ["duties"] = sovereign.Duties.Cast<object?>().ToList(),
            ["powers"] = sovereign.Powers.Cast<object?>().ToList(),
            ["prohibitions"] = sovereign.Prohibitions
                .Cast<object?>().ToList(),
            ["basis"] = sovereign.Basis,
        };

    /// <summary>Typed official-entry read ops (A435
    /// requested-scope-only).</summary>
    private static object? SessionOp(CodexReadSession session,
        string op, string? arg)
    {
        string Required()
        {
            if (arg is null)
                throw new ArgumentException($"--op {op} requires --arg");
            return arg;
        }
        switch (op)
        {
            case "identity": return session.CodexIdentity();
            case "sovereign":
                return SovereignJson(session.Sovereign(Required()));
            case "sovereigns":
                return session.Sovereigns()
                    .Select(SovereignJson).ToList();
            case "provision-exists":
                return session.ProvisionExists(Required());
            case "provision-text":
                return session.ProvisionText(Required());
            case "edicts":
                return session.EdictsByArea(Required())
                    .Cast<object?>().ToList();
            case "articles":
                return session.Articles().Select(a =>
                    (object?)new Dictionary<string, object?>(
                        StringComparer.Ordinal)
                    {
                        ["id"] = a.Id,
                        ["section"] = a.Section,
                        ["subject"] = a.Subject,
                        ["rule"] = a.Rule,
                        ["prohibition"] = a.Prohibition,
                        ["exception"] = a.Exception,
                    }).ToList();
            case "principles":
                return session.Principles().Select(p =>
                    (object?)new Dictionary<string, object?>(
                        StringComparer.Ordinal)
                    {
                        ["id"] = p.Id,
                        ["statement"] = p.Statement,
                        ["binding"] = p.Binding,
                    }).ToList();
            case "registry-names": return session.RegistryNames();
            case "directory-names": return session.DirectoryNames();
            case "registry": return session.Registry(Required());
            case "directory": return session.Directory(Required());
            case "snapshot":
                {
                    var codex = session.Snapshot();
                    return new Dictionary<string, object?>(
                        StringComparer.Ordinal)
                    {
                        ["schema"] = codex.Schema,
                        ["codex_version"] = codex.CodexVersion,
                        ["articles"] = (long)codex.Articles.Length,
                        ["principles"] = (long)codex.Principles.Length,
                        ["edicts"] = (long)codex.Edicts.Length,
                        ["sovereigns"] = (long)codex.Sovereigns.Length,
                        ["directories"] = (long)codex.Directories.Count,
                        ["registries"] = (long)codex.Registries.Count,
                    };
                }
            case "chinese-mirror":
                return new Dictionary<string, object?>(
                    StringComparer.Ordinal)
                {
                    ["mirror"] = session.ChineseMirror(),
                };
            default:
                throw new ArgumentException($"unknown --op '{op}'");
        }
    }

    private static string FileHash(string path)
    {
        using var stream = File.OpenRead(path);
        return Convert.ToHexString(
            System.Security.Cryptography.SHA256.HashData(stream))
            .ToLowerInvariant();
    }

}
