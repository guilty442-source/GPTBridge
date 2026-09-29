// Native renderer build driver — C# 14 port of
// scripts/packager/renderer_build.py (B168/E35).
//
// Build chain: standalone SWC transform -> ESM -> standalone esbuild
// bundle.  Identical contract to the retired Python driver:
//   * SWC (.tools/swc/swc.exe) transpiles JSX to plain ESM into a staging
//     tree under .tools/jsdeps/build that mirrors the workspace layout so
//     cross-repo relative imports resolve identically inside the stage.
//     The staged .tools/jsdeps/node_modules ancestor supplies the
//     vendored, pinned React/scheduler artifacts.
//   * esbuild (.tools/esbuild/esbuild.exe) bundles the staged ESM entry
//     (with css loader) into <out>/assets/index.{js,css}.
//   * The tool/main index.html is rewritten to the emitted asset paths.
//
// No Node.js, npm, bun or vite involvement — both binaries are governed
// build-time tools (B169 exception: standalone esbuild/swc never count
// as runtime consumers).  Python runtime retired (B167/B38): the driver
// itself must not depend on an interpreter.

using System.Diagnostics;
using System.Text;
using System.Text.Json;

namespace GPTBridge.Bootstrap;

internal static partial class Program
{
    private static string ToolsDir => Path.Combine(WorkspaceRoot, ".tools");
    private static string SwcExe => Path.Combine(ToolsDir, "swc", "swc.exe");
    private static string EsbuildExe =>
        Path.Combine(ToolsDir, "esbuild", "esbuild.exe");
    private static string BuildStage =>
        Path.Combine(ToolsDir, "jsdeps", "build");
    private static string Swcrc => Path.Combine(WorkspaceRoot, ".swcrc");
    private static string MainRendererSrc =>
        Path.Combine(ProjectRoot, "src-ui", "renderer");
    private static string MainRendererOut =>
        Path.Combine(ProjectRoot, "dist-ui", "renderer");

    // Shared UI surfaces transformed alongside the main renderer so
    // relative imports from tool UIs resolve inside the mirrored stage.
    private static string[] SharedStageSources =>
    [
        Path.Combine(ProjectRoot, "src-ui", "renderer"),
        Path.Combine(WorkspaceRoot, "shared-layer", "src", "ui"),
        Path.Combine(WorkspaceRoot, "shared-layer", "src", "tool_ui_shared"),
    ];

    private static bool _sharedStageReady;

    /// Stage parent such that <parent>/<basename> mirrors the workspace
    /// layout of srcDir.
    private static string StageParent(string srcDir)
    {
        var parentFull = Path.GetFullPath(
            Path.GetDirectoryName(srcDir) ?? srcDir);
        var relParent = Path.GetRelativePath(WorkspaceRoot, parentFull);
        return Path.Combine(BuildStage, relParent);
    }

    /// Two-pass transform: .jsx keeps .jsx, .js keeps .js (specifier
    /// parity); non-compilable assets (html/css/json) are copied.
    private static string SwcTransform(string srcDir)
    {
        var stageParent = StageParent(srcDir);
        var stagedDir = Path.Combine(
            stageParent, Path.GetFileName(srcDir));
        // SWC compile never deletes files removed from the source tree —
        // a stale stage would keep emitting retired modules.  Re-stage
        // cleanly so the bundle always reflects the current sources.
        if (Directory.Exists(stagedDir))
        {
            Directory.Delete(stagedDir, recursive: true);
        }
        Directory.CreateDirectory(stageParent);
        foreach (var ext in new[] { ".jsx", ".js" })
        {
            InvokeLauncherCommand(SwcExe, new[]
            {
                "compile", srcDir,
                "--out-dir", stageParent,
                "--config-file", Swcrc,
                "--extensions", ext,
                "--out-file-extension", ext.TrimStart('.'),
                "--copy-files",
            }, WorkspaceRoot);
        }
        return stagedDir;
    }

    /// Stage the shared UI trees once per build run (main renderer +
    /// shared-layer).  Always re-transforms — an exists-check would
    /// serve stale modules after shared sources change between runs.
    private static string EnsureSharedStage()
    {
        if (!_sharedStageReady)
        {
            foreach (var src in SharedStageSources)
            {
                if (Directory.Exists(src))
                {
                    SwcTransform(src);
                }
            }
            _sharedStageReady = true;
        }
        return Path.Combine(
            StageParent(MainRendererSrc), Path.GetFileName(MainRendererSrc));
    }

    private static void EsbuildBundle(
        string entry, string outDir,
        IReadOnlyList<KeyValuePair<string, string>> aliases,
        string format)
    {
        if (Directory.Exists(outDir))
        {
            Directory.Delete(outDir, recursive: true);
        }
        Directory.CreateDirectory(outDir);
        var argv = new List<string>
        {
            entry,
            "--bundle", $"--format={format}", "--minify",
            "--target=es2022",
            "--outfile=" + Path.Combine(outDir, "assets", "index.js"),
            "--loader:.css=css",
        };
        foreach (var alias in aliases)
        {
            argv.Add($"--alias:{alias.Key}={alias.Value}");
        }
        InvokeLauncherCommand(EsbuildExe, argv, WorkspaceRoot);
    }

    private static void RewriteIndexHtml(
        string stageHtml, string outDir, bool module)
    {
        var html = File.ReadAllText(stageHtml);
        foreach (var entryName in new[] { "main.js", "main.jsx" })
        {
            if (module)
            {
                html = html.Replace(
                    $"src=\"/{entryName}\"", "src=\"./assets/index.js\"");
                html = html.Replace(
                    $"src=\"{entryName}\"", "src=\"./assets/index.js\"");
            }
            else
            {
                // Tool windows load via file:// where module scripts are
                // blocked (opaque-origin CORS) — emit a classic deferred
                // script tag.
                html = html.Replace(
                    $"type=\"module\" src=\"/{entryName}\"",
                    "defer src=\"./assets/index.js\"");
                html = html.Replace(
                    $"type=\"module\" src=\"{entryName}\"",
                    "defer src=\"./assets/index.js\"");
            }
        }
        html = html.Replace(
            "href=\"index.css\"", "href=\"./assets/index.css\"");
        html = html.Replace(
            "href=\"/index.css\"", "href=\"./assets/index.css\"");
        File.WriteAllText(
            Path.Combine(outDir, "index.html"), html,
            new UTF8Encoding(encoderShouldEmitUTF8Identifier: false));
    }

    private static string BuildMainRenderer()
    {
        var staged = EnsureSharedStage();
        // Native ESM entry (C116/E180): main.js is the React-free entry;
        // the retired main.jsx is no longer the startup path.
        var entry = Path.Combine(staged, "main.js");
        if (!File.Exists(entry))
        {
            entry = Path.Combine(staged, "main.jsx");
        }
        EsbuildBundle(entry, MainRendererOut, new[]
        {
            new KeyValuePair<string, string>("@", staged),
            new KeyValuePair<string, string>(
                "@main-locales", Path.Combine(ProjectRoot, "locales")),
            new KeyValuePair<string, string>(
                "@resources", Path.Combine(ProjectRoot, "resources")),
        }, "esm");
        RewriteIndexHtml(
            Path.Combine(staged, "index.html"), MainRendererOut, true);
        return MainRendererOut;
    }

    /// Per-tool renderer (vite.platform-tools.config.mjs parity):
    /// transform the tool's src/ui into the mirrored stage, then bundle
    /// with @ -> staged main renderer, @resources -> main-system
    /// resources.
    private static bool BuildToolRenderer(
        string toolId, string uiRoot, string outDir, out string message)
    {
        var mainStage = EnsureSharedStage();
        var staged = SwcTransform(Path.GetFullPath(uiRoot));
        var entry = Path.Combine(staged, "main.jsx");
        if (!File.Exists(entry))
        {
            entry = Path.Combine(staged, "main.js");
        }
        if (!File.Exists(entry))
        {
            message = $"missing ui entry under {uiRoot}";
            return false;
        }
        EsbuildBundle(entry, outDir, new[]
        {
            new KeyValuePair<string, string>("@", mainStage),
            new KeyValuePair<string, string>(
                "@resources", Path.Combine(ProjectRoot, "resources")),
        }, "iife");
        RewriteIndexHtml(
            Path.Combine(staged, "index.html"), outDir, false);
        message = "";
        return true;
    }

    /// (tool_id, src/ui root) for every Standalone tool with a renderer.
    private static List<KeyValuePair<string, string>> IterToolUiRoots()
    {
        var roots = new List<KeyValuePair<string, string>>();
        var standalone = Path.Combine(WorkspaceRoot, "Standalone tools");
        if (!Directory.Exists(standalone))
        {
            return roots;
        }
        foreach (var manifest in Directory.EnumerateFiles(
            standalone, "manifest.json", SearchOption.AllDirectories))
        {
            var toolDir = Path.GetDirectoryName(manifest) ?? "";
            var toolId = Path.GetFileName(toolDir);
            try
            {
                using var doc = JsonDocument.Parse(
                    File.ReadAllText(manifest));
                if (doc.RootElement.TryGetProperty("id", out var idEl)
                    && idEl.ValueKind == JsonValueKind.String)
                {
                    toolId = idEl.GetString() ?? toolId;
                }
            }
            catch (Exception)
            {
                // Unparseable manifest — fall back to the directory name.
            }
            var uiRoot = Path.Combine(toolDir, "src", "ui");
            if (File.Exists(Path.Combine(uiRoot, "index.html")))
            {
                roots.Add(new KeyValuePair<string, string>(
                    toolId, uiRoot));
            }
        }
        return roots;
    }

    /// Native equivalent of ``renderer_build.py --all|--main|--tools``.
    private static void NativeRendererBuild(string target)
    {
        if (target is not ("--all" or "--main" or "--tools"))
        {
            throw new InvalidOperationException(
                $"unknown target: {target}");
        }
        if (target is "--all" or "--main")
        {
            var rendererPath = BuildMainRenderer();
            WriteLauncherStatus($"main renderer -> {rendererPath}");
        }
        if (target is "--all" or "--tools")
        {
            foreach (var tool in IterToolUiRoots())
            {
                var outDir = Path.Combine(
                    ProjectRoot, "dist-ui", "independent-tools",
                    tool.Key, "renderer");
                if (!BuildToolRenderer(
                        tool.Key, tool.Value, outDir, out var message))
                {
                    throw new InvalidOperationException(
                        $"{tool.Key}: {message}");
                }
                WriteLauncherStatus($"{tool.Key} -> {outDir}");
            }
        }
    }
}
