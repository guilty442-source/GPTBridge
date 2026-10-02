// CapabilityRegressionMatrix.cs — ``star-capability-regression-matrix/v1``
// (maturation-closure directive §24-§29).
//
// Rows are candidate primary capabilities; columns are the protected
// capabilities (§14). A cell says whether the candidate's regression
// obligation to that column is ``required`` (§26 core set or
// graph-derived REQUIRES/REGRESSES_WITH), ``optional`` (protected but
// not graph-related) or ``not_applicable``. The matrix is derived —
// §25 forbids hand-written regression suites.
//
// §28 keeps runtime axes (system-1/MTP/KV/delta-state/prefix-cache/
// quantization/precision/native-CUDA/resource-response) as a separate
// block: they are runtime regression, never model-capability scores
// (§29 — neither side may borrow the other's pass).

namespace GPTBridge.XingchengLearning;

internal static class CapabilityRegressionMatrix
{
    public const string Format = "star-capability-regression-matrix/v1";

    /// <summary>§28 runtime regression surfaces — orthogonal to the
    /// capability columns; always required for a weight-bearing
    /// candidate.</summary>
    public static readonly string[] RuntimeAxes =
    {
        "system1", "mtp", "kv", "delta_state", "prefix_cache",
        "quantization", "precision", "native_cuda",
        "resource_response",
    };

    /// <summary>Emit the full matrix: every canonical capability as a
    /// row, every currently-protected capability as a column.</summary>
    public static Dictionary<string, object?> Emit(string toolRoot)
    {
        var protectedSet = new HashSet<string>(
            CapabilityMaturityService.Protected(toolRoot),
            StringComparer.Ordinal);
        var rows = new Dictionary<string, object?>();
        foreach (var d in CapabilityRegistry.Canonical)
            rows[d.CapabilityId] = RowFor(d.CapabilityId, toolRoot,
                protectedSet);
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["format"] = Format,
            ["protected_capabilities"] =
                protectedSet.OrderBy(x => x).Cast<object?>().ToList(),
            ["core_regression"] = CapabilityMaturityService
                .CoreRegression.Cast<object?>().ToList(),
            ["runtime_axes"] = RuntimeAxes.Cast<object?>().ToList(),
            ["rows"] = rows,
            ["rule"] = "cells are derived from the capability graph " +
                "+ protected set — never hand-written (§25)",
        };
    }

    /// <summary>Single row: the regression obligation of one candidate
    /// primary capability across every protected column.</summary>
    public static Dictionary<string, object?> RowFor(
        string capabilityInput, string toolRoot,
        HashSet<string>? protectedSet = null)
    {
        string cap = CapabilityResolver.Require(capabilityInput);
        protectedSet ??= new HashSet<string>(
            CapabilityMaturityService.Protected(toolRoot),
            StringComparer.Ordinal);
        var required = new HashSet<string>(
            CapabilityResolver.RegressionSuite(cap, toolRoot),
            StringComparer.Ordinal);
        // §26: core capabilities are required whenever protected.
        foreach (var c in CapabilityMaturityService.CoreRegression)
            if (protectedSet.Contains(c)) required.Add(c);

        var cells = new Dictionary<string, object?>();
        foreach (var p in protectedSet.OrderBy(x => x))
            cells[p] = p == cap ? "not_applicable"
                : required.Contains(p) ? "required" : "optional";
        return new Dictionary<string, object?>
        {
            ["capability_id"] = cap,
            ["cells"] = cells,
            ["runtime_axes"] = RuntimeAxes.Cast<object?>().ToList(),
            ["derived_from"] =
                "graph REQUIRES/REGRESSES_WITH + protected set + §26 core",
        };
    }
}
