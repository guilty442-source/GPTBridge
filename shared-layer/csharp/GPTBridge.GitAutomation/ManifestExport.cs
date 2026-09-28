using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace GPTBridge.GitAutomation;

/// <summary>
/// C# port of governance_rule.execution.audit.export_audit_manifest
/// (``star-audit-manifest/v1``).  Rebuilds the manifest the native audit
/// engine consumes — same check ids, kinds and ordering semantics as the
/// retired Python exporter.  Unevaluable inputs emit ``fail`` rows
/// rather than silently dropping coverage.
/// </summary>
internal static partial class ManifestExport
{
    private const string ManifestRelative =
        "governance_rule/execution/audit/audit_checks_manifest.json";

    private static readonly string[] CheckModules =
    {
        "audit_artifacts", "audit_codex_integrity", "audit_authority",
        "audit_activation", "audit_architecture", "audit_directories",
        "audit_formal_rules", "audit_manifests", "audit_protected",
        "audit_contract_axes", "audit_runtime_contracts",
        "audit_sql_patterns", "audit_optimization",
    };

    private static readonly HashSet<string> NativeCovered =
        new(StringComparer.Ordinal)
    {
        "check_protected_sources", "check_forbidden_legacy",
        "check_codex_consistency", "check_codex_mirror_quality",
        "check_runtime_contracts", "check_metadata_contract",
        "check_shared_layer_structure", "check_embedded_browser",
        "check_orphan_scanner", "check_git_tiers",
        "check_release_manifest_file", "check_release_manifest_module",
        "check_architecture_sources", "check_main_system_source",
        "check_reconcile_modules", "check_bootstrap_native_entry",
        "check_channel_gateway_csharp",
        "check_tool_host_native_boundary", "check_tool_manifests",
        "check_typescript_retirement", "check_tool_isolation_hardening",
        "check_third_party_inventory", "check_bounded_worker_pools",
        "check_codex_text_integrity", "check_authority_policy",
        "check_identity_permissions", "check_repair_policy",
        "check_shared_layer_policy", "check_activation_states",
        "check_architecture_registry", "check_directory_audit",
        "check_directory_schemas", "check_directory_catalog_coverage",
        "check_directory_identity_and_format",
        "check_directory_mirror_parity", "check_directory_relationships",
        "check_directory_seal", "check_provision_classification",
        "check_formal_rules", "check_implementation_obligations",
        "check_tool_identity_registration", "check_contract_axes",
        "check_sql_anti_patterns", "check_gpu_coordinator_lazy_torch",
        "check_renderer_idle_gating",
    };

    private static string Rel(string root, string relative) =>
        Path.Combine(root, relative.Replace('/', Path.DirectorySeparatorChar));

    private static string ReadText(string path)
    {
        try { return File.ReadAllText(path); }
        catch (IOException) { return ""; }
        catch (UnauthorizedAccessException) { return ""; }
    }

    private static JsonObject? ReadJsonObject(string path)
    {
        try
        {
            return JsonNode.Parse(File.ReadAllText(path)) as JsonObject;
        }
        catch (IOException) { return null; }
        catch (JsonException) { return null; }
        catch (UnauthorizedAccessException) { return null; }
    }

    /// <summary>Regenerate the manifest on disk — the governed refresh
    /// lane replacing export_audit_manifest.build_manifest.</summary>
    public static void Refresh(string root)
    {
        var manifest = Build(root);
        var path = Rel(root, ManifestRelative);
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        var wasReadonly = File.Exists(path)
            && (File.GetAttributes(path) & FileAttributes.ReadOnly) != 0;
        if (wasReadonly)
            File.SetAttributes(path,
                File.GetAttributes(path) & ~FileAttributes.ReadOnly);
        try
        {
            using var doc = JsonDocument.Parse(manifest.ToJsonString());
            Canon.WriteJsonAtomic(
                path, Canon.Indented(doc.RootElement) + "\n");
        }
        finally
        {
            if (wasReadonly)
                File.SetAttributes(path,
                    File.GetAttributes(path) | FileAttributes.ReadOnly);
        }
    }

    /// <summary>Check-id multiset diff between two manifest files —
    /// parity evidence for the C# port vs the retired Python lane.
    /// Exit 0 when the id multisets are identical.</summary>
    public static int Diff(string leftPath, string rightPath)
    {
        static List<string> Ids(string path)
        {
            var doc = ReadJsonObject(path);
            return doc?["checks"] is JsonArray checks
                ? checks.Select(c => c?["id"]?.GetValue<string>() ?? "")
                    .ToList()
                : new List<string>();
        }
        var left = Ids(leftPath);
        var right = Ids(rightPath);
        var removed = left.GroupBy(i => i)
            .Select(g => (Id: g.Key,
                N: g.Count() - right.Count(r => r == g.Key)))
            .Where(x => x.N > 0).OrderBy(x => x.Id).ToList();
        var added = right.GroupBy(i => i)
            .Select(g => (Id: g.Key,
                N: g.Count() - left.Count(r => r == g.Key)))
            .Where(x => x.N > 0).OrderBy(x => x.Id).ToList();
        Console.WriteLine($"left={leftPath} checks={left.Count}");
        Console.WriteLine($"right={rightPath} checks={right.Count}");
        Console.WriteLine(
            $"removed={removed.Sum(x => x.N)} " +
            $"added={added.Sum(x => x.N)}");
        foreach (var x in removed.Take(60))
            Console.WriteLine($"  - {x.Id} (x{x.N})");
        foreach (var x in added.Take(60))
            Console.WriteLine($"  + {x.Id} (x{x.N})");
        return removed.Count == 0 && added.Count == 0 ? 0 : 1;
    }

    private sealed class Emitter
    {
        public readonly List<JsonObject> Checks = new();
        public void Emit(string id, string kind, string path = "",
            Action<JsonObject>? extra = null)
        {
            var row = new JsonObject { ["id"] = id, ["kind"] = kind };
            if (path.Length > 0) row["path"] = path;
            extra?.Invoke(row);
            Checks.Add(row);
        }
        public void Contains(string id, string path,
            IEnumerable<string> markers, bool optional = false) =>
            Emit(id, "file-contains", path, r =>
            {
                r["markers"] = Arr(markers);
                if (optional) r["optional"] = true;
            });
        public void NotContains(string id, string path,
            IEnumerable<string> markers, bool optional = false) =>
            Emit(id, "file-not-contains", path, r =>
            {
                r["markers"] = Arr(markers);
                if (optional) r["optional"] = true;
            });
        public void Fail(string id, string reason) =>
            Emit(id, "fail", "", r => r["reason"] = reason);
        public static JsonArray Arr(IEnumerable<string> items) =>
            new(items.Select(m => (JsonNode?)JsonValue.Create(m))
                .ToArray());
    }

    private static Dictionary<string, PyLit.Value> Module(
        string root, string relative) =>
        PyLit.ModuleConstants(ReadText(Rel(root, relative)));

    private static PyLit.Value? ModuleVar(
        string root, string relative, string name) =>
        Module(root, relative).TryGetValue(name, out var v) ? v : null;

    private static List<string> ModuleStrings(
        string root, string relative, string name) =>
        PyLit.Strings(ModuleVar(root, relative, name));

    private static PyLit.Call? KwCall(PyLit.Value? value, string field) =>
        value is PyLit.Call c
            ? PyLit.AsCall(
                c.Kw.FirstOrDefault(k => k.Name == field).Val)
            : null;

    private static string? KwStr(PyLit.Call? call, string field) =>
        call is not null ? PyLit.KwStr(call, field) : null;

    private static List<string> KwStrings(
        PyLit.Call? call, string field) =>
        call is not null ? PyLit.KwStrings(call, field)
                         : new List<string>();

    // ------------------------------------------------------------------
    //  Python check-body reducers — structural recognizer over the
    //  canonical shapes in _reduce_marker_check /
    //  _reduce_dir_filelist_check.  Unrecognised bodies stay delegated
    //  (fail-closed) — a reduction is only claimed on a full skeleton
    //  match, so the port can never silently weaken a check.
    // ------------------------------------------------------------------

    private sealed record Line(string Text, int Depth);

    /// <summary>``(name, depth-tagged logical lines)`` for every
    /// top-level ``def check_*``.  Logical lines merge bracketed
    /// continuations; Depth is the dedented column level.</summary>
    private static IEnumerable<(string Name, List<Line> Body)>
        CheckBodies(string src)
    {
        var lines = src.Replace("\r\n", "\n").Split('\n');
        for (var i = 0; i < lines.Length; i++)
        {
            var match = Regex.Match(lines[i],
                @"^def (check_\w+)\s*\(");
            if (!match.Success) continue;
            var raw = new List<string>();
            var j = i + 1;
            var baseIndent = -1;
            for (; j < lines.Length; j++)
            {
                var l = lines[j];
                if (l.Trim().Length == 0) { raw.Add(""); continue; }
                var indent = 0;
                while (indent < l.Length && l[indent] == ' ') indent++;
                if (indent == 0) break;
                if (baseIndent < 0) baseIndent = indent;
                raw.Add(l[Math.Min(baseIndent, l.Length)..]);
            }
            yield return (match.Groups[1].Value, Logical(raw));
            i = j - 1;
        }
    }

    /// <summary>Merge bracket continuations into logical lines; depth
    /// = first line's dedented column / 4.</summary>
    private static List<Line> Logical(List<string> raw)
    {
        var result = new List<Line>();
        var buffer = new System.Text.StringBuilder();
        var bracket = 0;
        var depth = 0;
        var inStr = false;
        var strCh = '\0';
        var triple = false;
        var active = false;
        foreach (var line in raw)
        {
            var trimmed = line.TrimEnd();
            if (!active)
            {
                if (trimmed.Trim().Length == 0) continue;
                var indent = 0;
                while (indent < trimmed.Length && trimmed[indent] == ' ')
                    indent++;
                depth = indent / 4;
                buffer.Clear();
                active = true;
            }
            var text = trimmed.Trim();
            buffer.Append(text);
            for (var k = 0; k < text.Length; k++)
            {
                var c = text[k];
                if (inStr)
                {
                    if (triple)
                    {
                        if (c == strCh && k + 2 < text.Length
                            && text[k + 1] == strCh
                            && text[k + 2] == strCh)
                        { inStr = false; k += 2; }
                    }
                    else if (c == strCh
                             && (k == 0 || text[k - 1] != '\\'))
                        inStr = false;
                    continue;
                }
                if (c is '"' or '\'')
                {
                    inStr = true; strCh = c;
                    triple = k + 2 < text.Length
                        && text[k + 1] == c && text[k + 2] == c;
                    if (triple) k += 2;
                }
                else if (c == '#') break;
                else if (c is '(' or '[' or '{') bracket++;
                else if (c is ')' or ']' or '}') bracket--;
            }
            if (bracket <= 0 && !inStr)
            {
                result.Add(new Line(buffer.ToString(), depth));
                bracket = 0;
                active = false;
            }
            else
            {
                buffer.Append(' ');
            }
        }
        return result;
    }

    /// <summary>Direct children of the block header at index.</summary>
    private static (List<Line> Inner, int Next) Block(
        IReadOnlyList<Line> lines, int index)
    {
        var inner = new List<Line>();
        var childDepth = -1;
        var i = index + 1;
        for (; i < lines.Count; i++)
        {
            var d = lines[i].Depth;
            if (d <= lines[index].Depth) break;
            if (childDepth < 0) childDepth = d;
            if (d == childDepth) inner.Add(lines[i]);
        }
        return (inner, i);
    }

    private static readonly Regex PathJoinRe = new(
        @"^(\w+)\s*=\s*root\s*((?:/\s*(?:""[^""]*""|'[^']*')\s*)+)$",
        RegexOptions.Compiled);
    private static readonly Regex PathPartRe = new(
        @"/\s*""([^""]*)""|/\s*'([^']*)'", RegexOptions.Compiled);

    private static string? PathJoinLiteral(
        string statement, out string varName)
    {
        varName = "";
        var match = PathJoinRe.Match(statement);
        if (!match.Success) return null;
        varName = match.Groups[1].Value;
        var parts = new List<string>();
        foreach (Match part in
                 PathPartRe.Matches(match.Groups[2].Value))
            parts.Add(part.Groups[1].Success
                ? part.Groups[1].Value : part.Groups[2].Value);
        return parts.Count > 0 ? string.Join('/', parts) : null;
    }

    private static bool AllAppends(IEnumerable<Line> lines) =>
        lines.All(l => Regex.IsMatch(l.Text, @"^\w+\.append\("));

    private static string? TextReadTarget(string stmt, string pathVar)
    {
        var m = Regex.Match(stmt,
            @"^(\w+)\s*=\s*read_text_cached\(\s*(\w+)\s*\)$");
        if (m.Success && m.Groups[2].Value == pathVar)
            return m.Groups[1].Value;
        m = Regex.Match(stmt, @"^(\w+)\s*=\s*(\w+)\.read_text\(.*\)\s*$");
        if (m.Success && m.Groups[2].Value == pathVar)
            return m.Groups[1].Value;
        return null;
    }

    /// <summary>Depth-0 statements with their body indices.</summary>
    private static List<(Line L, int I)> Tops(
        IReadOnlyList<Line> body) =>
        body.Select((l, i) => (l, i))
            .Where(t => t.l.Depth == 0).ToList();

    private static bool Docstring(Line l) =>
        l.Text.StartsWith("\"\"\"") || l.Text.StartsWith("'''");

    /// <summary>_reduce_marker_check parity — depth-0 skeleton:
    /// ``path = root / ...`` → optional ``if not path.is_file():``
    /// return-guard → ``text = read_text_cached(path)`` → terminal
    /// ``for marker in (lit...):`` or ``if "lit" not in text:`` with
    /// append-only children.</summary>
    private static (string Relative, List<string> Markers)?
        ReduceMarkerCheck(List<Line> body)
    {
        var tops = Tops(body);
        var index = 0;
        if (index < tops.Count && Docstring(tops[index].L)) index++;
        if (tops.Count - index is < 2 or > 4) return null;
        var relative = PathJoinLiteral(tops[index].L.Text,
            out var pathVar);
        if (relative is null) return null;
        index++;
        if (index < tops.Count
            && tops[index].L.Text == $"if not {pathVar}.is_file():")
        {
            var (inner, _) = Block(body, tops[index].I);
            var guardOk = inner.Count > 0
                && inner.All(l => l.Text == "return"
                    || l.Text.StartsWith("return ")
                    || Regex.IsMatch(l.Text, @"^\w+\.append\("))
                && inner.Any(l => l.Text == "return"
                    || l.Text.StartsWith("return "));
            if (!guardOk) return null;
            index++;
        }
        if (index >= tops.Count) return null;
        var textVar = TextReadTarget(tops[index].L.Text, pathVar);
        if (textVar is null) return null;
        index++;
        if (index != tops.Count - 1) return null;
        var last = tops[index];
        return MarkerLoop(body, last, textVar) is { } loop
            ? (relative, loop)
            : SingleMarker(body, last, textVar) is { } single
                ? (relative, single) : null;
    }

    private static List<string>? MarkerLoop(
        IReadOnlyList<Line> body, (Line L, int I) header,
        string textVar)
    {
        var match = Regex.Match(header.L.Text,
            @"^for (\w+) in (\(.*\)|\[.*\]):$");
        if (!match.Success) return null;
        var iter = PyLit.Eval(match.Groups[2].Value);
        if (iter is not PyLit.Seq seq
            || seq.Items.Count == 0
            || seq.Items.Any(i => i is not PyLit.Str))
            return null;
        var (inner, _) = Block(body, header.I);
        if (inner.Count != 1) return null;
        var markerVar = match.Groups[1].Value;
        if (inner[0].Text != $"if {markerVar} not in {textVar}:")
            return null;
        var (appends, _) = Block(body, header.I + 1);
        if (appends.Count == 0 || !AllAppends(appends)) return null;
        return seq.Items.Select(i => ((PyLit.Str)i).Text).ToList();
    }

    private static List<string>? SingleMarker(
        IReadOnlyList<Line> body, (Line L, int I) header,
        string textVar)
    {
        var m = Regex.Match(header.L.Text,
            @"^if ""([^""]*)"" not in (\w+):$"
            + @"|^if '([^']*)' not in (\w+):$");
        if (!m.Success) return null;
        var varName = m.Groups[2].Success ? m.Groups[2].Value
            : m.Groups[4].Value;
        if (varName != textVar) return null;
        var (appends, _) = Block(body, header.I);
        if (appends.Count == 0 || !AllAppends(appends)) return null;
        return new List<string>
        {
            m.Groups[1].Success ? m.Groups[1].Value
                                : m.Groups[3].Value,
        };
    }

    /// <summary>_reduce_dir_filelist_check parity:  docstring? →
    /// ``dir = root / "..." / "..."`` → ``for name in (literals):``
    /// whose sole child is ``if not (dir / name).is_file():`` with
    /// append-only children.</summary>
    private static (string Dir, List<string> Names)?
        ReduceFilelistCheck(List<Line> body)
    {
        var tops = Tops(body);
        var index = 0;
        if (index < tops.Count && Docstring(tops[index].L)) index++;
        if (tops.Count - index != 2) return null;
        var dir = PathJoinLiteral(tops[index].L.Text, out var dirVar);
        if (dir is null) return null;
        var loop = Regex.Match(tops[index + 1].L.Text,
            @"^for (\w+) in (\(.*\)|\[.*\]):$");
        if (!loop.Success) return null;
        var iter = PyLit.Eval(loop.Groups[2].Value);
        if (iter is not PyLit.Seq seq
            || seq.Items.Count == 0
            || seq.Items.Any(i => i is not PyLit.Str))
            return null;
        var nameVar = loop.Groups[1].Value;
        var (inner, next) = Block(body, tops[index + 1].I);
        if (next != body.Count || inner.Count != 1) return null;
        if (!Regex.IsMatch(inner[0].Text,
                @"^if not \(\s*" + Regex.Escape(dirVar)
                + @"\s*/\s*" + Regex.Escape(nameVar)
                + @"\s*\)\.is_file\(\):$"))
            return null;
        var (appends, _) = Block(body, tops[index + 1].I + 1);
        if (appends.Count == 0 || !AllAppends(appends))
            return null;
        return (dir,
            seq.Items.Select(i => ((PyLit.Str)i).Text).ToList());
    }

    /// <summary>All check_* defs across the audit modules
    /// (_iter_python_check_names parity — order of first occurrence).</summary>
    private static List<string> PythonCheckNames(string root)
    {
        var names = new List<string>();
        foreach (var module in CheckModules)
        {
            var src = ReadText(Rel(root,
                $"governance_rule/execution/audit/{module}.py"));
            foreach (var (name, _) in CheckBodies(src))
                if (!names.Contains(name))
                    names.Add(name);
        }
        return names;
    }

    private static Dictionary<string, (string, List<string>)>
        ReducibleMarkers(string root)
    {
        var reducible = new Dictionary<string, (string, List<string>)>(
            StringComparer.Ordinal);
        foreach (var module in CheckModules)
        {
            var src = ReadText(Rel(root,
                $"governance_rule/execution/audit/{module}.py"));
            foreach (var (name, body) in CheckBodies(src))
            {
                if (reducible.ContainsKey(name)) continue;
                var reduced = ReduceMarkerCheck(body);
                if (reduced is { } r)
                    reducible[name] = (r.Relative, r.Markers);
            }
        }
        return reducible;
    }

    private static Dictionary<string, (string, List<string>)>
        ReducibleFilelists(string root)
    {
        var reducible = new Dictionary<string, (string, List<string>)>(
            StringComparer.Ordinal);
        foreach (var module in CheckModules)
        {
            var src = ReadText(Rel(root,
                $"governance_rule/execution/audit/{module}.py"));
            foreach (var (name, body) in CheckBodies(src))
            {
                if (reducible.ContainsKey(name)) continue;
                var reduced = ReduceFilelistCheck(body);
                if (reduced is { } r)
                    reducible[name] = (r.Dir, r.Names);
            }
        }
        return reducible;
    }

    // ------------------------------------------------------------------
    //  Build — emit order mirrors export_audit_manifest.build_manifest.
    // ------------------------------------------------------------------

    public static JsonObject Build(string root)
    {
        var e = new Emitter();
        var ctx = new Ctx(root, e);
        LoadSnapshots(ctx);
        EmitForbiddenAndProtected(ctx);
        EmitPollutionAndContracts(ctx);
        var reducible = ReducibleMarkers(root);
        foreach (var (name, (relative, markers)) in reducible)
            e.Emit($"module-markers:{name}", "file-contains", relative,
                r => r["markers"] = Emitter.Arr(markers));
        var filelist = ReducibleFilelists(root);
        foreach (var (name, (dir, names)) in filelist)
            foreach (var filename in names)
                e.Emit($"dir-filelist:{name}:{filename}", "file-exists",
                    $"{dir}/{filename}");
        EmitStaticSection(ctx);
        EmitMirrorAndPolicy(ctx);
        EmitToolManifests(ctx);
        EmitRemainder(ctx);
        EmitSourceOwnership(ctx);
        var covered = new HashSet<string>(NativeCovered,
            StringComparer.Ordinal);
        covered.UnionWith(reducible.Keys);
        covered.UnionWith(filelist.Keys);
        foreach (var name in PythonCheckNames(root))
            if (!covered.Contains(name))
                e.Checks.Add(new JsonObject
                {
                    ["id"] = $"python-check:{name}",
                    ["kind"] = "delegated",
                    ["reason"] = "python oracle (transition)",
                    ["python"] = name,
                });
        return new JsonObject
        {
            ["schema"] = "star-audit-manifest/v1",
            ["generated_at"] = Canon.UtcNow(),
            ["generator"] = "governance_rule.execution.audit." +
                            "export_audit_manifest",
            ["coverage"] = new JsonObject
            {
                ["native_checks"] = e.Checks.Count(
                    c => c["kind"]?.GetValue<string>() != "delegated"),
                ["delegated_checks"] = e.Checks.Count(
                    c => c["kind"]?.GetValue<string>() == "delegated"),
                ["native_kinds"] = new JsonArray(
                    "file-exists", "file-not-exists", "file-readonly",
                    "dir-exists", "file-contains", "file-not-contains",
                    "file-not-contains-unless",
                    "text-no-pollution", "json-parses", "json-has-keys",
                    "json-key-absent",
                    "glob-min-count", "glob-not-contains", "glob-absent",
                    "glob-contains", "json-key-value", "py-bucket-budget",
                    "json-array-min-count", "fail"),
            },
            ["checks"] = new JsonArray(
                e.Checks.Select(c => (JsonNode?)c.DeepClone()).ToArray()),
        };
    }

    private sealed class Ctx
    {
        public readonly string Root;
        public readonly Emitter E;
        public Dictionary<string, PyLit.Value>? Policy;
        public PyLit.Call? PolicyCall;
        public Dictionary<string, PyLit.Value>? Directory;
        public Dictionary<string, PyLit.Value>? CodeRules;
        public PyLit.Call? CodeRulesCall;
        public List<Identity> Identities = new();
        public List<(string Actor, List<string> Caps)> Bindings = new();
        public List<string> CapabilityNames = new();
        public Dictionary<string, (string File, JsonArray Rows)>
            MirrorTables = new(StringComparer.Ordinal);
        public HashSet<string> RetiredIds = new(StringComparer.Ordinal);
        public HashSet<string> NonIndependent = new(StringComparer.Ordinal);
        public HashSet<string> PresentSeen = new(StringComparer.Ordinal);

        public Ctx(string root, Emitter e) { Root = root; E = e; }

        public PyLit.Call? PolicyKw(string field) =>
            KwCall(PolicyCall, field);
        public string? PolicyStr(string field) =>
            KwStr(PolicyCall, field);
        public List<string> PolicyStrings(string field) =>
            KwStrings(PolicyCall, field);
    }

    private sealed record Identity(
        string Actor, string Code, string BoundToolId, string Lifecycle);
}
