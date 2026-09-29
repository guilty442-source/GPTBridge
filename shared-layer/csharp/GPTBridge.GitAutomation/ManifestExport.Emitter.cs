using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace GPTBridge.GitAutomation;

internal static partial class ManifestExport
{
    private sealed class Emitter
    {
        public readonly List<JsonObject> Checks = new();
        public string Root = "";

        private static readonly Regex KwLiteral = new(
            @"^([A-Za-z_][A-Za-z0-9_]*)\s*(?::\s*[^=]+)?=(.+)$",
            RegexOptions.Compiled);

        /// <summary>Check paths follow the native JSON port
        /// (<c>name.json</c> beside a retired <c>name.py</c>) when the
        /// Python source is gone — same rule as <see cref="Module"/>.</summary>
        public string PortPath(string path)
        {
            if (Root.Length == 0
                || !path.EndsWith(".py", StringComparison.Ordinal))
                return path;
            var ported = path[..^3] + ".json";
            var gone = !File.Exists(
                Path.Combine(Root, path.Replace('/', '\\')));
            return gone && File.Exists(
                Path.Combine(Root, ported.Replace('/', '\\')))
                ? ported : path;
        }

        /// <summary>Translate a Python-literal marker
        /// (<c>name="v"</c>, <c>name=True</c>, <c>name=False</c>) into the
        /// JSON-port shape (<c>"name": "v"</c>, <c>"name": true</c>,
        /// <c>"name": false</c>).  Bare markers pass through — a quoted
        /// string occurs verbatim in both syntaxes.</summary>
        public static string PortMarker(string marker, bool ported)
        {
            if (!ported) return marker;
            var match = KwLiteral.Match(marker);
            if (!match.Success) return marker;
            var value = match.Groups[2].Value.Trim();
            value = value switch
            {
                "True" => "true",
                "False" => "false",
                _ => value,
            };
            return $"\"{match.Groups[1].Value}\": {value}";
        }

        public void Emit(string id, string kind, string path = "",
            Action<JsonObject>? extra = null)
        {
            var ported = kind is "file-contains" or "file-not-contains"
                or "file-exists"
                ? PortPath(path) : path;
            var row = new JsonObject { ["id"] = id, ["kind"] = kind };
            if (ported.Length > 0) row["path"] = ported;
            extra?.Invoke(row);
            if (ported != path
                && row["markers"] is JsonArray markers)
                row["markers"] = Arr(markers
                    .OfType<JsonValue>()
                    .Select(node => PortMarker(
                        node.GetValue<string>(), true)));
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

    /// <summary>Registry constants: prefer the native JSON port
    /// (<c>name.json</c> beside the retired <c>name.py</c>), fall back to
    /// the Python-literal source while it still exists.</summary>
    private static Dictionary<string, PyLit.Value> Module(
        string root, string relative)
    {
        var jsonRel = Regex.Replace(relative, @"\.py$", ".json");
        var jsonPath = Rel(root, jsonRel);
        if (jsonRel != relative && File.Exists(jsonPath))
            return PyLit.ModuleConstantsJson(ReadText(jsonPath));
        return PyLit.ModuleConstants(ReadText(Rel(root, relative)));
    }

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

}
