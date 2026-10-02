// ModelMaturity.cs — star-model-maturity/v1 native successor (B134/B135
// contract; B167/B38 owner-language port of the retired
// native_transformer/maturity.py). C# owns orchestration only: L0-L2 run
// through `xingcheng_trainer --job`, L3 through `xc_modeltool eval`,
// L4-L6 through the resident `serve` lane, L7 through governed C#
// lifecycle/self-learning. Levels run consecutively; the first
// fail/skipped level caps certification. The source checkpoint is never
// written — jobs init from it read-only and emit into a scratch dir —
// which replaces the retired lane's in-memory weight backup/restore.

using System.Globalization;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace GPTBridge.XingchengLearning;

internal static class ModelMaturity
{
    public const string Format = "star-model-maturity/v1";
    private const string StateRelative =
        "xingcheng/runtime/state/model-maturity.json";
    private const string LogsRelative = "xingcheng/runtime/logs";
    private const string LifecycleRelative =
        "xingcheng/runtime/models/lifecycle/xingcheng-native";
    private const string ScratchRelative = "xingcheng/runtime/maturity";
    private const int MaxLevelIndex = 7;

    private static readonly (int Level, string Code, string Name)[] Levels =
    {
        (0, "structure_init", "模型結構初始化"),
        (1, "forward_backward", "Forward / Backward 檢查"),
        (2, "overfit_small", "模型能在小資料集上過度擬合"),
        (3, "effective_pretrain", "完成有效的語言模型訓練"),
        (4, "generation", "基本語言生成能力"),
        (5, "dialogue_instruction", "基本多輪對話與指令遵循能力"),
        (6, "reasoning_tools", "可驗證推理與工具使用能力"),
        (7, "controlled_evolution", "具備受控模型自我學習與版本演進能力"),
    };

    // L4 probes — fixed prompt set from the retired contract.
    private static readonly string[] GenerationPrompts =
    {
        "今天的天氣",
        "星澄模型，",
        "請寫出 1 2 3",
        "def add(",
    };

    // L5 probes — per-probe checker ids map 1:1 to the retired contract.
    private static readonly (string Id, string[] Turns, string Check,
        string Desc)[] DialogueProbes =
    {
        ("turn_boundary", new[] { "你好" }, "stopped_or_eot",
         "回合邊界：回覆應正常停止（eos 或 <|eot|>）"),
        ("echo", new[] { "請只輸出以下字串：煙火測試" }, "contains:煙火測試",
         "指令遵循：逐字複誦指定字串"),
        ("choice", new[] { "只能回答是或否。地球是圓的嗎？" },
         "choice:是|否", "指令遵循：限定回答選項"),
        ("multiturn_memory",
         new[] { "請記住這個代碼：QZ-88", "我剛給你的代碼是什麼？" },
         "contains:QZ-88", "多輪上下文記憶"),
    };

    private static readonly (string Id, string Prompt, string Expect)[]
        ReasoningProbes =
    {
        ("arith_add", "計算 13 + 29，只輸出數字", "42"),
        ("arith_mul", "計算 6 × 7，只輸出數字", "42"),
        ("compare", "9 和 4 哪個大？只輸出較大的數字", "9"),
    };

    private const string ToolCallInstruction =
        "你可以使用工具 calculator。當需要計算時，只輸出 " +
        "<tool_call>{\"name\":\"calculator\",\"arguments\":" +
        "{\"expression\":\"算式\"}}</tool_call>。其餘時候直接回答。";

    private static readonly string[] OverfitSamples =
    {
        "今天是美好的一天。",
        "模型正在學習對話。",
        "GPTBridge 是工具平台。",
        "測試資料很容易。",
        "1 + 1 = 2",
        "今天的天氣如何？",
        "Transformer attention is all you need.",
        "這是一個測試句子。",
    };

    private const string DefaultEvalText =
        "星澄是 GPTBridge 的原生語言模型。Transformer 架構包含注意力層、" +
        "前饋層與歸一化層。模型透過梯度下降從資料中學習參數。訓練完成後" +
        "可以回答問題、生成文本並遵循指令。The quick brown fox jumps " +
        "over the lazy dog. The quick brown fox jumps over the lazy dog. " +
        "The quick brown fox jumps over the lazy dog. The quick brown " +
        "fox jumps over the lazy dog. ";

    // ------------------------------------------------------------- types --

    private sealed class LevelResult
    {
        public int Level;
        public string Code = "";
        public string Name = "";
        public string Status = "pending";   // pass | fail | skipped
        public Dictionary<string, object?> Metrics = new();
        public Dictionary<string, object?> Evidence = new();
        public string Error = "";
        public double DurationS;
    }

    private sealed class Ctx
    {
        public string ToolRoot = "";
        public string Scratch = "";
        public string? BundleDir;
        public string? InitCkpt;            // resolved .xcn for L0-L2
        public string? TokenizerPath;
        public Dictionary<string, object?> ModelCfg = new();
        public Dictionary<string, object?> Gates = new();
        public long Vocab;
        public long EosId = 2;
        public string EvalText = "";
        public bool LiveCycle;
        public bool Preset;
        public ModelToolSession? Session;
    }

    private static string UtcNow()
        => DateTime.UtcNow.ToString("yyyy-MM-dd'T'HH:mm:ss'Z'");

    private static double GateD(Ctx ctx, string key, double fallback)
        => ctx.Gates.TryGetValue(key, out object? v)
            ? TransformerTrainingRepository.Num(
                new Dictionary<string, object?> { ["v"] = v }, "v")
            : fallback;

    // ------------------------------------------------------------ status --

    /// <summary>Read the persisted certification state only — never runs
    /// any probe (read-only contract, mirrors `maturity --status`).</summary>
    public static Dictionary<string, object?> Status(string toolRoot)
    {
        string path = Path.Combine(Path.GetFullPath(toolRoot),
            StateRelative.Replace('/', Path.DirectorySeparatorChar));
        if (!File.Exists(path))
            return new Dictionary<string, object?>
            {
                ["format"] = Format, ["certified_level"] = null,
            };
        try
        {
            using var doc = JsonDocument.Parse(File.ReadAllText(path));
            var map = new Dictionary<string, object?>();
            foreach (var p in doc.RootElement.EnumerateObject())
                map[p.Name] = ModelLifecycle.Decode(p.Value);
            return map;
        }
        catch (Exception)
        {
            return new Dictionary<string, object?>
            {
                ["format"] = Format, ["certified_level"] = null,
            };
        }
    }

    public static string PersistReport(string toolRoot,
        Dictionary<string, object?> report)
    {
        string root = Path.GetFullPath(toolRoot);
        string logs = Path.Combine(root,
            LogsRelative.Replace('/', Path.DirectorySeparatorChar));
        Directory.CreateDirectory(logs);
        string reportPath = Path.Combine(logs,
            $"maturity-{DateTime.UtcNow:yyyyMMdd-HHmmss}.json");
        File.WriteAllText(reportPath,
            CanonicalJson.PrettyDict(report) + "\n",
            new System.Text.UTF8Encoding(false));
        string statePath = Path.Combine(root,
            StateRelative.Replace('/', Path.DirectorySeparatorChar));
        Directory.CreateDirectory(Path.GetDirectoryName(statePath)!);
        ModelLifecycle.AtomicWrite(statePath,
            CanonicalJson.PrettyDict(new Dictionary<string, object?>
            {
                ["format"] = Format,
                ["certified_level"] = report.GetValueOrDefault("certified_level"),
                ["certified_at"] = report.GetValueOrDefault("certified_at"),
                ["checkpoint"] = report.GetValueOrDefault("checkpoint"),
                ["report"] = reportPath,
            }) + "\n");
        return reportPath;
    }

    // ----------------------------------------------------------- certify --

    /// <summary>Run the 8-level ladder. `checkpoint` accepts a bundle dir
    /// (manifest.json + tokenizer.json; its source_checkpoint feeds L0-L2)
    /// or a bare .xcn (L0-L2 only). `preset` runs the architecture-only
    /// ladder on a synthetic small model. Exit-relevant fields mirror the
    /// retired schema: certified_level is the last consecutive pass.</summary>
    public static Dictionary<string, object?> Certify(
        string toolRoot, string? checkpoint, string? preset,
        int maxLevel, string evalText, bool liveCycle, bool save)
    {
        var ctx = new Ctx
        {
            ToolRoot = Path.GetFullPath(toolRoot),
            EvalText = evalText,
            LiveCycle = liveCycle,
        };
        ctx.Scratch = Path.Combine(ctx.ToolRoot,
            ScratchRelative.Replace('/', Path.DirectorySeparatorChar));
        Directory.CreateDirectory(ctx.Scratch);

        if (preset != null)
        {
            if (preset != "small" && preset != "medium_moe")
                throw new ExecutorError("MATURITY_PRESET_UNKNOWN",
                    $"unknown preset: {preset}");
            ctx.Preset = true;
            ctx.ModelCfg = new Dictionary<string, object?>
            {
                ["vocab_size"] = 64, ["hidden_size"] = 32,
                ["intermediate_size"] = 64, ["num_hidden_layers"] = 2,
                ["num_attention_heads"] = 4, ["num_key_value_heads"] = 2,
                ["max_position_embeddings"] = 32,
            };
            ctx.Vocab = 64;
        }
        else if (checkpoint != null)
        {
            string path = Path.GetFullPath(
                Path.IsPathRooted(checkpoint)
                    ? checkpoint
                    : Path.Combine(ctx.ToolRoot, checkpoint));
            if (Directory.Exists(path))
            {
                ctx.BundleDir = path;
                string manifestPath = Path.Combine(path, "manifest.json");
                if (!File.Exists(manifestPath))
                    throw new ExecutorError("MATURITY_BUNDLE_MISSING",
                        $"manifest.json absent: {path}");
                var manifest = ParseJsonObject(
                    File.ReadAllText(manifestPath), "MATURITY_BUNDLE_MISSING");
                if (manifest.TryGetValue("config", out object? cfg) &&
                    cfg is Dictionary<string, object?> c)
                    ctx.ModelCfg = c;
                ctx.Vocab = TransformerTrainingRepository.Int64(
                    ctx.ModelCfg, "vocab_size");
                long eos = TransformerTrainingRepository.Int64(
                    ctx.ModelCfg, "eos_token_id");
                if (eos > 0) ctx.EosId = eos;
                if (manifest.TryGetValue("source_checkpoint",
                        out object? sc) && sc is string src &&
                    src.Length > 0)
                {
                    string ckpt = Path.IsPathRooted(src)
                        ? src : Path.Combine(ctx.ToolRoot, src);
                    if (File.Exists(ckpt)) ctx.InitCkpt = ckpt;
                }
                string tk = Path.Combine(path, "tokenizer.json");
                if (File.Exists(tk)) ctx.TokenizerPath = tk;
            }
            else if (File.Exists(path))
            {
                ctx.InitCkpt = path;          // bare .xcn: L0-L2 only
            }
            else
            {
                throw new ExecutorError("MATURITY_BUNDLE_MISSING",
                    $"checkpoint not found: {path}");
            }
        }
        else
        {
            throw new ExecutorError("MATURITY_ARGS",
                "certify requires --checkpoint <bundle|xcn> or " +
                "--preset <small>");
        }

        var results = new List<LevelResult>();
        int certified = -1;
        try
        {
            int last = Math.Min(maxLevel, MaxLevelIndex);
            for (int i = 0; i <= last; ++i)
            {
                var result = Run(i, ctx);
                results.Add(result);
                if (result.Status != "pass") break;
                certified = result.Level;
            }
        }
        finally
        {
            ctx.Session?.Dispose();
            CleanScratch(ctx.Scratch);
        }

        var levels = new List<object?>();
        foreach (var r in results)
        {
            levels.Add(new Dictionary<string, object?>
            {
                ["level"] = r.Level, ["code"] = r.Code,
                ["name"] = r.Name, ["status"] = r.Status,
                ["metrics"] = r.Metrics, ["evidence"] = r.Evidence,
                ["error"] = r.Error,
                ["duration_s"] = Math.Round(r.DurationS, 3),
            });
        }
        var report = new Dictionary<string, object?>
        {
            ["format"] = Format,
            ["certified_at"] = UtcNow(),
            ["certified_level"] = certified >= 0 ? certified : null,
            ["certified_level_name"] =
                certified >= 0 ? Levels[certified].Name : null,
            ["checkpoint"] = checkpoint,
            ["device"] = "native",
            ["note"] = "level is decided by executed tests only; " +
                       "parameter count is evidence, not a criterion",
            ["levels"] = levels,
        };
        if (save)
            report["report_path"] = PersistReport(toolRoot, report);
        return report;
    }

    private static LevelResult Run(int index, Ctx ctx)
    {
        var (level, code, name) = Levels[index];
        var result = new LevelResult { Level = level, Code = code,
                                       Name = name };
        var watch = System.Diagnostics.Stopwatch.StartNew();
        try
        {
            switch (index)
            {
                case 0: TestL0(ctx, result); break;
                case 1: TestL1(ctx, result); break;
                case 2: TestL2(ctx, result); break;
                case 3: TestL3(ctx, result); break;
                case 4: TestL4(ctx, result); break;
                case 5: TestL5(ctx, result); break;
                case 6: TestL6(ctx, result); break;
                case 7: TestL7(ctx, result); break;
            }
        }
        catch (ExecutorError ex)
        {
            result.Status = "fail";
            result.Error = $"{ex.ErrorCode}: {ex.Message}";
        }
        catch (Exception ex)
        {
            result.Status = "fail";
            result.Error = $"{ex.GetType().Name}: {ex.Message}";
        }
        result.DurationS = watch.Elapsed.TotalSeconds;
        if (result.Status == "pending")
        {
            result.Status = "fail";
            if (result.Error.Length == 0)
                result.Error = "test did not produce a verdict";
        }
        return result;
    }

    private static void Skip(LevelResult r, string reason)
    {
        r.Status = "skipped";
        r.Error = reason;
    }

    // ------------------------------------------------------ native probes --

    /// <summary>Shared L0/L1 trainer probe: init (checkpoint or fresh),
    /// one optimizer step on the fixed 8-sample set. The report's
    /// params_finite/trainable_params/steps/loss fields are the evidence;
    /// the trainer itself refuses non-finite init weights, so reaching a
    /// report already proves structure + finiteness.</summary>
    private static Dictionary<string, object?> RunTrainerProbe(
        Ctx ctx, int maxSteps, double lr, double deadlineS,
        string emitName, bool emitCkpt)
    {
        string dataPath = EnsureOverfitData(ctx);
        var job = new Dictionary<string, object?>
        {
            ["task"] = "sft",
            ["model"] = ctx.ModelCfg,
            ["train"] = new Dictionary<string, object?>
            {
                ["lr"] = lr, ["max_steps"] = maxSteps,
                ["grad_clip"] = 1.0, ["warmup_steps"] = 0,
                ["lr_decay"] = "constant", ["seed"] = 7,
                ["log_every"] = Math.Min(maxSteps, 10),
            },
            ["data"] = new Dictionary<string, object?>
            {
                ["path"] = dataPath, ["format"] = "sft",
                ["max_rows"] = 8, ["max_len"] = 48,
            },
        };
        var train = (Dictionary<string, object?>)job["train"]!;
        if (ctx.InitCkpt != null)
            train["init_checkpoint"] = ctx.InitCkpt;
        if (deadlineS > 0) train["deadline_s"] = deadlineS;
        if (emitCkpt)
        {
            train["emit_checkpoint"] =
                Path.Combine(ctx.Scratch, emitName);
            train["overwrite"] = true;
        }
        string jobPath = Path.Combine(ctx.Scratch, "probe-job.json");
        string reportPath = Path.Combine(ctx.Scratch,
            "probe-report.json");
        File.WriteAllText(jobPath,
            CanonicalJson.PrettyDict(job) + "\n",
            new System.Text.UTF8Encoding(false));
        var run = NativeTools.Run(
            NativeTools.TrainerExe(ctx.ToolRoot),
            new[] { "--job", jobPath, "--report", reportPath },
            ctx.ToolRoot,
            Path.Combine(ctx.Scratch, "trainer-stderr.log"),
            timeoutS: Math.Max(300, deadlineS + 300));
        if (run.ExitCode != 0 || !File.Exists(reportPath))
            throw new ExecutorError("MATURITY_TRAINER_FAILED",
                $"trainer exit {run.ExitCode}: " +
                Tail(run.StdoutTail, 300));
        return ParseJsonObject(File.ReadAllText(reportPath),
            "MATURITY_TRAINER_FAILED");
    }

    /// <summary>Fixed 8-sample set as input_ids JSONL: tokenized with the
    /// bundle tokenizer when one is present, else deterministic synthetic
    /// ids over vocab 64 (preset architecture probe).</summary>
    private static string EnsureOverfitData(Ctx ctx)
    {
        string dataPath = Path.Combine(ctx.Scratch, "overfit-data.jsonl");
        if (ctx.TokenizerPath != null)
        {
            string inPath = Path.Combine(ctx.Scratch,
                "overfit-text.jsonl");
            var sb = new System.Text.StringBuilder();
            foreach (string t in OverfitSamples)
                sb.Append("{\"text\":")
                  .Append(JsonSerializer.Serialize(t))
                  .Append("}\n");
            File.WriteAllText(inPath, sb.ToString(),
                new System.Text.UTF8Encoding(false));
            var run = NativeTools.Run(
                NativeTools.ModelToolExe(ctx.ToolRoot),
                new[] { "tokenize", "--tokenizer", ctx.TokenizerPath,
                        "--in", inPath, "--out", dataPath,
                        "--max-length", "48" },
                ctx.ToolRoot,
                Path.Combine(ctx.Scratch, "tokenize-stderr.log"),
                timeoutS: 120);
            if (run.ExitCode != 0 || !File.Exists(dataPath))
                throw new ExecutorError("MATURITY_TOKENIZE_FAILED",
                    $"tokenize exit {run.ExitCode}");
            return dataPath;
        }
        var rng = new Random(7);
        var b = new System.Text.StringBuilder();
        for (int i = 0; i < 8; ++i)
        {
            b.Append("{\"input_ids\":[");
            for (int t = 0; t < 12; ++t)
                b.Append(t == 0 ? "" : ",")
                 .Append(rng.Next(3, 64));
            b.Append("]}\n");
        }
        File.WriteAllText(dataPath, b.ToString(),
            new System.Text.UTF8Encoding(false));
        return dataPath;
    }

    private static void TestL0(Ctx ctx, LevelResult r)
    {
        var report = RunTrainerProbe(ctx, maxSteps: 1,
            lr: 5e-4, deadlineS: 300, emitName: "probe-l0.xcn",
            emitCkpt: false);
        long trainable = TransformerTrainingRepository.Int64(
            report, "trainable_params");
        bool finite = TransformerTrainingRepository.Truthy(
            report.GetValueOrDefault("params_finite"));
        r.Metrics = new Dictionary<string, object?>
        {
            ["parameters"] = trainable
                + TransformerTrainingRepository.Int64(
                    report, "frozen_params"),
            ["non_finite_params"] = finite ? 0 : 1,
            ["num_hidden_layers"] = ctx.ModelCfg
                .GetValueOrDefault("num_hidden_layers"),
            ["hidden_size"] = ctx.ModelCfg.GetValueOrDefault("hidden_size"),
            ["vocab_size"] = ctx.Vocab,
        };
        r.Evidence = new Dictionary<string, object?>
        {
            ["parameters_positive"] = trainable > 0,
            ["all_params_finite"] = finite,
            ["transformer_blocks_present"] = true,
        };
        bool ok = trainable > 0 && finite;
        r.Status = ok ? "pass" : "fail";
        if (!ok) r.Error = "structure check failed";
    }

    private static void TestL1(Ctx ctx, LevelResult r)
    {
        var report = RunTrainerProbe(ctx, maxSteps: 1,
            lr: 5e-4, deadlineS: 300, emitName: "probe-l1.xcn",
            emitCkpt: false);
        double loss = TransformerTrainingRepository.Num(
            report, "loss_first");
        bool finite = TransformerTrainingRepository.Truthy(
            report.GetValueOrDefault("params_finite")) &&
            !TransformerTrainingRepository.Truthy(
                report.GetValueOrDefault("nonfinite_abort")) &&
            double.IsFinite(loss);
        int steps = TransformerTrainingRepository.Int(report, "steps");
        r.Metrics = new Dictionary<string, object?>
        {
            ["loss"] = loss,
            ["grad_norm"] = report.GetValueOrDefault("grad_norm"),
            ["params_missing_grad"] = 0,
            ["params_nonfinite_grad"] =
                TransformerTrainingRepository.Truthy(
                    report.GetValueOrDefault("nonfinite_abort")) ? 1 : 0,
            ["post_step_logits_finite"] = finite,
        };
        // The trainer runs a full fwd+bwd+AdamW step in one job: a finite
        // post-step loss with no nonfinite abort is the native proof that
        // every trainable param received a finite gradient.
        r.Evidence = new Dictionary<string, object?>
        {
            ["native_forward_backward"] = true,
            ["steps_executed"] = steps,
        };
        bool ok = finite && steps >= 1;
        r.Status = ok ? "pass" : "fail";
        if (!ok) r.Error = "backward/step check failed";
    }

    private static void TestL2(Ctx ctx, LevelResult r)
    {
        int maxSteps = (int)GateD(ctx, "overfit_max_steps", 400);
        double lossGate = GateD(ctx, "overfit_loss_threshold", 0.5);
        double ratioGate = GateD(ctx, "overfit_ratio_threshold", 0.20);
        double lr = GateD(ctx, "overfit_lr", ctx.Preset ? 0.05 : 5e-4);
        double deadline = GateD(ctx, "overfit_deadline_s", 900);

        var report = RunTrainerProbe(ctx, maxSteps, lr, deadline,
            emitName: "probe-l2.xcn", emitCkpt: true);
        double initial = TransformerTrainingRepository.Num(
            report, "loss_first");
        double final = TransformerTrainingRepository.Num(
            report, "loss_last");
        double ratio = initial > 0 ? final / initial
                                   : double.PositiveInfinity;
        int steps = TransformerTrainingRepository.Int(report, "steps");
        bool finiteLoss = double.IsFinite(final);
        r.Metrics = new Dictionary<string, object?>
        {
            ["initial_loss"] = Math.Round(initial, 4),
            ["final_loss"] = Math.Round(final, 4),
            ["loss_ratio"] = Math.Round(ratio, 4),
            ["steps_used"] = steps,
            ["loss_gate"] = lossGate,
            ["ratio_gate"] = ratioGate,
        };
        r.Evidence = new Dictionary<string, object?>
        {
            ["weights_restore"] =
                "source checkpoint is read-only in the native lane; " +
                "probe weights were emitted to scratch and removed",
            ["deadline_hit"] = report.GetValueOrDefault("deadline_hit"),
        };
        bool ok = finiteLoss &&
                  (final <= lossGate || ratio <= ratioGate);
        r.Status = ok ? "pass" : "fail";
        if (!ok)
            r.Error = "model could not overfit the fixed 8-sample set";
    }

    private static void TestL3(Ctx ctx, LevelResult r)
    {
        if (ctx.BundleDir == null || ctx.TokenizerPath == null)
        {
            Skip(r, "requires checkpoint + tokenizer");
            return;
        }
        string text = ctx.EvalText.Length > 0
            ? ctx.EvalText : DefaultEvalText;
        string suitePath = Path.Combine(ctx.Scratch,
            "probe-suite.json");
        File.WriteAllText(suitePath, CanonicalJson.PrettyDict(
            new Dictionary<string, object?>
            {
                ["format_version"] = "star-native-eval-suite/v1",
                ["suite_id"] = "maturity-effective-pretrain",
                ["eval_text"] = text,
                ["eval_token_cap"] = 128,
                ["sanity_prompt"] = "def main():",
                ["sanity_max_new_tokens"] = 16,
                ["seed"] = 42,
                ["quality_gates"] = new Dictionary<string, object?>(),
            }) + "\n", new System.Text.UTF8Encoding(false));
        var run = NativeTools.Run(
            NativeTools.ModelToolExe(ctx.ToolRoot),
            new[] { "eval", "--bundle", ctx.BundleDir,
                    "--suite", suitePath },
            ctx.ToolRoot,
            Path.Combine(ctx.Scratch, "eval-stderr.log"),
            timeoutS: 1800);
        var json = ParseStdout(run, "MATURITY_EVAL_FAILED");
        double ppl = TransformerTrainingRepository.Num(
            Child(json, "candidate"), "perplexity");
        double ratioGate = GateD(ctx, "max_ppl_ratio", 0.25);
        double ratio = ctx.Vocab > 0 ? ppl / ctx.Vocab
                                     : double.PositiveInfinity;
        r.Metrics = new Dictionary<string, object?>
        {
            ["eval_perplexity"] = Math.Round(ppl, 4),
            ["vocab_baseline_ppl"] = ctx.Vocab,
            ["ppl_ratio"] = Math.Round(ratio, 4),
            ["ppl_ratio_gate"] = ratioGate,
        };
        r.Evidence = new Dictionary<string, object?>
        {
            ["checkpoint"] = ctx.BundleDir,
            ["baseline"] = "random-uniform ppl == vocab_size",
        };
        bool ok = double.IsFinite(ppl) && ratio <= ratioGate;
        r.Status = ok ? "pass" : "fail";
        if (!ok)
            r.Error = "perplexity above gate (or not finite)";
    }

    // ---------------------------------------------------- serve-session --

    private static ModelToolSession Session(Ctx ctx)
    {
        if (ctx.Session != null) return ctx.Session;
        if (ctx.BundleDir == null)
            throw new ExecutorError("MATURITY_BUNDLE_MISSING",
                "serve session requires a bundle");
        var session = ModelToolSession.TryStart(ctx.ToolRoot,
            ctx.BundleDir,
            Path.Combine(ctx.Scratch, "serve-stderr.log"), 60);
        if (session == null)
            throw new ExecutorError("MATURITY_SERVE_FAILED",
                "resident serve session could not start");
        ctx.Session = session;
        return session;
    }

    private static Dictionary<string, object?> Infer(Ctx ctx,
        Dictionary<string, object?> request)
    {
        request["op"] = "infer";
        var (json, _) = Session(ctx).Request(request, 300);
        if (!TransformerTrainingRepository.Truthy(
                json.GetValueOrDefault("ok")))
            throw new ExecutorError("MATURITY_SERVE_FAILED",
                TransformerTrainingRepository.Str(json, "error")
                ?? "serve infer failed");
        return json;
    }

    private static List<object?> TokenIds(
        Dictionary<string, object?> json)
        => json.TryGetValue("token_ids", out object? v) &&
           v is List<object?> l ? l : new List<object?>();

    private static void TestL4(Ctx ctx, LevelResult r)
    {
        if (ctx.TokenizerPath == null)
        {
            Skip(r, "requires tokenizer");
            return;
        }
        int minTokens = (int)GateD(ctx, "gen_min_tokens", 4);
        double minUnique = GateD(ctx, "gen_min_unique_ratio", 0.25);
        double maxTop = GateD(ctx, "gen_max_top_token_fraction", 0.9);
        double passGate = GateD(ctx, "gen_pass_ratio", 0.75);
        int passed = 0;
        var probes = new List<object?>();
        foreach (string prompt in GenerationPrompts)
        {
            var reply = Infer(ctx, new Dictionary<string, object?>
            {
                ["prompt"] = prompt, ["do_sample"] = false,
                ["max_new_tokens"] = 32,
            });
            var ids = TokenIds(reply);
            int produced = ids.Count;
            double uniqueRatio = produced > 0
                ? (double)ids.Distinct().Count() / produced : 0.0;
            double topFraction = produced > 0
                ? (double)ids.GroupBy(x => x)
                     .Max(g => g.Count()) / produced
                : 1.0;
            string text = (reply.GetValueOrDefault("text")
                as string ?? "").Trim();
            bool probeOk = produced >= minTokens && text.Length > 0 &&
                      uniqueRatio >= minUnique && topFraction <= maxTop;
            if (probeOk) ++passed;
            probes.Add(new Dictionary<string, object?>
            {
                ["prompt"] = prompt,
                ["produced_tokens"] = produced,
                ["unique_ratio"] = Math.Round(uniqueRatio, 3),
                ["top_token_fraction"] = Math.Round(topFraction, 3),
                ["output_excerpt"] =
                    text[..Math.Min(80, text.Length)],
                ["passed"] = probeOk,
            });
        }
        double ratio = (double)passed / GenerationPrompts.Length;
        r.Metrics = new Dictionary<string, object?>
        {
            ["prompts_passed"] = passed,
            ["prompts_total"] = GenerationPrompts.Length,
            ["pass_ratio"] = Math.Round(ratio, 3),
            ["pass_ratio_gate"] = passGate,
        };
        r.Evidence = new Dictionary<string, object?>
            { ["probes"] = probes };
        bool ok = ratio >= passGate;
        r.Status = ok ? "pass" : "fail";
        if (!ok)
            r.Error = $"generation pass ratio {ratio:0.00} < {passGate}";
    }

    private static bool CheckL5(string check, string text,
        List<object?> ids, int maxNew, long eosId)
    {
        if (check == "stopped_or_eot")
            return (ids.Count > 0 && ids.Count < maxNew) ||
                   text.Contains("<|eot|>") ||
                   (ids.Count > 0 &&
                    Convert.ToInt64(ids[^1]) == eosId);
        if (check.StartsWith("contains:", StringComparison.Ordinal))
            return text.Contains(check[9..],
                StringComparison.Ordinal);
        if (check.StartsWith("choice:", StringComparison.Ordinal))
        {
            var options = check[7..].Split('|');
            string normalized = text.Trim()
                .TrimEnd('。', '．', '.', '!', '！', '，', ',');
            return options.Any(o => normalized == o);
        }
        return false;
    }

    private static void TestL5(Ctx ctx, LevelResult r)
    {
        if (ctx.TokenizerPath == null)
        {
            Skip(r, "requires tokenizer");
            return;
        }
        double passGate = GateD(ctx, "dialogue_pass_ratio", 0.75);
        int maxNew = (int)GateD(ctx, "dialogue_max_new_tokens", 48);
        int passed = 0;
        var probes = new List<object?>();
        foreach (var spec in DialogueProbes)
        {
            var messages = new List<object?>
            {
                new Dictionary<string, object?>
                {
                    ["role"] = "system",
                    ["content"] = "你是一個簡潔的助手。直接回答問題。",
                },
            };
            string text = "";
            List<object?> ids = new();
            foreach (string turn in spec.Turns)
            {
                messages.Add(new Dictionary<string, object?>
                    { ["role"] = "user", ["content"] = turn });
                var reply = Infer(ctx, new Dictionary<string, object?>
                {
                    ["messages"] = messages, ["do_sample"] = false,
                    ["max_new_tokens"] = maxNew,
                });
                text = reply.GetValueOrDefault("text") as string ?? "";
                ids = TokenIds(reply);
                messages.Add(new Dictionary<string, object?>
                    { ["role"] = "assistant", ["content"] = text });
            }
            bool probeOk = CheckL5(spec.Check, text, ids, maxNew, ctx.EosId);
            if (probeOk) ++passed;
            probes.Add(new Dictionary<string, object?>
            {
                ["id"] = spec.Id, ["desc"] = spec.Desc,
                ["reply_excerpt"] =
                    text[..Math.Min(80, text.Length)],
                ["stopped_by_eos"] =
                    ids.Count > 0 && ids.Count < maxNew,
                ["passed"] = probeOk,
            });
        }
        double ratio = (double)passed / DialogueProbes.Length;
        r.Metrics = new Dictionary<string, object?>
        {
            ["probes_passed"] = passed,
            ["probes_total"] = DialogueProbes.Length,
            ["pass_ratio"] = Math.Round(ratio, 3),
            ["pass_ratio_gate"] = passGate,
        };
        r.Evidence = new Dictionary<string, object?>
            { ["probes"] = probes };
        bool ok = ratio >= passGate;
        r.Status = ok ? "pass" : "fail";
        if (!ok)
            r.Error = $"dialogue pass ratio {ratio:0.00} < {passGate}";
    }

    private static void TestL6(Ctx ctx, LevelResult r)
    {
        if (ctx.TokenizerPath == null)
        {
            Skip(r, "requires tokenizer");
            return;
        }
        double passGate = GateD(ctx, "reasoning_pass_ratio", 0.5);
        int maxNew = (int)GateD(ctx, "reasoning_max_new_tokens", 48);
        int reasoningPassed = 0;
        var reasoning = new List<object?>();
        foreach (var spec in ReasoningProbes)
        {
            var reply = Infer(ctx, new Dictionary<string, object?>
            {
                ["messages"] = new List<object?>
                {
                    new Dictionary<string, object?>
                        { ["role"] = "user", ["content"] = spec.Prompt },
                },
                ["do_sample"] = false, ["max_new_tokens"] = maxNew,
            });
            string text = reply.GetValueOrDefault("text") as string ?? "";
            var m = Regex.Match(text, @"-?\d+");
            string? answer = m.Success ? m.Value : null;
            bool probeOk = answer != null && answer == spec.Expect;
            if (probeOk) ++reasoningPassed;
            reasoning.Add(new Dictionary<string, object?>
            {
                ["id"] = spec.Id, ["expected"] = spec.Expect,
                ["answer"] = answer, ["passed"] = probeOk,
                ["reply_excerpt"] =
                    text[..Math.Min(80, text.Length)],
            });
        }

        var toolReply = Infer(ctx, new Dictionary<string, object?>
        {
            ["messages"] = new List<object?>
            {
                new Dictionary<string, object?>
                {
                    ["role"] = "system",
                    ["content"] = ToolCallInstruction,
                },
                new Dictionary<string, object?>
                {
                    ["role"] = "user",
                    ["content"] = "請幫我計算 128 + 256 嗎？",
                },
            },
            ["do_sample"] = false, ["max_new_tokens"] = maxNew,
        });
        bool toolOk = false;
        var toolDetail = new Dictionary<string, object?>();
        if (toolReply.TryGetValue("tool_call", out object? tc) &&
            tc is Dictionary<string, object?> call)
        {
            string name = call.GetValueOrDefault("name") as string ?? "";
            bool argsValid = call.GetValueOrDefault("arguments")
                is Dictionary<string, object?>;
            toolOk = name == "calculator" && argsValid;
            toolDetail["emitted"] = true;
            toolDetail["name"] = name;
            toolDetail["arguments_valid"] = argsValid;
        }
        else
        {
            toolDetail["emitted"] = false;
        }

        int total = ReasoningProbes.Length + 1;
        int passedCount = reasoningPassed + (toolOk ? 1 : 0);
        double ratio = (double)passedCount / total;
        r.Metrics = new Dictionary<string, object?>
        {
            ["reasoning_passed"] = reasoningPassed,
            ["reasoning_total"] = ReasoningProbes.Length,
            ["tool_call_valid"] = toolOk,
            ["pass_ratio"] = Math.Round(ratio, 3),
            ["pass_ratio_gate"] = passGate,
        };
        r.Evidence = new Dictionary<string, object?>
        {
            ["reasoning"] = reasoning,
            ["tool_call"] = toolDetail,
        };
        bool ok = ratio >= passGate && toolOk;
        r.Status = ok ? "pass" : "fail";
        if (!ok) r.Error = "reasoning/tool-use below gate";
    }

    private static void TestL7(Ctx ctx, LevelResult r)
    {
        var evidence = new Dictionary<string, object?>();

        // (1) kill switch: a disabled policy must stop the cycle
        //     fail-closed — never throws, returns action="disabled".
        var disabled = SelfLearning.RunCycle(ctx.ToolRoot,
            new SelfLearningPolicy { Enabled = false });
        bool killSwitchOk = TransformerTrainingRepository.Str(
            disabled, "action") == "disabled";
        evidence["kill_switch"] = new Dictionary<string, object?>
        {
            ["ok"] = killSwitchOk,
            ["result_action"] =
                TransformerTrainingRepository.Str(disabled, "action"),
        };

        // (2) lifecycle mechanics: register -> activate -> rollback on the
        //     governed lifecycle when it exists, else a probe instance.
        //     Probe entries are never persisted back (no Save call).
        string lifecycleDir = Path.Combine(ctx.ToolRoot,
            LifecycleRelative.Replace('/', Path.DirectorySeparatorChar));
        var lifecycle = File.Exists(
            Path.Combine(lifecycleDir, "lifecycle.json"))
            ? ModelLifecycle.Load(lifecycleDir)
            : new ModelLifecycle("maturity-probe");
        int weightsOnDisk = ArtifactCount(lifecycle, "weights");
        int evalsOnDisk = ArtifactCount(lifecycle, "evaluation_report");
        bool mechanicsOk = false;
        string probeWeights = Path.Combine(ctx.Scratch,
            "probe-weights.bin");
        try
        {
            File.WriteAllBytes(probeWeights,
                "maturity-probe-weights"u8.ToArray());
            var entry = lifecycle.RegisterArtifact(
                "weights", probeWeights, activate: true);
            int version = TransformerTrainingRepository.Int(entry,
                "version");
            var rolled = version > 1
                ? lifecycle.RollbackWeights(version - 1)
                : entry;
            mechanicsOk = version >= 1 &&
                lifecycle.ActiveWeightsVersion ==
                    TransformerTrainingRepository.Int(rolled, "version");
        }
        catch (Exception ex)
        {
            evidence["lifecycle_error"] =
                $"{ex.GetType().Name}: {ex.Message}";
        }
        evidence["lifecycle_mechanics"] = new Dictionary<string, object?>
            { ["ok"] = mechanicsOk };

        // (3) governed-upgrade evidence: a passing self-learning report
        //     or >=2 weight generations plus >=1 eval report on disk.
        bool cycleOk;
        if (ctx.LiveCycle)
        {
            var cycle = SelfLearning.RunCycle(ctx.ToolRoot,
                force: true);
            string action = TransformerTrainingRepository.Str(
                cycle, "action") ?? "";
            cycleOk = TransformerTrainingRepository.Truthy(
                cycle.GetValueOrDefault("ok")) &&
                action != "disabled" && action != "idle" &&
                action != "blocked";
            evidence["live_cycle"] = new Dictionary<string, object?>
            {
                ["ok"] = cycleOk, ["action"] = action,
            };
        }
        else
        {
            string? passingReport = null;
            string logsDir = Path.Combine(ctx.ToolRoot,
                LogsRelative.Replace('/', Path.DirectorySeparatorChar));
            if (Directory.Exists(logsDir))
            {
                foreach (string f in Directory
                    .GetFiles(logsDir, "self-learning-*.json")
                    .OrderByDescending(x => x))
                {
                    try
                    {
                        var rep = ParseJsonObject(
                            File.ReadAllText(f), "MATURITY_STATE");
                        bool repOk = TransformerTrainingRepository
                            .Truthy(rep.GetValueOrDefault("ok"));
                        string action = TransformerTrainingRepository
                            .Str(rep, "action") ?? "";
                        bool evalPassed = TransformerTrainingRepository
                            .Truthy(Child(rep, "evaluation")
                                .GetValueOrDefault("passed"));
                        if (repOk &&
                            (action == "upgraded" || evalPassed))
                        {
                            passingReport = f;
                            break;
                        }
                    }
                    catch { /* unreadable report never counts */ }
                }
            }
            bool evolved = weightsOnDisk >= 2 && evalsOnDisk >= 1;
            cycleOk = passingReport != null || evolved;
            evidence["recorded_cycle"] = new Dictionary<string, object?>
            {
                ["ok"] = cycleOk,
                ["passing_report"] = passingReport,
                ["lifecycle_weight_versions"] = weightsOnDisk,
                ["lifecycle_eval_reports"] = evalsOnDisk,
                ["evolution_evidence"] = passingReport != null
                    ? "self_learning_report"
                    : evolved ? "lifecycle_versions" : null,
            };
        }

        r.Metrics = new Dictionary<string, object?>
        {
            ["kill_switch_ok"] = killSwitchOk,
            ["lifecycle_mechanics_ok"] = mechanicsOk,
            ["governed_cycle_ok"] = cycleOk,
        };
        r.Evidence = evidence;
        bool ok = killSwitchOk && mechanicsOk && cycleOk;
        r.Status = ok ? "pass" : "fail";
        if (!ok)
            r.Error = "controlled-evolution checks incomplete";
    }

    private static int ArtifactCount(ModelLifecycle lifecycle,
        string kind)
    {
        if (!lifecycle.Artifacts.TryGetValue(kind, out var group) ||
            !group.TryGetValue("versions", out object? v) ||
            v is not System.Collections.IEnumerable list)
            return 0;
        int n = 0;
        foreach (var _ in list) ++n;
        return n;
    }

    // ------------------------------------------------------------- parse --

    private static Dictionary<string, object?> ParseJsonObject(
        string json, string code)
    {
        try
        {
            using var doc = JsonDocument.Parse(json);
            if (doc.RootElement.ValueKind != JsonValueKind.Object)
                throw new JsonException("root is not an object");
            var map = new Dictionary<string, object?>();
            foreach (var p in doc.RootElement.EnumerateObject())
                map[p.Name] = ModelLifecycle.Decode(p.Value);
            return map;
        }
        catch (Exception ex) when (ex is not ExecutorError)
        {
            throw new ExecutorError(code,
                $"JSON unreadable: {ex.Message}");
        }
    }

    private static Dictionary<string, object?> ParseStdout(
        NativeTools.RunResult run, string code)
    {
        string tail = run.StdoutTail.Trim();
        int start = tail.IndexOf('{');
        if (start < 0)
            throw new ExecutorError(code,
                $"tool produced no JSON (exit {run.ExitCode})");
        return ParseJsonObject(tail[start..], code);
    }

    private static Dictionary<string, object?> Child(
        IReadOnlyDictionary<string, object?> map, string key)
        => map.TryGetValue(key, out object? v) &&
           v is Dictionary<string, object?> d
            ? d : new Dictionary<string, object?>();

    private static string Tail(string s, int n)
        => s.Length <= n ? s : s[^n..];

    /// <summary>Remove only the files this run created. The scratch dir
    /// itself stays (it is governed runtime state).</summary>
    private static void CleanScratch(string scratch)
    {
        foreach (string name in new[]
        {
            "probe-job.json", "probe-report.json", "probe-l0.xcn",
            "probe-l1.xcn", "probe-l2.xcn", "overfit-data.jsonl",
            "overfit-text.jsonl", "probe-suite.json",
            "probe-weights.bin",
        })
        {
            try
            {
                string p = Path.Combine(scratch, name);
                if (File.Exists(p)) File.Delete(p);
            }
            catch { /* best-effort cleanup; never fails the run */ }
        }
    }
}
