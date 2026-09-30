using System.Text.Json;
using System.Text.Json.Nodes;

namespace GPTBridge.CodexPipeline;

/// <summary>
/// Machine-evaluator registry view for the amendment pipeline.
///
/// ``_evaluator_codes()`` in the Python oracle resolves the set of rule
/// codes that carry a machine predicate by importing
/// ``formal_rules.evaluators``.  With the Python lane retired (B167/B38)
/// the authoritative registry is
/// ``governance_rule/execution/formal_rules/evaluators.json`` — a
/// ``star-rule-evaluators/v1`` document whose ``evaluators`` object maps
/// every registered rule code to its evaluator entry.  A missing or
/// empty registry is fail-closed.
/// </summary>
internal static class EvaluatorRegistry
{
    public static string EvaluatorsPath() =>
        Path.Combine(Repo.Root(), "governance_rule", "execution",
            "formal_rules", "evaluators.json");

    /// <summary>Set of rule codes with a registered machine evaluator.</summary>
    public static HashSet<string> RegisteredRuleCodes()
    {
        var path = EvaluatorsPath();
        if (!File.Exists(path))
            throw new InvalidOperationException(
                $"FORMAL_RULE_EVALUATOR_REGISTRY_UNAVAILABLE:{path}");
        JsonObject? doc;
        try
        {
            doc = JsonNode.Parse(File.ReadAllText(path)) as JsonObject;
        }
        catch (JsonException error)
        {
            throw new InvalidOperationException(
                $"FORMAL_RULE_EVALUATOR_REGISTRY_UNAVAILABLE:{path}:" +
                $"{error.Message}");
        }
        var codes = new HashSet<string>(StringComparer.Ordinal);
        if (doc?["evaluators"] is JsonObject evaluators)
            foreach (var (code, _) in evaluators)
                codes.Add(code);
        // Python parity: every DECLARED_PROVISION_RULES key was
        // auto-registered as a machine evaluator via
        // _declared_provision_evaluator(provision_id).
        if (doc?["DECLARED_PROVISION_RULES"] is JsonObject declared)
            foreach (var (code, _) in declared)
                codes.Add(code);
        // ``REGISTERED_RULE_CODES`` is the frozen union list emitted by
        // the Python module (machine predicates + declared rules +
        // post-loop registrations).
        if (doc?["REGISTERED_RULE_CODES"] is JsonArray registered)
            foreach (var item in registered)
                if (item is JsonValue value
                    && value.TryGetValue<string>(out var code)
                    && code.Length > 0)
                    codes.Add(code);
        if (codes.Count == 0)
            throw new InvalidOperationException(
                "FORMAL_RULE_EVALUATOR_REGISTRY_UNAVAILABLE:empty");
        return codes;
    }
}
