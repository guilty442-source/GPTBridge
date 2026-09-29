using System.Text.Json.Nodes;

namespace GPTBridge.CodexPipeline;


internal static class RepoTruthy
{
    /// <summary>Python truthiness for scalar cell values used in
    /// parity-evidence checks.</summary>
    public static bool TruthyText(object? value) => value switch
    {
        null => false,
        bool b => b,
        string s => s.Length > 0,
        _ => true,
    };
}
