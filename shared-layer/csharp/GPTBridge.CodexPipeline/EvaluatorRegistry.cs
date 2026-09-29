using System.Text.RegularExpressions;

namespace GPTBridge.CodexPipeline;

/// <summary>
/// Machine-evaluator registry view for the amendment pipeline.
///
/// ``_evaluator_codes()`` in the Python oracle resolves the set of rule
/// codes that carry a machine predicate by importing
/// ``formal_rules.evaluators``.  With the Python lane retired the
/// evaluator module survives as frozen contract text under
/// ``permission_directory/database/frozen/evaluators.txt`` (byte-exact
/// capture, see ``frozen_sources.json``); this reader still extracts the
/// declared ``@register_rule("CODE")`` bindings statically — the same
/// frozen-source convention the manifest exporter uses — so the
/// evaluator set is authoritative without executing Python.  A missing
/// or registration-free file is fail-closed.
/// </summary>
internal static class EvaluatorRegistry
{
    private static readonly Regex Registration = new(
        @"@register_rule\(\s*""([A-Za-z0-9_\-]+)""\s*\)",
        RegexOptions.Compiled);

    private static readonly Regex RegistrationSingle = new(
        @"@register_rule\(\s*'([A-Za-z0-9_\-]+)'\s*\)",
        RegexOptions.Compiled);

    public static string EvaluatorsPath() =>
        Path.Combine(Repo.Root(), "governance_rule",
            "permission_directory", "database", "frozen",
            "evaluators.txt");

    /// <summary>Set of rule codes with a registered machine evaluator.</summary>
    public static HashSet<string> RegisteredRuleCodes()
    {
        var path = EvaluatorsPath();
        if (!File.Exists(path))
            throw new InvalidOperationException(
                $"FORMAL_RULE_EVALUATOR_REGISTRY_UNAVAILABLE:{path}");
        var text = File.ReadAllText(path);
        var codes = new HashSet<string>(StringComparer.Ordinal);
        foreach (Match m in Registration.Matches(text))
            codes.Add(m.Groups[1].Value);
        foreach (Match m in RegistrationSingle.Matches(text))
            codes.Add(m.Groups[1].Value);
        if (codes.Count == 0)
            throw new InvalidOperationException(
                "FORMAL_RULE_EVALUATOR_REGISTRY_UNAVAILABLE:empty");
        return codes;
    }
}
