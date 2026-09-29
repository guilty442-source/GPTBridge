using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace GPTBridge.GitAutomation;

internal static partial class ManifestExport
{
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
        PyLit.ModuleConstants(SourceText(root, relative));

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
