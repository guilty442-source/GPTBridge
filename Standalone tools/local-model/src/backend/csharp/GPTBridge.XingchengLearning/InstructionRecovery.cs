// InstructionRecovery.cs — single-capability recovery lane
// (star-single-capability-recovery/v1).
//
// Trains exactly one capability — instruction_following — under
// capability_training_mode=SINGLE_CAPABILITY_RECOVERY. Everything else
// stays frozen: architecture (xc-fused-1), checkpoint format (XCN1),
// tokenizer, and every other capability. The lane is:
//
//   dataset build (A-F instruction categories, zh-TW dominant, every
//      row machine-verified by a rule spec, dedup, 18% held-out)
//   -> pretokenize (xc_modeltool tokenize --chat)
//   -> baseline + source RAW-layer eval (xc_modeltool capability)
//   -> staged native SFT (xingcheng_trainer, 50-step stages)
//   -> mini-eval each stage, regression + router health every 100 steps
//   -> early stop on parity / 3x no-improvement / regression / collapse
//   -> best candidate -> export-bundle (XCN validation) -> smoke gates
//      -> provenance -> star-single-capability-recovery/v1 report
//
// No active-weight mutation ever happens here; the report hands a
// staged candidate to the normal lifecycle for later activation.

using System.Text.Json;
using System.Text.RegularExpressions;

namespace GPTBridge.XingchengLearning;

internal static class InstructionRecovery
{
    public const string ReportFormat = "star-single-capability-recovery/v1";
    public const string SuiteFormat = "star-capability-suite/v1";

    // Active recovery capability — set from the plan at Run() start (or
    // via --capability on --recovery-dataset-build); the freeze guard
    // still requires it to equal policy.ActiveCapability.
    public static string Capability = "instruction_following";

    public static string EvalFormat => Capability switch
    {
        "context_tracking" => "star-context-eval-result/v1",
        "multi_turn" => "star-multiturn-eval-result/v1",
        "structured_output" => "star-structured-eval-result/v1",
        "tool_calling" => "star-toolcall-eval-result/v1",
        "reading_grounding" => "star-reading-eval-result/v1",
        "rag" => "star-rag-eval-result/v1",
        "math" => "star-math-eval-result/v1",
        "coding" => "star-coding-eval-result/v1",
        "native_thinking" => "star-thinking-eval-result/v1",
        _ => "star-instruction-eval-result/v1",
    };
    public static string DatasetFormat => Capability switch
    {
        "context_tracking" => "star-context-recovery-dataset/v1",
        "multi_turn" => "star-multiturn-recovery-dataset/v1",
        "structured_output" =>
            "star-structured-recovery-dataset/v1",
        "tool_calling" => "star-toolcall-recovery-dataset/v1",
        "reading_grounding" => "star-reading-recovery-dataset/v1",
        "rag" => "star-rag-recovery-dataset/v1",
        "math" => "star-math-recovery-dataset/v1",
        "coding" => "star-coding-recovery-dataset/v1",
        _ => "star-instruction-recovery-dataset/v1",
    };
    private static string SuiteId => Capability switch
    {
        "context_tracking" => "star-context-recovery-eval-20261001",
        "multi_turn" => "star-multiturn-recovery-eval-20261001",
        "structured_output" =>
            "star-structured-recovery-eval-20261001",
        "tool_calling" => "star-toolcall-recovery-eval-20261001",
        "reading_grounding" =>
            "star-reading-recovery-eval-20261001",
        "rag" => "star-rag-recovery-eval-20261001",
        "math" => "star-math-recovery-eval-20261001",
        "coding" => "star-coding-recovery-eval-20261001",
        _ => "star-instruction-recovery-eval-20261001",
    };

    private static readonly string[] SupportedCapabilities =
        { "instruction_following", "context_tracking", "multi_turn",
          "structured_output", "tool_calling", "reading_grounding",
          "rag", "math", "coding" };

    // §20 sub-metrics -> score weights, per capability.
    private static readonly (string metric, double w)[]
        InstructionMetricWeights =
    {
        ("instruction_completion", 0.25),
        ("format_accuracy", 0.20),
        ("constraint_following", 0.20),
        ("negative_constraint", 0.15),
        ("language_accuracy", 0.10),
        ("extra_content", 0.10),
    };
    private static readonly (string metric, double w)[]
        ContextMetricWeights =
    {
        ("recall_accuracy", 0.30),
        ("entity_binding", 0.25),
        ("update_tracking", 0.15),
        ("count_tracking", 0.10),
        ("order_tracking", 0.10),
        ("distractor_rejection", 0.10),
    };
    private static readonly (string metric, double w)[]
        MultiTurnMetricWeights =
    {
        ("followup_reference", 0.30),
        ("correction_acceptance", 0.20),
        ("elaboration_control", 0.15),
        ("topic_shift_return", 0.15),
        ("role_consistency", 0.10),
        ("multi_step_state", 0.10),
    };
    // §9 maturation spec order: validity and schema dominate; enum /
    // nested / array conformance are real but secondary surfaces.
    private static readonly (string metric, double w)[]
        StructuredMetricWeights =
    {
        ("json_valid", 0.25),
        ("schema_conformant", 0.20),
        ("typed_fields", 0.20),
        ("enum_membership", 0.15),
        ("nested_objects", 0.10),
        ("arrays", 0.10),
    };
    // Registered metric names from Maturation300M. Selection and
    // argument correctness dominate — calling the right tool with the
    // right payload is the capability; necessity/interpretation/
    // failure are the honesty surfaces (a spurious call or a
    // fabricated result is a real failure, not noise).
    private static readonly (string metric, double w)[]
        ToolMetricWeights =
    {
        ("tool_selection", 0.30),
        ("argument_correctness", 0.25),
        ("tool_necessity", 0.20),
        ("result_interpretation", 0.15),
        ("failure_recovery", 0.10),
    };
    // Registered names from Maturation300M (9 metrics). Comprehension
    // and honesty surfaces carry equal weight — a grounded reader that
    // fabricates or leaks memory answers is failing the capability.
    private static readonly (string metric, double w)[]
        ReadingMetricWeights =
    {
        ("document_qa", 0.15),
        ("multi_passage", 0.15),
        ("conflicting_evidence", 0.10),
        ("insufficient_evidence", 0.10),
        ("citation_alignment", 0.10),
        ("summarization", 0.10),
        ("fact_extraction", 0.10),
        ("retrieval_failure_isolated", 0.10),
        ("comprehension_failure_isolated", 0.10),
    };
    // Registered names from Maturation300M (6 metrics). Necessity and
    // evidence use dominate — knowing WHEN to retrieve and using ONLY
    // the retrieved text is the capability; quality/conflict/revision
    // are the honesty surfaces.
    private static readonly (string metric, double w)[]
        RagMetricWeights =
    {
        ("retrieval_necessity", 0.20),
        ("evidence_use", 0.20),
        ("citation_correctness", 0.15),
        ("document_conflict", 0.15),
        ("revision_awareness", 0.15),
        ("retrieval_quality", 0.15),
    };
    // Registered names from Maturation300M (8 metrics). Arithmetic
    // weighting is flat — a model that adds but cannot carry is not
    // half-good at arithmetic, it is broken at carry_borrow.
    private static readonly (string metric, double w)[]
        MathMetricWeights =
    {
        ("add_sub", 0.15),
        ("mul_div", 0.15),
        ("carry_borrow", 0.13),
        ("percentage", 0.12),
        ("ratio", 0.10),
        ("parentheses", 0.10),
        ("simple_algebra", 0.12),
        ("word_problem", 0.13),
    };
    // Registered names from Maturation300M (6 metrics). Syntax and
    // function dominate the floor; fim/bug_fix are the editing
    // surfaces; small_multi_file is the shallow repo boundary (large
    // agent work stays out of scope per the canonical suite notes).
    private static readonly (string metric, double w)[]
        CodingMetricWeights =
    {
        ("syntax", 0.20),
        ("function", 0.20),
        ("unit_task", 0.20),
        ("fim", 0.15),
        ("bug_fix", 0.15),
        ("small_multi_file", 0.10),
    };
    // Registered names from Maturation300M (6 metrics). These are
    // decode-surface measurements, not SFT-trainable skills — the
    // capability gate is the §15/§16 OFF/ON comparison, so
    // native_thinking is intentionally absent from
    // SupportedCapabilities (an SFT plan for it fails closed).
    private static readonly (string metric, double w)[]
        ThinkingMetricWeights =
    {
        ("off_baseline", 0.30),
        ("accuracy_gain", 0.25),
        ("token_cost", 0.15),
        ("branch_acceptance", 0.15),
        ("latency", 0.10),
        ("gpu_cost", 0.05),
    };
    private static (string metric, double w)[] MetricWeights =>
        Capability switch
        {
            "context_tracking" => ContextMetricWeights,
            "multi_turn" => MultiTurnMetricWeights,
            "structured_output" => StructuredMetricWeights,
            "tool_calling" => ToolMetricWeights,
            "reading_grounding" => ReadingMetricWeights,
            "rag" => RagMetricWeights,
            "math" => MathMetricWeights,
            "coding" => CodingMetricWeights,
            "native_thinking" => ThinkingMetricWeights,
            _ => InstructionMetricWeights,
        };

    // ------------------------------------------------------------ pools --

    private static readonly (string topic, string[] items)[] ZhTopics =
    {
        ("水果", new[] { "蘋果", "香蕉", "芒果", "葡萄", "西瓜", "鳳梨", "草莓", "橘子" }),
        ("交通工具", new[] { "腳踏車", "捷運", "公車", "高鐵", "機車", "渡輪" }),
        ("運動", new[] { "游泳", "慢跑", "羽球", "籃球", "登山", "瑜珈" }),
        ("台灣城市", new[] { "台北", "台中", "高雄", "台南", "花蓮", "台東" }),
        ("家電", new[] { "電風扇", "洗衣機", "冰箱", "吸塵器", "電鍋" }),
        ("動物", new[] { "台灣黑熊", "石虎", "藍鵲", "梅花鹿", "獼猴" }),
        ("職業", new[] { "護理師", "消防員", "廚師", "教師", "工程師" }),
        ("蔬菜", new[] { "高麗菜", "地瓜葉", "胡蘿蔔", "洋蔥", "玉米筍" }),
        ("文具", new[] { "鉛筆", "橡皮擦", "尺", "剪刀", "釘書機" }),
        ("節日", new[] { "春節", "中秋節", "端午節", "元宵節", "重陽節" }),
    };

    private static readonly (string q, string a)[] ZhYesNo =
    {
        ("天空是藍色的嗎", "是"), ("魚會飛嗎", "否"), ("冰是熱的嗎", "否"),
        ("一年有十二個月嗎", "是"), ("貓是哺乳動物嗎", "是"),
        ("汽車可以在水上行駛嗎", "否"), ("太陽從東邊升起嗎", "是"),
        ("一週有八天嗎", "否"), ("水在零度會結冰嗎", "是"),
        ("企鵝會飛嗎", "否"), ("台灣四面環海嗎", "是"),
        ("月亮會自己發光嗎", "否"), ("人是植物嗎", "否"),
        ("火車在軌道上行駛嗎", "是"), ("沙漠雨量很多嗎", "否"),
        ("蜜蜂會採蜜嗎", "是"), ("石頭會呼吸嗎", "否"),
        ("冬天比夏天熱嗎", "否"), ("書本可以閱讀嗎", "是"),
        ("蜘蛛有八隻腳嗎", "是"),
    };

    private static readonly (string q, string a)[] ZhShort =
    {
        ("台灣的首都是哪裡", "台北"), ("一年有幾個季節", "四個"),
        ("水的化學式是什麼", "H2O"), ("一星期有幾天", "七天"),
        ("紅綠燈的紅燈代表什麼", "停"), ("中華民國國旗有幾種顏色", "三種"),
        ("人一天通常睡幾小時", "八小時"), ("一斤有幾兩", "十六兩"),
    };

    private static readonly (string entity, string prop, string value, string unit)[] ZhProps =
    {
        ("台灣最高峰玉山", "高度", "3952", "公尺"),
        ("台北市", "人口級別", "約250萬", "人"),
        ("一杯水", "容量", "240", "毫升"),
        ("標準籃球場", "長度", "28", "公尺"),
        ("成年人", "正常體溫", "37", "度"),
        ("高速公路", "速限", "110", "公里"),
        ("教室黑板", "寬度", "約4", "公尺"),
        ("一打雞蛋", "數量", "12", "顆"),
    };

    private static readonly (string q, string a)[] EnYesNo =
    {
        ("Is the sky blue", "YES"), ("Can fish fly", "NO"),
        ("Is ice hot", "NO"), ("Does a week have seven days", "YES"),
        ("Is water wet", "YES"), ("Do penguins fly", "NO"),
        ("Is the sun a star", "YES"), ("Can humans breathe underwater", "NO"),
    };

    private static readonly string[] EnTopics =
    {
        "fruits", "colors", "animals", "sports", "tools", "vehicles",
    };
    private static readonly Dictionary<string, string[]> EnItems = new()
    {
        ["fruits"] = new[] { "apple", "banana", "mango", "grape" },
        ["colors"] = new[] { "red", "blue", "green", "yellow" },
        ["animals"] = new[] { "tiger", "rabbit", "eagle", "whale" },
        ["sports"] = new[] { "tennis", "swimming", "cycling", "boxing" },
        ["tools"] = new[] { "hammer", "wrench", "drill", "chisel" },
        ["vehicles"] = new[] { "bus", "train", "ferry", "scooter" },
    };

    // ----------------------------------------------- context pools ----
    // Reserved eval values (BuildContextSuiteItems) must stay disjoint
    // from every training pool below so suite items cannot collide with
    // training prompts.
    private static readonly string[] CtxCodes =
    {
        "QZ-88", "KX-31", "MN-07", "RT-64", "BV-29", "HP-53", "LX-12",
        "DW-76", "FJ-90", "CY-45", "GT-21", "NK-58", "PQ-33", "SW-69",
        "VM-14", "ZB-82", "TR-55", "EH-04", "UL-97", "OA-26",
    };
    private static readonly string[] CtxDigits =
        { "5827", "4196", "8352", "2679", "9403", "1784", "6538", "2915" };
    private static readonly string[] CtxNames =
    {
        "阿明", "小華", "美玲", "志豪", "淑芬", "建宏", "雅婷", "家豪",
        "怡君", "冠廷", "小琳", "柏翰", "欣怡", "俊傑", "詩涵", "威廷",
    };
    private static readonly string[] CtxColors =
        { "紅色", "藍色", "綠色", "黃色", "紫色", "黑色", "白色", "橘色" };
    private static readonly string[] CtxPlaces =
    {
        "台北", "台中", "高雄", "台南", "花蓮", "宜蘭", "屏東", "嘉義",
        "新竹", "苗栗",
    };
    private static readonly string[] CtxObjects =
        { "鑰匙", "雨傘", "手錶", "錢包", "眼鏡", "水杯", "筆記本", "耳機" };
    private static readonly string[] CtxContainers =
        { "盒子", "抽屜", "背包", "口袋", "櫃子", "箱子" };
    private static readonly string[] CtxAcks =
        { "好的。", "好的，記住了。", "了解了。", "沒問題。", "收到。" };
    private static readonly string[] CtxDistractors =
    {
        "順便說，今天天氣不錯。", "對了，我晚點要去買東西。",
        "這個先放著，我想到再說。", "昨天的會議開得有點久。",
    };

    // ------------------------------------------------------------- rows --

    private sealed class Row
    {
        public string Prompt = "", Completion = "", Category = "",
                      Rule = "", Source = "synthetic";
        public Dictionary<string, object?> ToDict() => new()
        {
            ["prompt"] = Prompt,
            ["completion"] = Completion,
            ["category"] = Category,
            ["capability"] = Capability,
            ["source"] = Source,
            ["rule"] = Rule,
        };
    }

    private static T Take<T>(Random r, T[] xs) => xs[r.Next(xs.Length)];

    private static string[] SampleItems(Random r, string[] pool, int n)
    {
        var copy = pool.ToArray();
        for (int i = copy.Length - 1; i > 0; --i)
            (copy[i], copy[r.Next(i + 1)]) = (copy[r.Next(i + 1)], copy[i]);
        return copy[..Math.Min(n, copy.Length)];
    }

    // Category generators — every row carries a `rule` the quality gate
    // can verify mechanically (§8): line counts, JSON validity, forbidden
    // substrings, max length, language.
    private static IEnumerable<Row> Generate(int seed, int count)
    {
        var r = new Random(seed);
        var rows = new List<Row>();
        void Add(Row row) => rows.Add(row);

        // -- A. single explicit instruction (~30%) ---------------------
        foreach (var (q, a) in ZhYesNo)
            for (int i = 0; i < 6; i++)
                Add(new Row
                {
                    Prompt = $"只回答「是」或「否」：{q}？",
                    Completion = a, Category = "A",
                    Rule = $"choice:{a}",
                    Source = i < 2 ? "failure-pool" : "synthetic",
                });
        for (int i = 0; i < count / 10; i++)
        {
            var t = Take(r, ZhTopics);
            int n = 3 + r.Next(3);
            var items = SampleItems(r, t.items, n);
            Add(new Row
            {
                Prompt = $"只列出{n}項{t.topic}，逐條列出，不要解釋。",
                Completion = string.Join("\n", items.Select(x => "- " + x)),
                Category = "A", Rule = $"bullet_count:{n}",
            });
            Add(new Row
            {
                Prompt = $"請複誦：{items[0]}",
                Completion = items[0], Category = "A",
                Rule = $"exact:{items[0]}", Source = "failure-pool",
            });
        }
        foreach (var (q, a) in ZhShort)
            for (int i = 0; i < 4; i++)
                Add(new Row
                {
                    Prompt = $"用繁體中文回答：{q}？答案不超過8個字。",
                    Completion = a, Category = "A",
                    Rule = "max_chars:8;lang:zh",
                });

        // -- B. multi-condition (~20%) ----------------------------------
        for (int i = 0; i < count / 7; i++)
        {
            var t = Take(r, ZhTopics);
            int n = 3 + r.Next(3);
            var items = SampleItems(r, t.items, n);
            string marker = Take(r, new[] { "◦", "※", "→" });
            Add(new Row
            {
                Prompt = $"以繁體中文列出{n}個{t.topic}，每項一行並以" +
                         $"「{marker}」開頭，最後一行輸出 JSON：" +
                         "{\"count\": 數字}。",
                Completion = string.Join("\n",
                                 items.Select(x => $"{marker} {x}")) +
                             $"\n{{\"count\": {n}}}",
                Category = "B",
                Rule = $"bullet_count:{n};marker:{marker};tail_json",
            });
            var (entity, prop, value, unit) = Take(r, ZhProps);
            Add(new Row
            {
                Prompt = $"使用繁體中文回答。先寫一句不超過15字的說明，" +
                         $"再輸出 JSON 物件：{entity}的{prop}？",
                Completion = $"{entity}的{prop}如下。\n" +
                             $"{{\"name\": \"{entity}\", \"value\": " +
                             $"\"{value}\", \"unit\": \"{unit}\"}}",
                Category = "B", Rule = "tail_json;lang:zh",
            });
            var shuffled = SampleItems(r, t.items, 4);
            Add(new Row
            {
                Prompt = "使用繁體中文將以下項目整理為 JSON 字串陣列，" +
                         $"依原順序，不要增加或刪減項目：{string.Join("、", shuffled)}",
                Completion = "[" + string.Join(",",
                    shuffled.Select(x => $"\"{x}\"")) + "]",
                Category = "B", Rule = "json_arr",
            });
        }

        // -- C. negative constraints (~20%) ------------------------------
        for (int i = 0; i < count / 6; i++)
        {
            var (q, a) = ZhYesNo[r.Next(ZhYesNo.Length)];
            var t = Take(r, ZhTopics);
            var items = SampleItems(r, t.items, 3);
            Add(new Row
            {
                Prompt = $"{q}？不要使用表格，不要加入額外資訊。",
                Completion = a == "是" ? "是。" : "否。",
                Category = "C", Rule = "no_char:|;no_char:表;max_chars:6",
            });
            Add(new Row
            {
                Prompt = $"列出{items[0]}、{items[1]}、{items[2]}三者的" +
                         "共同分類。不要解釋原因，不要分段。",
                Completion = $"三者同屬於{t.topic}。",
                Category = "C",
                Rule = "no_char:\n;no_sub:因為;no_sub:所以",
            });
            Add(new Row
            {
                Prompt = $"將「{items[0]}」翻譯成英文，只輸出翻譯結果，" +
                         "不要加引號，不要加說明。",
                Completion = ZhEn(items[0]),
                Category = "C", Rule = "no_char:「;no_char:」;max_chars:24",
            });
            Add(new Row
            {
                Prompt = $"{q}？只輸出答案本身，不要輸出問句，" +
                         "不要加上「答：」前綴。",
                Completion = a, Category = "C",
                Rule = $"exact:{a}",
            });
        }

        // -- D. output formats (~12%) -------------------------------------
        for (int i = 0; i < count / 9; i++)
        {
            var (entity, prop, value, unit) = Take(r, ZhProps);
            Add(new Row
            {
                Prompt = $"輸出 JSON 物件，欄位固定且依序為 name、value、" +
                         $"unit，不要其他欄位：{entity}的{prop}。",
                Completion = $"{{\"name\": \"{entity}\", \"value\": " +
                             $"\"{value}\", \"unit\": \"{unit}\"}}",
                Category = "D", Rule = "json_obj;field_order:name,value,unit",
            });
            var t = Take(r, ZhTopics);
            var items = SampleItems(r, t.items, 3);
            Add(new Row
            {
                Prompt = $"以 key=value 格式逐行輸出以下配對，順序固定：" +
                         $"第一名={items[0]}，第二名={items[1]}，" +
                         $"第三名={items[2]}",
                Completion = $"第一名={items[0]}\n第二名={items[1]}\n" +
                             $"第三名={items[2]}",
                Category = "D",
                Rule = $"line_count:3;order:第一名,第二名,第三名",
            });
            Add(new Row
            {
                Prompt = $"以 Markdown 輸出：一個 ## 標題「{t.topic}」" +
                         "加上恰好三個清單項目。",
                Completion = $"## {t.topic}\n- {items[0]}\n- {items[1]}\n- {items[2]}",
                Category = "D", Rule = "head:##;bullet_count:3",
            });
            Add(new Row
            {
                Prompt = $"依序輸出下列{t.topic}的中文名稱，以頓號分隔，" +
                         $"不要加其他內容：{items[2]}、{items[0]}、{items[1]}",
                Completion = $"{items[2]}、{items[0]}、{items[1]}",
                Category = "D",
                Rule = $"order:{items[2]},{items[0]},{items[1]};no_char:\n",
            });
        }

        // -- E. instruction priority (~10%) -------------------------------
        for (int i = 0; i < count / 11; i++)
        {
            var t = Take(r, ZhTopics);
            int n = 3 + r.Next(3);
            var items = SampleItems(r, t.items, n);
            Add(new Row
            {
                Prompt = $"主要任務：列出{n}個{t.topic}。" +
                         "格式要求：每行一項，以「- 」開頭。" +
                         "限制：使用繁體中文，不要加說明。",
                Completion = string.Join("\n",
                    items.Select(x => "- " + x)),
                Category = "E",
                Rule = $"bullet_count:{n};lang:zh",
            });
            Add(new Row
            {
                Prompt = $"先確認{entity2(r)}的{prop2(r)}，再以 JSON 輸出" +
                         "{\"ok\": true}。只輸出最終結果。",
                Completion = "{\"ok\": true}",
                Category = "E", Rule = "json_obj;exact_json:ok",
            });
        }

        // -- F. ambiguous instruction (~8%) --------------------------------
        for (int i = 0; i < count / 14; i++)
        {
            var t = Take(r, ZhTopics);
            var items = SampleItems(r, t.items, 3);
            Add(new Row
            {
                Prompt = $"摘要以下內容：{t.topic}包含許多種類，" +
                         $"例如{items[0]}、{items[1]}與{items[2]}，" +
                         "各有不同的特性與用途。",
                Completion = $"說明{t.topic}的多樣性，" +
                             $"並以{items[0]}等為例。",
                Category = "F", Rule = "lang:zh;max_chars:60",
            });
            Add(new Row
            {
                Prompt = "把以下內容改寫得更通順：" +
                         $"{items[0]}是非常好吃而且是{t.topic}裡面的。",
                Completion = $"{items[0]}是{t.topic}中很受歡迎的一種。",
                Category = "F", Rule = "lang:zh;max_chars:60",
            });
        }

        // -- English subset (~10%) ------------------------------------------
        foreach (var (q, a) in EnYesNo)
            for (int i = 0; i < 3; i++)
                Add(new Row
                {
                    Prompt = $"Reply with only YES or NO: {q}?",
                    Completion = a, Category = "A",
                    Rule = $"choice:{a}",
                });
        for (int i = 0; i < count / 16; i++)
        {
            string tp = Take(r, EnTopics);
            var items = SampleItems(r, EnItems[tp], 3);
            Add(new Row
            {
                Prompt = $"List exactly 3 {tp} as a JSON array of strings.",
                Completion = "[" + string.Join(",",
                    items.Select(x => $"\"{x}\"")) + "]",
                Category = "B", Rule = "json_arr",
            });
            Add(new Row
            {
                Prompt = $"Output key=value lines for: first={items[0]}, " +
                         $"second={items[1]}. No other text.",
                Completion = $"first={items[0]}\nsecond={items[1]}",
                Category = "D", Rule = "line_count:2",
            });
        }
        return rows;
    }

    // Context-tracking row generator. Rows embed earlier turns inside the
    // prompt via the canonical <|eot|>/<|assistant|>/<|user|> markup — the
    // --chat wrap then produces a faithful multi-turn transcript. Ack
    // turns never repeat the tracked value, so recall must come from the
    // user's own statement (matching the recorded multiturn_memory probe
    // semantics). Categories: A code/digit recall, B entity binding,
    // C update tracking, D list count/index, E multi-binding,
    // F order + distractor rejection.
    private static string Ctx(string state, string ack, string q) =>
        $"{state}\n<|eot|>\n<|assistant|>\n{ack}\n<|eot|>\n<|user|>\n{q}";
    private static string Ctx3(string s1, string a1, string s2,
                               string a2, string q) =>
        $"{s1}\n<|eot|>\n<|assistant|>\n{a1}\n<|eot|>\n<|user|>\n" +
        $"{s2}\n<|eot|>\n<|assistant|>\n{a2}\n<|eot|>\n<|user|>\n{q}";

    private static IEnumerable<Row> GenerateContext(int seed, int count)
    {
        var r = new Random(seed);
        var rows = new List<Row>();
        void Add(Row row) => rows.Add(row);
        string Ack() => Take(r, CtxAcks);
        bool Hard() => r.Next(4) == 0; // ~25% tagged failure-pool weighting

        // -- A. code / digit-string recall ------------------------------
        var codeAsk = new[]
        {
            "我剛才給你的代號是什麼？", "代號是什麼？",
            "請告訴我剛才的代號。", "剛才那組代號是？",
        };
        foreach (var code in CtxCodes)
            for (int i = 0; i < 6; i++)
            {
                string state = i % 2 == 0
                    ? $"請記住這個代號：{code}"
                    : $"記住代號 {code}。";
                Add(new Row
                {
                    Prompt = Ctx(state, Ack(), Take(r, codeAsk)),
                    Completion = code, Category = "A",
                    Rule = $"exact:{code}",
                    Source = Hard() ? "failure-pool" : "synthetic",
                });
            }
        foreach (var d in CtxDigits)
            for (int i = 0; i < 4; i++)
                Add(new Row
                {
                    Prompt = Ctx($"記住這個號碼：{d}", Ack(),
                                 Take(r, new[] { "號碼是什麼？",
                                                 "剛才的號碼是多少？" })),
                    Completion = d, Category = "A",
                    Rule = $"exact:{d}",
                    Source = Hard() ? "failure-pool" : "synthetic",
                });
        for (int i = 0; i < count / 20; i++)
        {
            // two-turn depth: state, ack, distractor turn, recall.
            string code = Take(r, CtxCodes);
            Add(new Row
            {
                Prompt = Ctx3($"請記住這個代號：{code}", Ack(),
                              Take(r, CtxDistractors), Ack(),
                              Take(r, codeAsk)),
                Completion = code, Category = "A",
                Rule = $"exact:{code}",
                Source = Hard() ? "failure-pool" : "synthetic",
            });
        }

        // -- B. entity / attribute binding ------------------------------
        var nameAsk = new[]
            { "我叫什麼名字？", "請問我的名字是？", "你記得我的名字嗎？" };
        foreach (var n in CtxNames)
            for (int i = 0; i < 3; i++)
                Add(new Row
                {
                    Prompt = Ctx($"我叫{n}。", Ack(), Take(r, nameAsk)),
                    Completion = n, Category = "B",
                    Rule = $"exact:{n}",
                    Source = Hard() ? "failure-pool" : "synthetic",
                });
        for (int i = 0; i < count / 14; i++)
        {
            string obj = Take(r, CtxObjects);
            string col = Take(r, CtxColors);
            string con = Take(r, CtxContainers);
            string loc = $"{col}{con}";
            Add(new Row
            {
                Prompt = Ctx($"我把{obj}放在{loc}裡。", Ack(),
                             Take(r, new[] { $"{obj}在哪裡？",
                                             $"我的{obj}放在哪裡？" })),
                Completion = $"{loc}裡", Category = "B",
                Rule = $"exact:{loc}裡",
                Source = Hard() ? "failure-pool" : "synthetic",
            });
        }
        for (int i = 0; i < count / 16; i++)
        {
            string n = Take(r, CtxNames);
            string p = Take(r, CtxPlaces);
            Add(new Row
            {
                Prompt = Ctx($"{n}住在{p}。", Ack(), $"{n}住在哪裡？"),
                Completion = p, Category = "B", Rule = $"exact:{p}",
                Source = Hard() ? "failure-pool" : "synthetic",
            });
        }
        for (int i = 0; i < count / 18; i++)
        {
            var t = Take(r, ZhTopics);
            string it = Take(r, t.items);
            Add(new Row
            {
                Prompt = Ctx($"我最喜歡的{t.topic}是{it}。", Ack(),
                             $"我最喜歡的{t.topic}是什麼？"),
                Completion = it, Category = "B", Rule = $"exact:{it}",
            });
        }

        // -- C. update tracking — latest value wins ---------------------
        for (int i = 0; i < count / 8; i++)
        {
            string oldC = Take(r, CtxCodes), newC = Take(r, CtxCodes);
            if (oldC == newC) continue;
            int kind = r.Next(3);
            if (kind == 0)
                Add(new Row
                {
                    Prompt = Ctx($"代號本來是{oldC}。等一下，改成{newC}。",
                                 Ack(),
                                 Take(r, codeAsk)),
                    Completion = newC, Category = "C",
                    Rule = $"exact:{newC};no_sub:{oldC}",
                    Source = Hard() ? "failure-pool" : "synthetic",
                });
            else if (kind == 1)
                Add(new Row
                {
                    Prompt = Ctx3($"請記住這個代號：{oldC}", Ack(),
                                  $"更正一下，代號改成{newC}。", Ack(),
                                  Take(r, codeAsk)),
                    Completion = newC, Category = "C",
                    Rule = $"exact:{newC};no_sub:{oldC}",
                    Source = Hard() ? "failure-pool" : "synthetic",
                });
            else
            {
                string n1 = Take(r, CtxNames), n2 = Take(r, CtxNames);
                if (n1 == n2) continue;
                Add(new Row
                {
                    Prompt = Ctx($"負責人是{n1}。後來換成{n2}。", Ack(),
                                 "現在的負責人是誰？"),
                    Completion = n2, Category = "C",
                    Rule = $"exact:{n2};no_sub:{n1}",
                    Source = Hard() ? "failure-pool" : "synthetic",
                });
            }
        }

        // -- D. list membership / count ----------------------------------
        var ordinal = new[] { "第一", "第二", "第三", "第四", "第五",
                              "第六" };
        for (int i = 0; i < count / 10; i++)
        {
            var t = Take(r, ZhTopics);
            int n = 3 + r.Next(4);
            var items = SampleItems(r, t.items, n);
            string list = $"清單上有：{string.Join("、", items)}。";
            if (r.Next(2) == 0)
                Add(new Row
                {
                    Prompt = Ctx(list, Ack(),
                                 Take(r, new[] { "清單有幾項？",
                                                 "一共有幾樣東西？" })),
                    Completion = $"{n}", Category = "D",
                    Rule = $"exact:{n}",
                    Source = Hard() ? "failure-pool" : "synthetic",
                });
            else
            {
                int k = r.Next(items.Length);
                Add(new Row
                {
                    Prompt = Ctx(list, Ack(),
                                 $"{ordinal[k]}項是什麼？"),
                    Completion = items[k], Category = "D",
                    Rule = $"exact:{items[k]}",
                    Source = Hard() ? "failure-pool" : "synthetic",
                });
            }
        }

        // -- E. multi-entity binding -------------------------------------
        for (int i = 0; i < count / 9; i++)
        {
            string n1 = Take(r, CtxNames), n2 = Take(r, CtxNames);
            string p1 = Take(r, CtxPlaces), p2 = Take(r, CtxPlaces);
            if (n1 == n2 || p1 == p2) continue;
            bool flip = r.Next(2) == 0;
            var (qn, qa) = flip ? (n2, p2) : (n1, p1);
            Add(new Row
            {
                Prompt = Ctx($"{n1}住在{p1}，{n2}住在{p2}。", Ack(),
                             $"{qn}住在哪裡？"),
                Completion = qa, Category = "E", Rule = $"exact:{qa}",
                Source = Hard() ? "failure-pool" : "synthetic",
            });
        }
        for (int i = 0; i < count / 14; i++)
        {
            string n1 = Take(r, CtxNames), n2 = Take(r, CtxNames);
            var t = Take(r, ZhTopics);
            var its = SampleItems(r, t.items, 2);
            if (n1 == n2) continue;
            bool flip = r.Next(2) == 0;
            var (qn, qa) = flip ? (n2, its[1]) : (n1, its[0]);
            Add(new Row
            {
                Prompt = Ctx($"{n1}喜歡{its[0]}，{n2}喜歡{its[1]}。", Ack(),
                             $"{qn}喜歡什麼？"),
                Completion = qa, Category = "E", Rule = $"exact:{qa}",
            });
        }

        // -- F. order / distractor rejection ------------------------------
        for (int i = 0; i < count / 12; i++)
        {
            var t = Take(r, ZhTopics);
            var items = SampleItems(r, t.items, 3);
            bool first = r.Next(2) == 0;
            string ans = first ? items[0] : items[2];
            Add(new Row
            {
                Prompt = Ctx($"順序是：先{items[0]}，再{items[1]}，" +
                             $"最後{items[2]}。", Ack(),
                             first ? "第一個是什麼？" : "最後一個是什麼？"),
                Completion = ans, Category = "F", Rule = $"exact:{ans}",
            });
        }
        for (int i = 0; i < count / 14; i++)
        {
            string n = Take(r, CtxNames);
            Add(new Row
            {
                Prompt = Ctx3($"我叫{n}。", Ack(),
                              Take(r, CtxDistractors), Ack(),
                              Take(r, nameAsk)),
                Completion = n, Category = "F", Rule = $"exact:{n}",
                Source = Hard() ? "failure-pool" : "synthetic",
            });
        }

        // -- English subset (~6%) ----------------------------------------
        foreach (var code in CtxCodes.Take(8))
            for (int i = 0; i < 2; i++)
                Add(new Row
                {
                    Prompt = $"Remember this code: {code}\n<|eot|>\n" +
                             "<|assistant|>\nGot it.\n<|eot|>\n<|user|>\n" +
                             "What was the code?",
                    Completion = code, Category = "A",
                    Rule = $"exact:{code}",
                });
        return rows;
    }

    private static int n2(Random r) => 3 + r.Next(3);
    private static string entity2(Random r) => Take(r, ZhProps).entity;
    private static string prop2(Random r) => Take(r, ZhProps).prop;

    private static readonly Dictionary<string, string> ZhEnMap = new()
    {
        ["蘋果"] = "apple", ["香蕉"] = "banana", ["芒果"] = "mango",
        ["葡萄"] = "grape", ["西瓜"] = "watermelon", ["鳳梨"] = "pineapple",
        ["草莓"] = "strawberry", ["橘子"] = "orange",
        ["腳踏車"] = "bicycle", ["捷運"] = "metro", ["公車"] = "bus",
        ["高鐵"] = "high-speed rail", ["機車"] = "scooter", ["渡輪"] = "ferry",
        ["游泳"] = "swimming", ["慢跑"] = "jogging", ["羽球"] = "badminton",
        ["籃球"] = "basketball", ["登山"] = "hiking", ["瑜珈"] = "yoga",
        ["台北"] = "Taipei", ["台中"] = "Taichung", ["高雄"] = "Kaohsiung",
        ["台南"] = "Tainan", ["花蓮"] = "Hualien", ["台東"] = "Taitung",
        ["電風扇"] = "electric fan", ["洗衣機"] = "washing machine",
        ["冰箱"] = "refrigerator", ["吸塵器"] = "vacuum cleaner",
        ["電鍋"] = "rice cooker",
        ["台灣黑熊"] = "Formosan black bear", ["石虎"] = "leopard cat",
        ["藍鵲"] = "blue magpie", ["梅花鹿"] = "sika deer",
        ["獼猴"] = "macaque",
        ["護理師"] = "nurse", ["消防員"] = "firefighter",
        ["廚師"] = "chef", ["教師"] = "teacher", ["工程師"] = "engineer",
        ["高麗菜"] = "cabbage", ["地瓜葉"] = "sweet potato leaves",
        ["胡蘿蔔"] = "carrot", ["洋蔥"] = "onion", ["玉米筍"] = "baby corn",
        ["鉛筆"] = "pencil", ["橡皮擦"] = "eraser", ["尺"] = "ruler",
        ["剪刀"] = "scissors", ["釘書機"] = "stapler",
        ["春節"] = "Lunar New Year", ["中秋節"] = "Mid-Autumn Festival",
        ["端午節"] = "Dragon Boat Festival",
        ["元宵節"] = "Lantern Festival", ["重陽節"] = "Double Ninth Festival",
    };
    private static string ZhEn(string zh) => ZhEnMap.GetValueOrDefault(zh, zh);

    // -------------------------------------------------------- quality gate

    // §8 — every row must satisfy its declared rule spec; violations drop
    // the row outright (fail closed, no repair on the data lane).
    private static bool QualityOk(Row row, out string reason)
    {
        reason = "";
        string c = row.Completion;
        if (row.Prompt.Trim().Length == 0 || c.Trim().Length == 0)
        { reason = "empty"; return false; }
        if (row.Prompt.Length > 400 || c.Length > 600)
        { reason = "length"; return false; }
        foreach (var rule in row.Rule.Split(';'))
        {
            var kv = rule.Split(':', 2);
            string k = kv[0], v = kv.Length > 1 ? kv[1] : "";
            bool ok = k switch
            {
                "choice" => c.Trim() == v,
                "exact" => c.Trim() == v,
                "bullet_count" =>
                    c.Split('\n').Count(l =>
                        Regex.IsMatch(l.Trim(), @"^[-◦※→*]")) ==
                    int.Parse(v),
                "line_count" =>
                    c.Split('\n').Count(l => l.Trim().Length > 0) ==
                    int.Parse(v),
                "max_chars" => c.Trim().Length <= int.Parse(v),
                "no_char" => !c.Contains(v),
                "no_sub" => !c.Contains(v),
                "marker" => c.Split('\n')
                        .Where(l => l.Trim().Length > 0)
                        .TakeWhile(l => !l.TrimStart().StartsWith('{'))
                        .All(l => l.TrimStart().StartsWith(v)),
                "lang" => v != "zh" || ZhRatio(c) > 0.5,
                "tail_json" => TailParses(c),
                "json_arr" => Parses(c) && c.TrimStart().StartsWith('['),
                "json_obj" => Parses(c) && c.TrimStart().StartsWith('{'),
                "field_order" => FieldOrder(c, v),
                "exact_json" => Parses(c) &&
                                c.Contains($"\"{v}\""),
                "order" => InOrder(c, v.Split(',')),
                "head" => c.TrimStart().StartsWith(v),
                _ => true,
            };
            if (!ok) { reason = rule; return false; }
        }
        return true;
    }

    private static double ZhRatio(string s)
    {
        int zh = s.Count(ch => ch >= '一' && ch <= '鿿');
        int alnum = s.Count(char.IsLetterOrDigit);
        return alnum == 0 ? 0 : (double)zh / alnum;
    }

    private static bool Parses(string s)
    {
        string t = s.Trim();
        // a row may end with the JSON payload after prose lines —
        // accept a trailing JSON block for tail_json rows.
        try { using var _ = JsonDocument.Parse(t); return true; }
        catch { }
        int idx = t.LastIndexOf('\n');
        while (idx >= 0)
        {
            string tail = t[(idx + 1)..].Trim();
            if (tail.Length > 1 && (tail.StartsWith('{') || tail.StartsWith('[')))
                try { using var _ = JsonDocument.Parse(tail); return true; }
                catch { }
            idx = t.LastIndexOf('\n', idx - 1);
        }
        return false;
    }

    private static bool TailParses(string s)
    {
        int idx = s.LastIndexOf('\n');
        if (idx < 0) return Parses(s);
        try
        {
            using var _ = JsonDocument.Parse(s[(idx + 1)..].Trim());
            return true;
        }
        catch { return false; }
    }

    private static bool FieldOrder(string json, string order)
    {
        try
        {
            using var doc = JsonDocument.Parse(json.Trim());
            if (doc.RootElement.ValueKind != JsonValueKind.Object)
                return false;
            var got = doc.RootElement.EnumerateObject()
                        .Select(p => p.Name).ToList();
            return got.SequenceEqual(order.Split(','));
        }
        catch { return false; }
    }

    private static bool InOrder(string s, string[] parts)
    {
        int pos = 0;
        foreach (var p in parts)
        {
            int i = s.IndexOf(p.Trim(), pos, StringComparison.Ordinal);
            if (i < 0) return false;
            pos = i + p.Trim().Length;
        }
        return true;
    }

    // -------------------------------------------------------- suite emit --

    // §20/§21 — the eval suite uses `category` = metric name so the
    // native capability report's per-category pass_rate IS the sub-metric;
    // `fail_code` tags feed the error taxonomy in the aggregation layer.
    private static List<Dictionary<string, object?>> BuildSuiteItems()
    {
        var items = new List<Dictionary<string, object?>>();
        void It(string id, string metric, string check, string prompt,
                string fail, params (string k, object? v)[] extra)
        {
            var d = new Dictionary<string, object?>
            {
                ["id"] = id, ["category"] = metric, ["check"] = check,
                ["prompt"] = prompt, ["fail_code"] = fail,
            };
            foreach (var (k, v) in extra) d[k] = v;
            items.Add(d);
        }

        // instruction_completion — verbatim instruction satisfaction.
        It("ic-echo-1", "instruction_completion", "choice",
           "請複誦：桂花", "IGNORED_INSTRUCTION",
           ("allowed", new List<object?> { "桂花" }),
           ("max_new_tokens", 8));
        It("ic-yesno-1", "instruction_completion", "choice",
           "只回答「是」或「否」：鏡子可以照出影像嗎？",
           "IGNORED_INSTRUCTION",
           ("allowed", new List<object?> { "是" }),
           ("max_new_tokens", 8));
        It("ic-yesno-2", "instruction_completion", "choice",
           "只回答「是」或「否」：鯨魚是魚類嗎？",
           "IGNORED_INSTRUCTION",
           ("allowed", new List<object?> { "否" }),
           ("max_new_tokens", 8));
        It("ic-oneword-1", "instruction_completion", "regex",
           "用一個詞回答：彩虹通常在什麼天氣後出現？",
           "IGNORED_INSTRUCTION",
           ("pattern", "^\\s*(雨|雨後|下雨)\\s*$"),
           ("max_new_tokens", 10));
        It("ic-echo-2", "instruction_completion", "choice",
           "請複誦：紫藤", "IGNORED_INSTRUCTION",
           ("allowed", new List<object?> { "紫藤" }),
           ("max_new_tokens", 8));
        It("ic-yesno-en", "instruction_completion", "choice",
           "Reply with only YES or NO: Is the ocean salty?",
           "IGNORED_INSTRUCTION",
           ("allowed", new List<object?> { "YES", "yes" }),
           ("max_new_tokens", 6));

        // format_accuracy — structure must match exactly.
        It("fa-json-1", "format_accuracy", "json_valid",
           "輸出 JSON 物件，欄位固定且依序為 name、value、unit：" +
           "一張桌子的寬度。", "FORMAT_VIOLATION",
           ("required_fields",
            new List<object?> { "name", "value", "unit" }),
           ("exact_fields", 1),
           ("field_order", new List<object?> { "name", "value", "unit" }),
           ("max_new_tokens", 48));
        It("fa-json-2", "format_accuracy", "json_valid",
           "輸出 JSON 物件，欄位為 city 與 country，不要其他欄位：" +
           "東京所在的城市與國家。", "FORMAT_VIOLATION",
           ("required_fields", new List<object?> { "city", "country" }),
           ("exact_fields", 1), ("max_new_tokens", 48));
        It("fa-bullets-3", "format_accuracy", "count_lines",
           "只列出3項甜點，每行一項以「- 」開頭，不要其他內容。",
           "FORMAT_VIOLATION",
           ("line_pattern", "^- "), ("expected", 3),
           ("total_lines", 3), ("max_new_tokens", 32));
        It("fa-kv-1", "format_accuracy", "regex_all",
           "以 key=value 格式逐行輸出：rank=gold，level=3。" +
           "不要其他文字。", "FORMAT_VIOLATION",
           ("patterns", new List<object?>
            { "^rank=gold$", "^level=3$" }),
           ("max_new_tokens", 24));
        It("fa-order-1", "format_accuracy", "regex",
           "依序輸出：紅、綠、藍，以逗號分隔，不要加其他內容。",
           "ORDER_VIOLATION",
           ("pattern", "紅.*綠.*藍"), ("max_new_tokens", 24));
        It("fa-md-1", "format_accuracy", "regex_all",
           "以 Markdown 輸出：一個 ## 標題加上恰好兩個清單項目。",
           "FORMAT_VIOLATION",
           ("patterns", new List<object?>
            { "##", "- ", "[\\s\\S]*(- [^\\n]*\\n?){2}\\s*$" }),
           ("max_new_tokens", 40));

        // constraint_following — multi-condition instructions.
        It("cf-count-json", "constraint_following", "json_valid",
           "以繁體中文列出3個樂器，每行一項以「◦」開頭，" +
           "最後一行輸出 {\"count\": 3}。", "PARTIAL_INSTRUCTION",
           ("required_fields", new List<object?> { "count" }),
           ("max_new_tokens", 64));
        It("cf-arr-1", "constraint_following", "regex_all",
           "使用繁體中文將以下項目整理為 JSON 字串陣列，依原順序：" +
           "風箏、陀螺、毽子", "PARTIAL_INSTRUCTION",
           ("patterns", new List<object?>
            { "^\\s*[[]", "風箏[\\s\\S]*陀螺[\\s\\S]*毽子", "[]]\\s*$" }),
           ("max_new_tokens", 40));
        It("cf-len-1", "constraint_following", "regex",
           "用繁體中文回答：一天有幾小時？答案不超過8個字。",
           "PARTIAL_INSTRUCTION",
           ("pattern", "^\\s*.{1,8}\\s*$"), ("max_new_tokens", 12));
        It("cf-two-step", "constraint_following", "regex_all",
           "先說明檸檬是水果，再以 JSON 輸出 {\"ok\": true}。" +
           "只輸出最終結果。", "PARTIAL_INSTRUCTION",
           ("patterns", new List<object?>
            { "^\\s*[{]", "\"ok\"\\s*:\\s*true" }),
           ("max_new_tokens", 32));

        // negative_constraint — forbidden content must be absent.
        It("nc-notable-1", "negative_constraint", "not_contains",
           "企鵝住在哪裡？不要使用表格，不要加入額外資訊。",
           "NEGATIVE_CONSTRAINT_VIOLATION",
           ("expected", "|"), ("forbidden", new List<object?> { "表格", "----" }),
           ("max_new_tokens", 32));
        It("nc-noexplain", "negative_constraint", "not_contains",
           "輸出 3 加 4 的結果。不要解釋計算過程。",
           "NEGATIVE_CONSTRAINT_VIOLATION",
           ("expected", "因為"),
           ("forbidden", new List<object?> { "所以", "等於", "加" }),
           ("max_new_tokens", 12));
        It("nc-noextra-1", "negative_constraint", "regex_all",
           "只回答「是」或「否」：玻璃是透明的嗎？",
           "NEGATIVE_CONSTRAINT_VIOLATION",
           ("patterns", new List<object?>
            { "^(?!.*因為)(?!.*所以)(?!.*例如)[\\s\\S]*$", "是" }),
           ("max_new_tokens", 10));
        It("nc-one-line", "negative_constraint", "regex",
           "列出蘋果、香蕉的共同分類。不要列點，不要分段，" +
           "用一句話回答。", "NEGATIVE_CONSTRAINT_VIOLATION",
           ("pattern", "^[^\\n]+$"), ("max_new_tokens", 24));

        // language_accuracy — zh-TW surface.
        It("la-zh-1", "language_accuracy", "regex",
           "用繁體中文回答：台灣最長的河流是哪一條？",
           "LANGUAGE_VIOLATION",
           ("pattern", "濁水溪"), ("max_new_tokens", 16));
        It("la-zh-2", "language_accuracy", "regex",
           "用繁體中文回答：冬天的氣溫通常比夏天高還是低？",
           "LANGUAGE_VIOLATION",
           ("pattern", "低"), ("max_new_tokens", 12));
        It("la-nomix-1", "language_accuracy", "regex",
           "用繁體中文寫出三個交通工具的名稱，以頓號分隔。",
           "LANGUAGE_VIOLATION",
           ("pattern", "^[^a-zA-Z]*$"), ("max_new_tokens", 24));

        // extra_content — output must stop at the asked-for content.
        It("ec-exact-1", "extra_content", "regex",
           "只輸出以下內容，不要加任何字：42",
           "EXTRA_CONTENT",
           ("pattern", "^\\s*42\\s*$"), ("max_new_tokens", 8));
        It("ec-noprefix", "extra_content", "regex",
           "只回答「是」或「否」：巧克力是甜的嗎？",
           "EXTRA_CONTENT",
           ("pattern", "^\\s*是\\s*$"), ("max_new_tokens", 8));
        It("ec-nofence", "extra_content", "regex",
           "輸出 JSON 物件 {\"done\": true}，不要加說明，" +
           "不要用程式碼區塊。", "EXTRA_CONTENT",
           ("pattern", "^(?![\\s\\S]*```)\\s*[{][\\s\\S]*[}]\\s*$"),
           ("max_new_tokens", 24));

        // ambiguous — follow the determinable part only.
        It("am-summary", "instruction_completion", "regex",
           "摘要以下內容：夜市是台灣常見的生活風景，" +
           "聚集了小吃、遊戲與各式攤販。", "HALLUCINATED_REQUIREMENT",
           ("pattern", "夜市|小吃|攤販"), ("max_new_tokens", 40));
        It("am-improve", "instruction_completion", "regex",
           "把以下內容改寫得更通順：今天的天氣是很不錯好的。",
           "HALLUCINATED_REQUIREMENT",
           ("pattern", "天氣"), ("max_new_tokens", 40));
        return items;
    }

    // Context-tracking eval suite — `category` = sub-metric so the
    // native report's per-category pass_rate feeds ContextMetricWeights;
    // every value (codes, names, colors, places, objects) is deliberately
    // disjoint from the Ctx* training pools. fail_code carries the
    // context taxonomy: CONTEXT_FORGOTTEN / ENTITY_SUBSTITUTION /
    // STALE_VALUE / WRONG_COUNT / ORDER_CONFUSED / DISTRACTED.
    private static List<Dictionary<string, object?>> BuildContextSuiteItems()
    {
        var items = new List<Dictionary<string, object?>>();
        void It(string id, string metric, string check, string prompt,
                string fail, params (string k, object? v)[] extra)
        {
            var d = new Dictionary<string, object?>
            {
                ["id"] = id, ["category"] = metric, ["check"] = check,
                ["prompt"] = prompt, ["fail_code"] = fail,
            };
            foreach (var (k, v) in extra) d[k] = v;
            items.Add(d);
        }
        string T2(string s, string ack, string q) =>
            $"{s}\n<|eot|>\n<|assistant|>\n{ack}\n<|eot|>\n<|user|>\n{q}";
        string T3(string s1, string a1, string s2, string a2, string q) =>
            $"{s1}\n<|eot|>\n<|assistant|>\n{a1}\n<|eot|>\n<|user|>\n" +
            $"{s2}\n<|eot|>\n<|assistant|>\n{a2}\n<|eot|>\n<|user|>\n{q}";

        // recall_accuracy — stated code/digit carried across turns.
        It("rc-code-1", "recall_accuracy", "contains",
           T2("請記住這個代號：JX-17", "好的。",
              "我剛才給你的代號是什麼？"),
           "CONTEXT_FORGOTTEN",
           ("expected", "JX-17"), ("max_new_tokens", 16));
        It("rc-code-2", "recall_accuracy", "contains",
           T2("記住代號 AZ-63。", "了解了。", "代號是什麼？"),
           "CONTEXT_FORGOTTEN",
           ("expected", "AZ-63"), ("max_new_tokens", 16));
        It("rc-digit-1", "recall_accuracy", "contains",
           T2("記住這個號碼：7308", "好的。", "號碼是什麼？"),
           "CONTEXT_FORGOTTEN",
           ("expected", "7308"), ("max_new_tokens", 16));
        It("rc-digit-2", "recall_accuracy", "contains",
           T2("幫我記住數字 5164。", "記住了。", "剛才的數字是多少？"),
           "CONTEXT_FORGOTTEN",
           ("expected", "5164"), ("max_new_tokens", 16));
        It("rc-code-deep", "recall_accuracy", "contains",
           T3("請記住這個代號：QF-09", "好的。",
              "順便說，今天天氣不錯。", "收到。", "剛才那組代號是？"),
           "CONTEXT_FORGOTTEN",
           ("expected", "QF-09"), ("max_new_tokens", 16));

        // entity_binding — who/what/where stated once, recalled later.
        It("eb-name-1", "entity_binding", "contains",
           T2("我叫宗翰。", "你好。", "我叫什麼名字？"),
           "ENTITY_SUBSTITUTION",
           ("expected", "宗翰"), ("max_new_tokens", 16));
        It("eb-name-2", "entity_binding", "contains",
           T2("我叫宛儒。", "你好宛儒。", "你記得我的名字嗎？"),
           "ENTITY_SUBSTITUTION",
           ("expected", "宛儒"), ("max_new_tokens", 16));
        It("eb-loc-1", "entity_binding", "contains",
           T2("我把護照放在灰色鐵盒裡。", "好的。", "護照在哪裡？"),
           "ENTITY_SUBSTITUTION",
           ("expected", "灰色"), ("max_new_tokens", 24));
        It("eb-city-1", "entity_binding", "contains",
           T2("彥君住在南投。", "了解了。", "彥君住在哪裡？"),
           "ENTITY_SUBSTITUTION",
           ("expected", "南投"), ("max_new_tokens", 16));
        It("eb-fav-1", "entity_binding", "contains",
           T2("我最喜歡的顏色是粉紅色。", "知道了。",
              "我最喜歡的顏色是什麼？"),
           "ENTITY_SUBSTITUTION",
           ("expected", "粉紅"), ("max_new_tokens", 16));

        // update_tracking — the LATEST value wins, never the stale one.
        It("ut-code-1", "update_tracking", "regex_all",
           T3("請記住這個代號：WK-30", "好的。",
              "更正一下，代號改成 SD-72。", "好的，已更新。",
              "代號是什麼？"),
           "STALE_VALUE",
           ("patterns", new List<object?>
            { "SD-72", "^(?!.*WK-30)[\\s\\S]*$" }),
           ("max_new_tokens", 16));
        It("ut-time-1", "update_tracking", "regex_all",
           T2("會議時間本來是下午三點。改成上午十點。", "好的。",
              "會議現在幾點？"),
           "STALE_VALUE",
           ("patterns", new List<object?>
            { "十點", "^(?!.*三點)[\\s\\S]*$" }),
           ("max_new_tokens", 16));
        It("ut-owner-1", "update_tracking", "regex_all",
           T2("負責人是宗翰。後來換成彥君。", "了解了。",
              "現在的負責人是誰？"),
           "STALE_VALUE",
           ("patterns", new List<object?>
            { "彥君", "^(?!.*宗翰)[\\s\\S]*$" }),
           ("max_new_tokens", 16));

        // count_tracking — cardinality / position inside a stated list.
        It("ct-count-1", "count_tracking", "regex",
           T2("購物清單有：毛巾、牙刷、肥皂、梳子。", "好的。",
              "清單有幾項？"),
           "WRONG_COUNT",
           ("pattern", "[4四]"), ("max_new_tokens", 12));
        It("ct-index-1", "count_tracking", "contains",
           T2("名單依序是：佩珊、俊良、郁雯。", "好的。",
              "第二個人是誰？"),
           "WRONG_COUNT",
           ("expected", "俊良"), ("max_new_tokens", 16));

        // order_tracking — first/last inside a stated sequence.
        It("ot-first-1", "order_tracking", "contains",
           T2("我先去郵局，再去銀行，最後去藥局。", "了解了。",
              "我最先去哪裡？"),
           "ORDER_CONFUSED",
           ("expected", "郵局"), ("max_new_tokens", 16));
        It("ot-last-1", "order_tracking", "contains",
           T2("步驟順序是：加熱、攪拌、冷卻。", "好的。",
              "最後一個步驟是什麼？"),
           "ORDER_CONFUSED",
           ("expected", "冷卻"), ("max_new_tokens", 16));

        // distractor_rejection — an unrelated turn must not displace the
        // tracked fact.
        It("dr-code-1", "distractor_rejection", "contains",
           T3("請記住這個代號：VG-41", "好的。",
              "昨天的會議開得有點久。", "辛苦了。", "代號是什麼？"),
           "DISTRACTED",
           ("expected", "VG-41"), ("max_new_tokens", 16));
        It("dr-name-1", "distractor_rejection", "contains",
           T3("我叫庭萱。", "你好。", "對了，我晚點要去買東西。", "好的。",
              "我叫什麼名字？"),
           "DISTRACTED",
           ("expected", "庭萱"), ("max_new_tokens", 16));
        It("dr-loc-1", "distractor_rejection", "contains",
           T3("我把遙控器放在粉紅色鞋櫃裡。", "知道了。",
              "這個先放著，我想到再說。", "好的。", "遙控器在哪裡？"),
           "DISTRACTED",
           ("expected", "粉紅"), ("max_new_tokens", 24));
        return items;
    }

    // ---------------------------------------------- multi-turn pools ----
    // Multi-turn is a *dialogue-flow* capability — distinct from
    // context_tracking's single-fact recall: follow-up references to the
    // assistant's own previous answer, mid-dialogue corrections,
    // elaboration control, topic shift & return, role discipline, and
    // accumulating multi-step state. Eval-suite values (names, items,
    // times) are deliberately disjoint from these training pools.
    private static readonly (string subject, string benefit)[]
        MtBenefits =
    {
        ("慢跑", "提升心肺功能"), ("游泳", "對關節負擔小"),
        ("登山", "接觸大自然"), ("瑜珈", "增加柔軟度"),
        ("羽球", "訓練反應速度"), ("籃球", "培養團隊合作"),
    };
    private static readonly string[] MtDays =
        { "週一", "週二", "週三", "週四", "週六", "週日" };
    private static readonly string[] MtTasks =
        { "回信", "交報告", "開會", "看醫生", "繳費", "大掃除" };

    private static string Mt(string u1, string a1, string u2) =>
        Ctx(u1, a1, u2);

    private static IEnumerable<Row> GenerateMultiTurn(int seed,
                                                      int count)
    {
        var r = new Random(seed);
        var rows = new List<Row>();
        void Add(Row row) => rows.Add(row);
        string Ack() => Take(r, CtxAcks);
        bool Hard() => r.Next(4) == 0;

        // -- A. followup_reference (~30%) — pronouns / positions /
        //    "the second one" resolved against the assistant's own
        //    previous answer.
        for (int i = 0; i < count / 8; i++)
        {
            var t = Take(r, ZhTopics);
            int n = 3;
            var items = SampleItems(r, t.items, n);
            int k = r.Next(n);
            string ord = k == 0 ? "第一個" : k == 1 ? "第二個" : "第三個";
            Add(new Row
            {
                Prompt = Mt($"只列出{n}項{t.topic}。",
                            string.Join("\n",
                                items.Select(x => "- " + x)),
                            $"{ord}是哪一項？"),
                Completion = items[k], Category = "A",
                Rule = $"exact:{items[k]}",
                Source = Hard() ? "failure-pool" : "synthetic",
            });
        }
        foreach (var (sub, ben) in MtBenefits)
            for (int i = 0; i < 4; i++)
            {
                Add(new Row
                {
                    Prompt = Mt($"什麼是{sub}？", $"{sub}是一種活動。",
                                "它的好處是什麼？"),
                    Completion = ben, Category = "A",
                    Rule = $"exact:{ben}",
                    Source = Hard() ? "failure-pool" : "synthetic",
                });
            }
        for (int i = 0; i < count / 16; i++)
        {
            var t = Take(r, ZhTopics);
            var items = SampleItems(r, t.items, 2);
            Add(new Row
            {
                Prompt = Mt($"我喜歡{items[0]}和{items[1]}。",
                            Ack(), "「它們」指的是什麼？"),
                Completion = $"{items[0]}和{items[1]}",
                Category = "A",
                Rule = $"exact:{items[0]}和{items[1]}",
            });
        }

        // -- B. correction_acceptance (~20%) — the user's mid-dialogue
        //    correction replaces the earlier statement.
        for (int i = 0; i < count / 10; i++)
        {
            string task = Take(r, MtTasks);
            string d1 = Take(r, MtDays);
            string d2 = Take(r, MtDays.Where(d => d != d1).ToArray());
            Add(new Row
            {
                Prompt = Mt($"{task}排在{d1}。改成{d2}。",
                            Ack(), $"{task}是哪天？"),
                Completion = d2, Category = "B",
                Rule = $"exact:{d2};no_sub:{d1}",
            });
        }
        for (int i = 0; i < count / 16; i++)
        {
            var (sub, ben) = Take(r, MtBenefits);
            Add(new Row
            {
                Prompt = Mt($"我覺得{sub}很無聊。",
                            $"{sub}其實{ben}。",
                            "你說得對，{sub}有什麼好處？"
                                .Replace("{sub}", sub)),
                Completion = ben, Category = "B",
                Rule = $"exact:{ben}",
                Source = Hard() ? "failure-pool" : "synthetic",
            });
        }

        // -- C. elaboration_control (~15%) — shorter / longer follow-ups
        //    reshape the previous answer, never restart it.
        var explBase = new[]
        {
            ("什麼是光合作用", "植物利用陽光製造養分。",
             "植物利用陽光、水和二氧化碳製造養分並釋放氧氣。"),
            ("什麼是雷", "雲層放電產生的聲音。",
             "雷是雲層中電荷累積後瞬間放電產生的巨大聲響。"),
            ("什麼是季風", "隨季節改變方向的風。",
             "季風是因海陸受熱差異而隨季節改變方向的大規模風系。"),
        };
        foreach (var (q, short_, long_) in explBase)
            for (int i = 0; i < 3; i++)
            {
                Add(new Row
                {
                    Prompt = Mt($"{q}？簡單回答。", short_,
                                "再詳細一點。"),
                    Completion = long_, Category = "C",
                    Rule = $"exact:{long_}",
                });
                Add(new Row
                {
                    Prompt = Mt($"{q}？", long_, "再簡短一點。"),
                    Completion = short_, Category = "C",
                    Rule = $"exact:{short_}",
                });
            }

        // -- D. topic_shift_return (~15%) — an unrelated turn then a
        //    return cue must resume the original thread.
        var shiftPairs = new[]
        {
            ("幫我決定明天穿什麼", "明天穿輕便外套比較合適。",
             "對了，晚餐吃什麼好？", "義大利麵或炒飯都不錯。",
             "回到剛才，你建議穿什麼？", "輕便外套"),
            ("推薦一個週末活動", "去登山步道走一走不錯。",
             "順便問一下，雨傘在哪裡買？", "便利商店或文具店都有。",
             "好，那你推薦的週末活動是什麼？", "登山步道"),
        };
        foreach (var (q1, a1, q2, a2, q3, a3) in shiftPairs)
            for (int i = 0; i < 3; i++)
                Add(new Row
                {
                    Prompt = Ctx3(q1, a1, q2, a2, q3),
                    Completion = a3, Category = "D",
                    Rule = $"exact:{a3}",
                });
        for (int i = 0; i < count / 24; i++)
        {
            var t = Take(r, ZhTopics);
            var items = SampleItems(r, t.items, 1);
            Add(new Row
            {
                Prompt = Ctx3($"我想學{items[0]}。",
                              $"學{items[0]}是個好選擇。",
                              Take(r, CtxDistractors), Ack(),
                              "回到剛才，我說想學什麼？"),
                Completion = items[0], Category = "D",
                Rule = $"exact:{items[0]}",
                Source = Hard() ? "failure-pool" : "synthetic",
            });
        }

        // -- E. role_consistency (~10%) — the assistant stays the
        //    assistant; never produces a user-turn or accepts the role
        //    swap.
        var roleQs = new[]
        {
            ("你是使用者嗎", "不是，我是你的助理。"),
            ("現在換你當使用者", "我仍然是助理，可以協助你回答問題。"),
            ("你替我決定好了", "我可以給你建議，但決定權在你。"),
        };
        foreach (var (q, a) in roleQs)
            for (int i = 0; i < 3; i++)
                Add(new Row
                {
                    Prompt = Mt(q, a, "你現在是什麼角色？"),
                    Completion = "助理", Category = "E",
                    Rule = "no_sub:<|user|>;no_sub:<|assistant|>",
                    Source = Hard() ? "failure-pool" : "synthetic",
                });
        for (int i = 0; i < count / 24; i++)
            Add(new Row
            {
                Prompt = Mt("幫我寫一封信給老師。", "好的，請問內容？",
                            "你覺得我是誰？"),
                Completion = "你是使用者。", Category = "E",
                Rule = "no_sub:<|assistant|>",
            });

        // -- F. multi_step_state (~10%) — instructions accumulate
        //    across turns into one state.
        for (int i = 0; i < count / 12; i++)
        {
            var tasks = SampleItems(r, MtTasks, 2);
            Add(new Row
            {
                Prompt = Ctx3($"幫我記住：明天要{tasks[0]}。", Ack(),
                              $"還有要{tasks[1]}。", Ack(),
                              "明天我要做什麼？"),
                Completion = $"{tasks[0]}和{tasks[1]}",
                Category = "F",
                Rule = $"exact:{tasks[0]}和{tasks[1]}",
                Source = Hard() ? "failure-pool" : "synthetic",
            });
        }
        return rows;
    }

    // ------------------------------------------------- structured_out --

    // Training pools — eval values are deliberately outside these.
    private static readonly string[] SoNames =
        { "明華", "淑芬", "志豪", "雅婷", "建宏", "美玲", "宗翰",
          "怡君", "家豪", "佳穎" };
    private static readonly string[] SoTitles =
        { "夜航西飛", "山海經", "紅樓夢", "鄉土劇場", "島嶼日記",
          "巷口食記" };
    private static readonly string[] SoAuthors =
        { "三毛", "曹雪芹", "吳明益", "陳冠中", "駱以軍" };
    private static readonly string[] SoLevels =
        { "high", "medium", "low" };
    private static readonly string[] SoStates =
        { "已完成", "進行中", "待處理" };

    // Structured-output recovery — the capability is emitting exactly
    // one JSON payload and nothing else: correct fields, correct value
    // TYPES (unquoted numbers / literal booleans), enum membership and
    // nesting. Every row's completion is one raw JSON object — a
    // prose-wrapped answer is a negative sample even when the JSON
    // inside parses (§9 invalid output is never positive).
    private static IEnumerable<Row> GenerateStructuredOutput(
        int seed, int count)
    {
        var r = new Random(seed);
        var rows = new List<Row>();
        void Add(Row row) => rows.Add(row);
        bool Hard() => r.Next(4) == 0;
        string Name() => Take(r, SoNames);

        // -- A. json_valid (~25%) — bare object, nothing else. --------
        for (int i = 0; i < count / 4; i++)
        {
            var t = Take(r, ZhTopics);
            string item = Take(r, t.items);
            int k = 1 + r.Next(9);
            Add(new Row
            {
                Prompt = $"只輸出 JSON 物件，不要任何其他文字："
                       + $"{{\"name\": \"{item}\", \"count\": {k}}}",
                Completion = $"{{\"name\":\"{item}\",\"count\":{k}}}",
                Category = "A",
                Rule = "json_obj",
                Source = Hard() ? "failure-pool" : "synthetic",
            });
        }
        // ok-echo rows — the degenerate valid-object instruction.
        foreach (bool b in new[] { true, false })
            for (int i = 0; i < 8; i++)
                Add(new Row
                {
                    Prompt = $"只輸出 JSON：{{\"ok\": "
                             + (b ? "true" : "false") + "}。"
                             + "不要其他文字。",
                    Completion = $"{{\"ok\":{(b ? "true" : "false")}}}",
                    Category = "A",
                    Rule = "json_obj",
                });

        // -- B. schema_conformant (~20%) — exact keys, declared order. --
        for (int i = 0; i < count / 10; i++)
        {
            string title = Take(r, SoTitles);
            string author = Take(r, SoAuthors);
            int year = 1950 + r.Next(74);
            Add(new Row
            {
                Prompt = $"回傳 JSON，欄位只能是 title、author、year"
                       + $"（照此順序）。title={title},author={author},"
                       + $"year={year}。只輸出 JSON。",
                Completion = $"{{\"title\":\"{title}\","
                           + $"\"author\":\"{author}\","
                           + $"\"year\":{year}}}",
                Category = "B",
                Rule = "json_obj;field_order:title,author,year",
                Source = Hard() ? "failure-pool" : "synthetic",
            });
        }
        // compact two-field schema.
        for (int i = 0; i < count / 16; i++)
        {
            string id = $"T-{100 + r.Next(900)}";
            string st = Take(r, SoStates);
            Add(new Row
            {
                Prompt = $"輸出 JSON，恰好兩個欄位 id 與 state。"
                       + $"id={id},state={st}。",
                Completion = $"{{\"id\":\"{id}\",\"state\":\"{st}\"}}",
                Category = "B",
                Rule = "json_obj;field_order:id,state",
            });
        }

        // -- C. typed_fields (~20%) — numbers unquoted, booleans
        //    literal; a quoted number is a type error.
        for (int i = 0; i < count / 10; i++)
        {
            int k = 2 + r.Next(48);
            string label = Take(r, SoTitles);
            Add(new Row
            {
                Prompt = $"輸出 JSON：count 為整數（不要加引號），"
                       + $"label 為字串。count={k},label={label}。"
                       + "只輸出 JSON。",
                Completion = $"{{\"count\":{k},\"label\":\"{label}\"}}",
                Category = "C",
                Rule = "json_obj",
                Source = Hard() ? "failure-pool" : "synthetic",
            });
        }
        for (int i = 0; i < count / 16; i++)
        {
            bool ok = r.Next(2) == 0;
            int score = 40 + r.Next(60);
            Add(new Row
            {
                Prompt = $"輸出 JSON：{{\"score\": <數字>, "
                       + $"\"pass\": <布林>}}。score={score},"
                       + $"pass={(ok ? "true" : "false")}。",
                Completion = $"{{\"score\":{score},"
                           + $"\"pass\":{(ok ? "true" : "false")}}}",
                Category = "C",
                Rule = "json_obj",
            });
        }

        // -- D. enum_membership (~15%) — value drawn from a closed set;
        //    a second payload field keeps the shapes distinct so dedup
        //    does not collapse the lane to six canonical rows.
        for (int i = 0; i < count / 8; i++)
        {
            string lv = Take(r, SoLevels);
            string task = Take(r, MtTasks);
            Add(new Row
            {
                Prompt = $"輸出 JSON：{{\"priority\": <值>, \"task\": "
                       + "<字串>}，priority 只能是 high、medium、low "
                       + $"其中之一。選 {lv}。task={task}。",
                Completion = $"{{\"priority\":\"{lv}\","
                           + $"\"task\":\"{task}\"}}",
                Category = "D",
                Rule = $"json_obj;exact_json:{lv}",
                Source = Hard() ? "failure-pool" : "synthetic",
            });
        }
        foreach (string st in SoStates)
            for (int i = 0; i < 6; i++)
            {
                string task = Take(r, MtTasks);
                Add(new Row
                {
                    Prompt = "只輸出 JSON：{\"狀態\": <值>, \"事項\": "
                           + "<字串>}，狀態只能是「已完成」「進行中」"
                           + "「待處理」之一。"
                           + $"選「{st}」。事項={task}。",
                    Completion = $"{{\"狀態\":\"{st}\","
                               + $"\"事項\":\"{task}\"}}",
                    Category = "D",
                    Rule = $"json_obj;exact_json:{st}",
                });
            }

        // -- E. nested_objects (~10%) — an object inside the object.
        for (int i = 0; i < count / 10; i++)
        {
            string n = Name();
            int age = 18 + r.Next(50);
            Add(new Row
            {
                Prompt = $"輸出 JSON：{{\"user\": {{\"name\": <字串>, "
                       + $"\"age\": <數字>}}}}。name={n},age={age}。"
                       + "只輸出 JSON。",
                Completion = $"{{\"user\":{{\"name\":\"{n}\","
                           + $"\"age\":{age}}}}}",
                Category = "E",
                Rule = "json_obj",
                Source = Hard() ? "failure-pool" : "synthetic",
            });
        }

        // -- F. arrays (~10%) — array fields with correct element
        //    types and a matching count.
        for (int i = 0; i < count / 10; i++)
        {
            var t = Take(r, ZhTopics);
            var items = SampleItems(r, t.items, 2 + r.Next(2));
            string arr = string.Join(
                ",", items.Select(x => $"\"{x}\""));
            Add(new Row
            {
                Prompt = $"輸出 JSON：{{\"items\": [<字串陣列>], "
                       + $"\"total\": <數字>}}。items="
                       + $"{string.Join("、", items)}。",
                Completion = $"{{\"items\":[{arr}],"
                           + $"\"total\":{items.Length}}}",
                Category = "F",
                Rule = "json_obj",
                Source = Hard() ? "failure-pool" : "synthetic",
            });
        }
        return rows;
    }

    // Multi-turn eval suite — `category` = sub-metric feeding
    // MultiTurnMetricWeights; every entity/time/item value is disjoint
    // from the Mt* training pools. fail_code carries the multi-turn
    // taxonomy: REFERENCE_LOST / CORRECTION_IGNORED /
    // RESTARTED_INSTEAD_OF_EXTENDED / TOPIC_LOST / ROLE_CONFUSED /
    // STATE_LOST.
    private static List<Dictionary<string, object?>>
        BuildMultiTurnSuiteItems()
    {
        var items = new List<Dictionary<string, object?>>();
        void It(string id, string metric, string check, string prompt,
                string fail, params (string k, object? v)[] extra)
        {
            var d = new Dictionary<string, object?>
            {
                ["id"] = id, ["category"] = metric, ["check"] = check,
                ["prompt"] = prompt, ["fail_code"] = fail,
            };
            foreach (var (k, v) in extra) d[k] = v;
            items.Add(d);
        }
        string T2(string s, string ack, string q) =>
            $"{s}\n<|eot|>\n<|assistant|>\n{ack}\n<|eot|>\n<|user|>\n{q}";
        string T3(string s1, string a1, string s2, string a2, string q) =>
            $"{s1}\n<|eot|>\n<|assistant|>\n{a1}\n<|eot|>\n<|user|>\n" +
            $"{s2}\n<|eot|>\n<|assistant|>\n{a2}\n<|eot|>\n<|user|>\n{q}";

        // followup_reference — resolve against the assistant's own
        // previous answer (values: 荔枝/火龍果/酪梨 are outside ZhTopics).
        It("fr-pos-1", "followup_reference", "contains",
           T2("只列出3項水果。", "- 荔枝\n- 火龍果\n- 酪梨",
              "第二個是哪一項？"),
           "REFERENCE_LOST",
           ("expected", "火龍果"), ("max_new_tokens", 12));
        It("fr-pos-2", "followup_reference", "contains",
           T2("只列出3項文具。", "- 螢光筆\n- 美工刀\n- 迴紋針",
              "最後一項是什麼？"),
           "REFERENCE_LOST",
           ("expected", "迴紋針"), ("max_new_tokens", 12));
        It("fr-pron-1", "followup_reference", "contains",
           T2("什麼是打瞌睡？", "打瞌睡是短暫的小睡。",
              "它通常發生在什麼時候？"),
           "REFERENCE_LOST",
           ("expected", "打瞌睡"), ("max_new_tokens", 24));
        It("fr-pron-2", "followup_reference", "contains",
           T2("我喜歡咖啡和烏龍茶。", "好的。",
              "「它們」指的是什麼？"),
           "REFERENCE_LOST",
           ("expected", "咖啡"), ("max_new_tokens", 16));

        // correction_acceptance — the correction wins (eval days/values
        // 週五/上午十一點 disjoint from MtDays/MtTasks pools).
        It("ca-day-1", "correction_acceptance", "regex_all",
           T2("面試排在週五。改成週六。", "好的。",
              "面試是哪天？"),
           "CORRECTION_IGNORED",
           ("patterns", new List<object?>
            { "週六", "^(?!.*週五)[\\s\\S]*$" }),
           ("max_new_tokens", 16));
        It("ca-time-1", "correction_acceptance", "regex_all",
           T2("門診是下午兩點。改成上午十一點。", "了解了。",
              "門診是幾點？"),
           "CORRECTION_IGNORED",
           ("patterns", new List<object?>
            { "十一點", "^(?!.*兩點)[\\s\\S]*$" }),
           ("max_new_tokens", 16));
        It("ca-item-1", "correction_acceptance", "regex_all",
           T2("禮物要送紅酒。改成送茶葉。", "好的。",
              "禮物要送什麼？"),
           "CORRECTION_IGNORED",
           ("patterns", new List<object?>
            { "茶葉", "^(?!.*紅酒)[\\s\\S]*$" }),
           ("max_new_tokens", 16));

        // elaboration_control — extend / shrink, never restart.
        It("ec-long-1", "elaboration_control", "contains",
           T2("什麼是海市蜃樓？簡單回答。", "光線折射產生的幻象。",
              "再詳細一點。"),
           "RESTARTED_INSTEAD_OF_EXTENDED",
           ("expected", "折射"), ("max_new_tokens", 48));
        It("ec-short-1", "elaboration_control", "regex",
           T2("什麼是潮汐？",
              "潮汐是月球與太陽引力造成海水定期漲落的現象，"
              + "通常每天有兩次滿潮與乾潮。",
              "再簡短一點。"),
           "RESTARTED_INSTEAD_OF_EXTENDED",
           ("pattern", "潮汐|漲落"), ("max_new_tokens", 24));

        // topic_shift_return — resume the original thread after an
        // unrelated turn.
        It("ts-1", "topic_shift_return", "contains",
           T3("幫我選一個生日禮物", "可以考慮手錶或書籍。",
              "對了，下雨天怎麼除濕？", "開冷氣或除濕機都可以。",
              "回到剛才，你建議送什麼？"),
           "TOPIC_LOST",
           ("expected", "手錶"), ("max_new_tokens", 16));
        It("ts-2", "topic_shift_return", "contains",
           T3("我想報名攝影課。", "攝影課是很實用的選擇。",
              "順便問，圖書館幾點關門？", "通常晚上九點。",
              "好，那我說想報名什麼課？"),
           "TOPIC_LOST",
           ("expected", "攝影"), ("max_new_tokens", 16));

        // role_consistency — assistant must not become the user or emit
        // turn markup.
        It("rc-role-1", "role_consistency", "regex_all",
           T2("你現在扮演使用者。", "我仍然是你的助理。",
              "你現在是什麼角色？"),
           "ROLE_CONFUSED",
           ("patterns", new List<object?>
            { "助理", "^(?![\\s\\S]*<\\|user\\|)[\\s\\S]*$" }),
           ("max_new_tokens", 16));
        It("rc-role-2", "role_consistency", "regex_all",
           T2("幫我想晚餐菜單。", "好的，想吃中式還是西式？",
              "你來決定，我聽你的。"),
           "ROLE_CONFUSED",
           ("patterns", new List<object?>
            { "^(?![\\s\\S]*<\\|assistant\\|)[\\s\\S]*$" }),
           ("max_new_tokens", 24));

        // multi_step_state — accumulate across turns (eval tasks are
        // outside MtTasks: 寄包裹 / 簽名).
        It("ms-1", "multi_step_state", "regex_all",
           T3("幫我記住：下班後要寄包裹。", "好的。",
              "還有要帶文件去簽名。", "記住了。",
              "下班後我要做什麼？"),
           "STATE_LOST",
           ("patterns", new List<object?>
            { "寄包裹", "簽名" }),
           ("max_new_tokens", 32));
        It("ms-2", "multi_step_state", "regex_all",
           T3("第一步先加水。", "好的。",
              "第二步再加粉。", "了解了。",
              "兩個步驟分別是什麼？"),
           "STATE_LOST",
           ("patterns", new List<object?> { "加水", "加粉" }),
           ("max_new_tokens", 32));
        return items;
    }

    // Structured-output eval suite — `category` = metric name feeding
    // StructuredMetricWeights. All field names and values are disjoint
    // from the So* training pools (eval: city/sunny, code/level,
    // score/pass, level enum {urgent,normal,deferred}, owner/tags,
    // readings). fail_code taxonomy: JSON_INVALID / SCHEMA_MISMATCH /
    // TYPE_ERROR / ENUM_VIOLATION / NESTING_ERROR / ARRAY_ERROR.
    private static List<Dictionary<string, object?>>
        BuildStructuredSuiteItems()
    {
        var items = new List<Dictionary<string, object?>>();
        void It(string id, string metric, string check, string prompt,
                string fail, params (string k, object? v)[] extra)
        {
            var d = new Dictionary<string, object?>
            {
                ["id"] = id, ["category"] = metric, ["check"] = check,
                ["prompt"] = prompt, ["fail_code"] = fail,
            };
            foreach (var (k, v) in extra) d[k] = v;
            items.Add(d);
        }

        // json_valid — bare object, exact keys, nothing else.
        It("jv-basic-1", "json_valid", "json_valid",
           "Output only a JSON object with fields city (string) and "
           + "sunny (boolean). city=Tainan, sunny=true. No other text.",
           "JSON_INVALID",
           ("required_fields", new List<object?> { "city", "sunny" }),
           ("field_types", new Dictionary<string, object?>
            { ["city"] = "string", ["sunny"] = "boolean" }),
           ("max_new_tokens", 40));
        It("jv-exact-1", "json_valid", "json_valid",
           "只輸出 JSON：{\"done\": false}。不要任何其他文字。",
           "JSON_INVALID",
           ("required_fields", new List<object?> { "done" }),
           ("exact_fields", 1),
           ("field_types", new Dictionary<string, object?>
            { ["done"] = "boolean" }),
           ("max_new_tokens", 24));

        // schema_conformant — exact field set and declared order.
        It("sc-order-1", "schema_conformant", "json_valid",
           "Return a JSON object with EXACTLY the keys code, label, "
           + "retry — in that order. code=\"E7\", label=\"timeout\", "
           + "retry=false. Only the object.",
           "SCHEMA_MISMATCH",
           ("required_fields",
            new List<object?> { "code", "label", "retry" }),
           ("exact_fields", 1),
           ("field_order",
            new List<object?> { "code", "label", "retry" }),
           ("field_types", new Dictionary<string, object?>
            { ["code"] = "string", ["label"] = "string",
              ["retry"] = "boolean" }),
           ("max_new_tokens", 56));
        It("sc-two-1", "schema_conformant", "json_valid",
           "輸出 JSON，恰好兩個欄位 month 與 day。month=11,day=30。",
           "SCHEMA_MISMATCH",
           ("required_fields",
            new List<object?> { "month", "day" }),
           ("exact_fields", 1),
           ("field_types", new Dictionary<string, object?>
            { ["month"] = "number", ["day"] = "number" }),
           ("max_new_tokens", 32));

        // typed_fields — a quoted digit fails the number type check.
        It("tf-num-1", "typed_fields", "json_valid",
           "Output JSON with fields score (integer, NOT a string) and "
           + "note (string). score=88, note=pass. Only JSON.",
           "TYPE_ERROR",
           ("required_fields",
            new List<object?> { "score", "note" }),
           ("field_types", new Dictionary<string, object?>
            { ["score"] = "number", ["note"] = "string" }),
           ("max_new_tokens", 40));
        It("tf-bool-1", "typed_fields", "json_valid",
           "輸出 JSON：enabled 為布林值（不是字串），ratio 為數字。"
           + "enabled=true,ratio=0.5。只輸出 JSON。",
           "TYPE_ERROR",
           ("required_fields",
            new List<object?> { "enabled", "ratio" }),
           ("field_types", new Dictionary<string, object?>
            { ["enabled"] = "boolean", ["ratio"] = "number" }),
           ("max_new_tokens", 40));

        // enum_membership — value inside the closed set only.
        It("em-en-1", "enum_membership", "json_valid",
           "Output JSON {\"level\": <value>} where value must be one "
           + "of: urgent, normal, deferred. Choose urgent. Only JSON.",
           "ENUM_VIOLATION",
           ("required_fields", new List<object?> { "level" }),
           ("field_values", new Dictionary<string, object?>
            { ["level"] = new List<object?>
              { "urgent", "normal", "deferred" } }),
           ("max_new_tokens", 24));
        It("em-zh-1", "enum_membership", "json_valid",
           "只輸出 JSON：{\"優先級\": <值>}，值只能是"
           + "「緊急」「普通」「延後」其中之一。選「緊急」。",
           "ENUM_VIOLATION",
           ("required_fields", new List<object?> { "優先級" }),
           ("field_values", new Dictionary<string, object?>
            { ["優先級"] = new List<object?>
              { "緊急", "普通", "延後" } }),
           ("max_new_tokens", 24));

        // nested_objects — object inside object.
        It("no-user-1", "nested_objects", "json_valid",
           "Output JSON {\"owner\": {\"name\": <string>, \"id\": "
           + "<number>}} with owner.name=\"Kai\", owner.id=42. "
           + "Only JSON.",
           "NESTING_ERROR",
           ("required_fields", new List<object?> { "owner" }),
           ("field_types", new Dictionary<string, object?>
            { ["owner"] = "object" }),
           ("max_new_tokens", 48));
        It("no-deep-1", "nested_objects", "json_valid",
           "Produce nested JSON: {\"meta\": {\"version\": 2}, "
           + "\"ok\": true}. Follow this shape exactly. Only JSON.",
           "NESTING_ERROR",
           ("required_fields",
            new List<object?> { "meta", "ok" }),
           ("field_types", new Dictionary<string, object?>
            { ["meta"] = "object", ["ok"] = "boolean" }),
           ("max_new_tokens", 48));

        // arrays — real array fields, correct element types and count.
        It("ar-list-1", "arrays", "json_valid",
           "輸出 JSON：{\"items\": [<字串陣列>], \"total\": <數字>}。"
           + "items=鉛筆、橡皮擦、尺。只輸出 JSON。",
           "ARRAY_ERROR",
           ("required_fields",
            new List<object?> { "items", "total" }),
           ("field_types", new Dictionary<string, object?>
            { ["items"] = "array", ["total"] = "number" }),
           ("max_new_tokens", 56));
        It("ar-empty-1", "arrays", "json_valid",
           "Output JSON {\"readings\": []} — readings must be an empty "
           + "array, not a string. Only JSON.",
           "ARRAY_ERROR",
           ("required_fields", new List<object?> { "readings" }),
           ("field_types", new Dictionary<string, object?>
            { ["readings"] = "array" }),
           ("max_new_tokens", 24));
        return items;
    }

    // --------------------------------------------------- tool_calling --

    // Tool surface for TRAINING only — the eval suite deliberately uses
    // a different tool set (search/calculator/get_news) so a passing
    // score means the contract transferred, not that one name was
    // memorised. Payload contract is the canonical
    // <tool_call>{"name":..,"arguments":{..}}</tool_call> the native
    // check splits on.
    private static readonly (string name, string sig, string desc)[]
        TcTools =
    {
        ("translate", "text,to_lang", "翻譯一段文字"),
        ("get_stock", "symbol", "查股票即時價格"),
        ("book_ticket", "from,to,date", "訂車票"),
        ("set_reminder", "time,text", "設定提醒"),
        ("unit_convert", "value,from,to", "單位換算"),
        ("wiki_lookup", "title", "查百科條目"),
    };
    private static readonly string[] TcArgsText =
        { "早安你好", "這份報告的重點", "明天的會議紀要", "產品說明書",
          "使用手冊第三節", "給客戶的感謝信" };
    private static readonly string[] TcStocks =
        { "2330", "2317", "2454", "2881", "2412" };
    private static readonly string[] TcCities =
        { "台北", "高雄", "台中", "台南", "花蓮", "新竹" };
    private static readonly string[] TcDates =
        { "2026-10-02", "2026-10-05", "2026-10-10", "2026-11-01" };
    private static readonly string[] TcTopics =
        { "半導體", "再生能源", "電動車", "央行利率", "颱風動態" };
    private static readonly string[] TcStableFacts =
        { "水在標準大氣壓下的沸點是幾度？", "一年有幾個月？",
          "光的真空速度約是多少？", "中文「謝謝」的英文怎麼說？",
          "地球繞太陽一圈約多久？", "一加一等於多少？",
          "一週有幾天？", "一小時有幾分鐘？", "彩虹有幾種顏色？",
          "水的化學式是什麼？", "三角形有幾個內角？", "十月有幾天？",
          "人體正常體溫約幾度？", "一斤等於幾兩？", "法國的首都是哪裡？",
          "日本的首都是哪裡？", "一星期有幾個小時？",
          "「大」的反義詞是什麼？" };
    private static readonly string[] TcStableAnswers =
        { "100°C", "12個月", "每秒約30萬公里", "Thank you",
          "約365天", "2", "7天", "60分鐘", "7種", "H2O", "3個",
          "31天", "約37°C", "16兩", "巴黎", "東京", "168小時", "小" };

    // Tool-calling recovery — five surfaces: pick the right tool, emit
    // typed arguments, decline when no tool is needed, fold a result
    // back into the answer, and stay honest on tool errors. The
    // negative surfaces (necessity/failure) are trained as real
    // completions — a spurious <tool_call> or a fabricated value is a
    // failure the model must learn to avoid, not coverage noise.
    private static IEnumerable<Row> GenerateToolCalling(
        int seed, int count)
    {
        var r = new Random(seed);
        var rows = new List<Row>();
        void Add(Row row) => rows.Add(row);
        bool Hard() => r.Next(4) == 0;
        string ToolList() => string.Join(
            "、", TcTools.Select(t => $"{t.name}({t.sig})"));
        string Call(string name, string args) =>
            $"<tool_call>{{\"name\":\"{name}\",\"arguments\":{{{args}}}}}"
            + "</tool_call>";

        // -- A. tool_selection (~25%) — declared tool list, need →
        //    correct <tool_call> with name + plausible arguments.
        for (int i = 0; i < count / 4; i++)
        {
            switch (r.Next(6))
            {
                case 0:
                {
                    string text = Take(r, TcArgsText);
                    string lang = Take(r, new[] { "en", "ja", "zh-TW" });
                    Add(new Row
                    {
                        Prompt = $"可用工具：{ToolList()}。使用者想把「"
                               + $"{text}」翻成 {lang}。輸出正確的 "
                               + "<tool_call>。",
                        Completion = Call("translate",
                            $"\"text\":\"{text}\",\"to_lang\":\"{lang}\""),
                        Category = "A", Rule = "head:<tool_call>",
                        Source = Hard() ? "failure-pool" : "synthetic",
                    });
                    break;
                }
                case 1:
                {
                    string sym = Take(r, TcStocks);
                    Add(new Row
                    {
                        Prompt = $"Tools: {ToolList()}. The user asks "
                               + $"for the current price of stock {sym}."
                               + " Emit the appropriate <tool_call>.",
                        Completion = Call("get_stock",
                            $"\"symbol\":\"{sym}\""),
                        Category = "A", Rule = "head:<tool_call>",
                    });
                    break;
                }
                case 2:
                {
                    string from = Take(r, TcCities);
                    string to = Take(r, TcCities.Where(
                        c => c != from).ToArray());
                    string date = Take(r, TcDates);
                    Add(new Row
                    {
                        Prompt = $"可用工具：{ToolList()}。使用者要訂 "
                               + $"{date} 從{from}到{to}的車票。輸出 "
                               + "<tool_call>。",
                        Completion = Call("book_ticket",
                            $"\"from\":\"{from}\",\"to\":\"{to}\","
                            + $"\"date\":\"{date}\""),
                        Category = "A", Rule = "head:<tool_call>",
                        Source = Hard() ? "failure-pool" : "synthetic",
                    });
                    break;
                }
                case 3:
                {
                    int v = 10 + r.Next(90);
                    string from = Take(r, new[] { "km", "kg", "cm" });
                    string to = from == "km" ? "mile"
                              : from == "kg" ? "lb" : "inch";
                    Add(new Row
                    {
                        Prompt = $"Tools: {ToolList()}. Convert {v} "
                               + $"{from} to {to}. Emit <tool_call>.",
                        Completion = Call("unit_convert",
                            $"\"value\":{v},\"from\":\"{from}\","
                            + $"\"to\":\"{to}\""),
                        Category = "A", Rule = "head:<tool_call>",
                    });
                    break;
                }
                case 4:
                {
                    string topic = Take(r, TcTopics);
                    Add(new Row
                    {
                        Prompt = $"可用工具：{ToolList()}。使用者想看"
                               + $"「{topic}」的百科條目。輸出 "
                               + "<tool_call>。",
                        Completion = Call("wiki_lookup",
                            $"\"title\":\"{topic}\""),
                        Category = "A", Rule = "head:<tool_call>",
                    });
                    break;
                }
                default:
                {
                    string tm = $"{7 + r.Next(12)}:30";
                    string what = Take(r, MtTasks);
                    Add(new Row
                    {
                        Prompt = $"Tools: {ToolList()}. Remind the user "
                               + $"at {tm} to {what}. Emit "
                               + "<tool_call>.",
                        Completion = Call("set_reminder",
                            $"\"time\":\"{tm}\",\"text\":\"{what}\""),
                        Category = "A", Rule = "head:<tool_call>",
                        Source = Hard() ? "failure-pool" : "synthetic",
                    });
                    break;
                }
            }
        }

        // -- B. argument_correctness (~25%) — argument VALUES are the
        //    point: correct keys, correct types, no dropped fields.
        for (int i = 0; i < count / 4; i++)
        {
            if (r.Next(2) == 0)
            {
                int v = 5 + r.Next(200);
                Add(new Row
                {
                    Prompt = "Emit <tool_call> for unit_convert with "
                           + $"value={v}, from=\"kg\", to=\"lb\".",
                    Completion = Call("unit_convert",
                        $"\"value\":{v},\"from\":\"kg\",\"to\":\"lb\""),
                    Category = "B", Rule = "head:<tool_call>",
                    Source = Hard() ? "failure-pool" : "synthetic",
                });
            }
            else
            {
                string from = Take(r, TcCities);
                string to = Take(r, TcCities.Where(
                    c => c != from).ToArray());
                string date = Take(r, TcDates);
                Add(new Row
                {
                    Prompt = $"輸出 book_ticket 的 <tool_call>：起點 "
                           + $"{from}、終點 {to}、日期 {date}。",
                    Completion = Call("book_ticket",
                        $"\"from\":\"{from}\",\"to\":\"{to}\","
                        + $"\"date\":\"{date}\""),
                    Category = "B", Rule = "head:<tool_call>",
                });
            }
        }

        // -- C. tool_necessity (~20%) — stable knowledge: answer
        //    directly, NO markup. The completion is the plain answer.
        for (int i = 0; i < count / 5; i++)
        {
            int q = r.Next(TcStableFacts.Length);
            int variant = r.Next(3);
            string prompt = variant switch
            {
                0 => $"可用工具：{ToolList()}。使用者問：「"
                   + $"{TcStableFacts[q]}」不需要工具——直接回答，"
                   + "不要輸出 <tool_call>。",
                1 => $"Tools: {ToolList()}. The user asks a stable "
                   + $"fact: {TcStableFacts[q]} No tool is needed — "
                   + "answer directly WITHOUT emitting <tool_call>.",
                _ => $"可用工具：{ToolList()}。這是常識問題：「"
                   + $"{TcStableFacts[q]}」直接回答即可，無需呼叫工具。",
            };
            Add(new Row
            {
                Prompt = prompt,
                Completion = TcStableAnswers[q],
                Category = "C", Rule = "no_sub:<tool_call>",
                Source = Hard() ? "failure-pool" : "synthetic",
            });
        }

        // -- D. result_interpretation (~15%) — fold a returned result
        //    into the answer; the value must survive verbatim.
        for (int i = 0; i < count / 7; i++)
        {
            if (r.Next(2) == 0)
            {
                string sym = Take(r, TcStocks);
                int price = 50 + r.Next(950);
                string ans = $"{sym} 目前價格為 {price} 元。";
                Add(new Row
                {
                    Prompt = $"get_stock 回傳 {{\"symbol\":\"{sym}\","
                           + $"\"price\":{price}}}。使用者原本問 {sym} "
                           + "現在多少錢。用工具結果回答。",
                    Completion = ans,
                    Category = "D",
                    Rule = $"exact:{ans}",
                    Source = Hard() ? "failure-pool" : "synthetic",
                });
            }
            else
            {
                string text = Take(r, TcArgsText);
                Add(new Row
                {
                    Prompt = "translate returned "
                           + "{\"translated\":\"Bonjour le monde\"}. "
                           + "The user asked to translate 「" + text
                           + "」. Answer with the translated text.",
                    Completion = "Bonjour le monde",
                    Category = "D", Rule = "exact:Bonjour le monde",
                });
            }
        }

        // -- E. failure_recovery (~15%) — tool errors: state the
        //    failure, never fabricate the value the tool did not
        //    return. Prompts carry a scenario so the surface varies.
        var errScenarios = new[]
        {
            ("原本想查股價", "股價", "the stock price"),
            ("原本想查百科", "百科條目", "the wiki entry"),
            ("原本想訂票", "車票", "the ticket booking"),
            ("原本想查匯率", "匯率", "the exchange rate"),
        };
        for (int i = 0; i < count / 7; i++)
        {
            var (scZh, whatZh, whatEn) = Take(r, errScenarios);
            string err = Take(r, new[] { "timeout", "rate_limited",
                                         "not_found",
                                         "service_unavailable" });
            bool zh = r.Next(2) == 0;
            int retry = 1 + r.Next(9);
            Add(new Row
            {
                Prompt = zh
                    ? $"使用者{scZh}，但工具回傳 "
                      + $"{{\"error\":\"{err}\"}}。向使用者說明查詢失敗"
                      + $"並建議 {retry} 分鐘後再試——不要假造{whatZh}"
                      + "的資料。"
                    : $"The user wanted {whatEn}, but the tool returned "
                      + $"{{\"error\":\"{err}\"}}. Tell the user the "
                      + $"lookup failed and suggest retrying in {retry} "
                      + "minutes — do NOT fabricate a result.",
                Completion = zh
                    ? $"抱歉，查詢失敗（{err}）。建議 {retry} 分鐘後再試。"
                    : $"Sorry, the lookup failed ({err}). Please try "
                      + $"again in {retry} minutes.",
                Category = "E", Rule = "no_sub:<tool_call>",
                Source = Hard() ? "failure-pool" : "synthetic",
            });
        }
        return rows;
    }

    private static List<Dictionary<string, object?>>
        BuildToolCallingSuiteItems()
    {
        var items = new List<Dictionary<string, object?>>();
        void It(string id, string metric, string check, string prompt,
                string fail, params (string k, object? v)[] extra)
        {
            var d = new Dictionary<string, object?>
            {
                ["id"] = id, ["category"] = metric, ["check"] = check,
                ["prompt"] = prompt, ["fail_code"] = fail,
            };
            foreach (var (k, v) in extra) d[k] = v;
            items.Add(d);
        }

        // tool_selection — eval tools (search/calculator/get_news) are
        // deliberately outside the TRAINING tool pool; transfer, not
        // memorisation.
        It("tc-sel-1", "tool_selection", "tool_call",
           "Tools: search(query), calculator(expr), get_news(topic). "
           + "User asks for today's exchange rate — retrieval required. "
           + "Emit the appropriate <tool_call> block.",
           "WRONG_TOOL", ("tool_name", "search"),
           ("max_new_tokens", 48));
        It("tc-sel-2", "tool_selection", "tool_call",
           "Tools: search(query), calculator(expr), get_news(topic). "
           + "User asks: what is 732 / 12? Emit the appropriate "
           + "<tool_call> block.",
           "WRONG_TOOL", ("tool_name", "calculator"),
           ("max_new_tokens", 48));
        It("tc-sel-3", "tool_selection", "tool_call",
           "可用工具：search(query)、calculator(expr)、"
           + "get_news(topic)。使用者想看最新的太空新聞。輸出正確的 "
           + "<tool_call>。",
           "WRONG_TOOL", ("tool_name", "get_news"),
           ("max_new_tokens", 48));

        // argument_correctness — the payload fields must be present
        // with the right values.
        It("tc-arg-1", "argument_correctness", "regex_all",
           "Emit <tool_call>{\"name\":\"search\",\"arguments\":"
           + "{\"query\":\"...\"}}</tool_call> for the question: "
           + "recent typhoon warnings.",
           "ARG_MISSING",
           ("patterns", new List<object?>
            { "<tool_call>", "search", "query", "typhoon" }),
           ("max_new_tokens", 64));
        It("tc-arg-2", "argument_correctness", "regex_all",
           "Emit <tool_call>{\"name\":\"calculator\",\"arguments\":"
           + "{\"expr\":\"...\"}}</tool_call> for 17 * 23.",
           "ARG_MISSING",
           ("patterns", new List<object?>
            { "calculator", "expr", "17", "23" }),
           ("max_new_tokens", 64));

        // tool_necessity — stable facts: answer directly, no markup.
        It("tc-nec-1", "tool_necessity", "not_contains",
           "Tools: search(query), calculator(expr). User asks a stable "
           + "fact: how many sides does a triangle have? Answer "
           + "directly WITHOUT emitting <tool_call>.",
           "SPURIOUS_CALL", ("expected", "<tool_call>"),
           ("max_new_tokens", 32));
        It("tc-nec-2", "tool_necessity", "not_contains",
           "可用工具：search(query)、calculator(expr)。使用者問常識："
           + "「彩虹有幾種顏色？」直接回答，不要輸出 <tool_call>。",
           "SPURIOUS_CALL", ("expected", "<tool_call>"),
           ("max_new_tokens", 32));

        // result_interpretation — the returned value must reach the
        // answer verbatim.
        It("tc-int-1", "result_interpretation", "contains",
           "Tool result from search: {\"answer\": \"Taipei 101 is 508 "
           + "metres tall\"}. Answer the user's question using this "
           + "result: how tall is Taipei 101?",
           "IGNORED_RESULT", ("expected", "508"),
           ("max_new_tokens", 48));
        It("tc-int-2", "result_interpretation", "contains",
           "calculator 回傳：{\"result\": 391}。使用者原本問 17 * 23。"
           + "用工具結果回答使用者。",
           "IGNORED_RESULT", ("expected", "391"),
           ("max_new_tokens", 32));

        // failure_recovery — a failed tool yields an honest failure
        // message, never the fabricated answer.
        It("tc-fail-1", "failure_recovery", "not_contains",
           "Tool call failed: {\"error\": \"timeout\", \"tool\": "
           + "\"search\"}. Tell the user the search failed — do NOT "
           + "fabricate an answer by pretending the tool returned "
           + "data.",
           "FABRICATED_RESULT", ("expected", "508"),
           ("max_new_tokens", 64));
        It("tc-fail-2", "failure_recovery", "regex",
           "search 工具回傳 error: not_found。向使用者說明查詢沒有"
           + "結果並建議換個關鍵字。回覆中應包含「失敗」「沒有」或"
           + "「關鍵字」語意。",
           "SILENT_FAILURE",
           ("pattern", "失敗|沒有|關鍵字|not found|failed"),
           ("max_new_tokens", 64));
        return items;
    }

    // -------------------------------------------------- reading_ground --

    // Reading/grounding training passages — every fact/value below is
    // deliberately disjoint from BOTH the generated recovery suite and
    // the canonical star-capability-suite-reading-300m items. The
    // capability being trained is *answer-from-text, not memory*: the
    // passage carries the answer and the completion must reproduce it.
    private static readonly (string passage, string question,
                             string answer)[]
        RdQa =
    {
        ("「台北捷運於 1996 年通車，是台灣第一條捷運系統。」",
         "台北捷運哪一年通車？", "1996"),
        ("「日月潭海拔約 748 公尺，是台灣最大的湖泊。」",
         "日月潭的海拔約多少公尺？", "748"),
        ("「台積電成立於 1987 年，首創專業晶圓代工模式。」",
         "台積電成立於哪一年？", "1987"),
        ("「墾丁國家公園成立於 1984 年，位於屏東縣。」",
         "墾丁國家公園位於哪個縣？", "屏東"),
        ("「合歡山主峰標高 3417 公尺，是台灣百岳之一。」",
         "合歡山主峰標高多少？", "3417"),
        ("「滷肉飯是台灣代表性小吃，以五花肉切丁滷製。」",
         "滷肉飯主要使用什麼部位？", "五花肉"),
        ("Passage: \"The museum opens at 10 AM and closes at 5 PM, "
         + "except Mondays when it is closed.\"",
         "Question: when is the museum closed?", "Mondays"),
        ("Passage: \"The ferry to Green Island departs at 8 AM and "
         + "takes about 50 minutes.\"",
         "Question: how long is the ferry ride?", "50"),
        ("Passage: \"The coastal railway in Hualien was electrified "
         + "in 2014, cutting the trip to under two hours.\"",
         "Question: when was the Hualien railway electrified?", "2014"),
        ("Passage: \"The night market employs about 120 vendors and "
         + "operates until 1 AM on weekends.\"",
         "Question: how many vendors work at the night market?",
         "120"),
        ("「安平古堡建於 1624 年，是台灣最古老的城堡之一。」",
         "安平古堡建於哪一年？", "1624"),
        ("「曾文水庫是台灣最大的水庫，於 1973 年完工。」",
         "曾文水庫於哪一年完工？", "1973"),
        ("「台東熱氣球嘉年華自 2011 年開始舉辦，每年夏季舉行。」",
         "台東熱氣球嘉年華從哪一年開始？", "2011"),
        ("「蘭嶼距離台東約 90 公里，以飛魚文化聞名。」",
         "蘭嶼距離台東約幾公里？", "90"),
        ("「竹塹城是今天新竹市的舊稱，建城於 1827 年。」",
         "竹塹城建城於哪一年？", "1827"),
        ("「萬華龍山寺始建於 1738 年，主祀觀世音菩薩。」",
         "萬華龍山寺始建於哪一年？", "1738"),
    };
    private static readonly string[] RdQaFrames =
        { "文章：{0}問題：{1}（答案在文內）",
          "根據文章回答：{0}問題：{1}",
          "閱讀下列段落並作答：{0}問：{1}" };
    private static readonly string[] RdCities =
        { "台東", "嘉義", "宜蘭", "彰化", "雲林", "苗栗" };
    private static readonly string[] RdProducts =
        { "地瓜", "茶葉", "米", "芒果", "文旦", "蓮霧" };
    private static readonly string[] RdEvents =
        { "產品發表會", "校慶運動會", "社區義診", "讀書會", "義賣市集" };

    private static IEnumerable<Row> GenerateReading(
        int seed, int count)
    {
        var r = new Random(seed);
        var rows = new List<Row>();
        void Add(Row row) => rows.Add(row);
        bool Hard() => r.Next(4) == 0;

        // -- A. document_qa (~18%) — single passage, answer in text;
        //    three framings per fact keep the surface varied.
        foreach (var (p, q, a) in RdQa)
            for (int i = 0; i < count / 40; i++)
            {
                bool zh = ZhRatio(p) > 0.3;
                string prompt = zh
                    ? string.Format(
                        Take(r, RdQaFrames), p, q)
                    : $"{p} {q} (answer is in the text)";
                Add(new Row
                {
                    Prompt = prompt,
                    Completion = a,
                    Category = "A",
                    Rule = $"exact:{a}",
                    Source = Hard() ? "failure-pool" : "synthetic",
                });
            }

        // -- B. multi_passage (~15%) — two sources, synthesize both.
        for (int i = 0; i < count / 6; i++)
        {
            string ev = Take(r, RdEvents);
            string c1 = Take(r, RdCities);
            string c2 = Take(r, RdCities.Where(x => x != c1).ToArray());
            int m1 = 1 + r.Next(6), m2 = 7 + r.Next(5);
            string ans = $"第一階段在{c1}（{m1}月），第二階段在{c2}"
                       + $"（{m2}月）。";
            Add(new Row
            {
                Prompt = $"文件一：「{ev}第一階段{m1}月在{c1}舉行。」"
                       + $"文件二：「第二階段{m2}月移師{c2}。」綜合兩份"
                       + "文件：兩階段分別在哪裡、哪個月？",
                Completion = ans,
                Category = "B", Rule = $"exact:{ans}",
                Source = Hard() ? "failure-pool" : "synthetic",
            });
        }

        // -- C. conflicting_evidence (~12%) — report both claims,
        //    never pick a side.
        for (int i = 0; i < count / 8; i++)
        {
            string prod = Take(r, RdProducts);
            int y1 = 1980 + r.Next(30), y2 = y1 + 1 + r.Next(15);
            string ans = $"來源一說 {y1} 年，來源二說 {y2} 年；兩個來源"
                       + "互相矛盾，無法確定正確年份。";
            Add(new Row
            {
                Prompt = $"來源一：「{prod}於{y1}年開始量產。」來源二："
                       + $"「{prod}於{y2}年開始量產。」兩來源矛盾，請"
                       + "分別指出各說了哪年，不要自行選邊。",
                Completion = ans,
                Category = "C", Rule = $"exact:{ans}",
            });
        }

        // -- D. insufficient_evidence (~12%) — the field is absent;
        //    the honest completion says so.
        for (int i = 0; i < count / 8; i++)
        {
            string city = Take(r, RdCities);
            string prod = Take(r, RdProducts);
            string field = Take(r, new[]
                { "出口量", "平均價格", "種植面積", "產值" });
            string ans = $"文章沒有提到{field}，無法從文中判斷。";
            Add(new Row
            {
                Prompt = $"文章：「{city}的{prod}產量去年創新高，主要"
                       + $"供應國內市場。」問題：該產品的{field}是多少？"
                       + "文中沒有答案，請明說無法判斷。",
                Completion = ans,
                Category = "D",
                Rule = $"exact:{ans}",
                Source = Hard() ? "failure-pool" : "synthetic",
            });
        }

        // -- E. citation_alignment (~10%) — answer names the source.
        for (int i = 0; i < count / 10; i++)
        {
            string city = Take(r, RdCities);
            string prod = Take(r, RdProducts);
            int yr = 1990 + r.Next(30);
            string ans = $"依來源甲，{city}的{prod}於{yr}年獲得認證。"
                       + "（來源甲）";
            Add(new Row
            {
                Prompt = $"[來源甲]「{city}的{prod}在{yr}年獲得地理標誌"
                       + "認證。」[來源乙]「該認證帶動產值成長。」問題："
                       + $"{prod}哪年獲得認證？請引用來源回答。",
                Completion = ans,
                Category = "E", Rule = $"exact:{ans}",
            });
        }

        // -- F. summarization (~10%) — bounded one-sentence summaries.
        for (int i = 0; i < count / 10; i++)
        {
            string ev = Take(r, RdEvents);
            string city = Take(r, RdCities);
            int att = 200 + r.Next(800);
            string ans = $"{ev}在{city}舉行，吸引約 {att} 人參加。";
            Add(new Row
            {
                Prompt = $"請用恰好一句話摘要：「上週六，{city}市公所"
                       + $"舉辦了{ev}，現場湧入約 {att} 位民眾，活動於"
                       + "傍晚順利落幕。」",
                Completion = ans,
                Category = "F",
                Rule = $"exact:{ans}",
                Source = Hard() ? "failure-pool" : "synthetic",
            });
        }

        // -- G. fact_extraction (~12%) — text → typed JSON object.
        for (int i = 0; i < count / 8; i++)
        {
            string city = Take(r, RdCities);
            int yr = 1995 + r.Next(30);
            Add(new Row
            {
                Prompt = $"從句子抽取 JSON {{\"city\": <字串>, \"year\":"
                       + $" <數字>}}：「{city}美術館於{yr}年開幕。」"
                       + "只輸出 JSON。",
                Completion = $"{{\"city\":\"{city}\",\"year\":{yr}}}",
                Category = "G", Rule = "json_obj",
                Source = Hard() ? "failure-pool" : "synthetic",
            });
        }

        // -- H. retrieval_failure_isolated (~10%) — empty retrieval:
        //    state no source, never answer from memory.
        for (int i = 0; i < count / 10; i++)
        {
            string q = Take(r, new[]
                { "冰島的人口是多少？", "火星的直徑多大？",
                  "尼羅河全長幾公里？", "貓的平均壽命？",
                  "喜馬拉雅山有多少座八千公尺峰？", "袋鼠原產於哪裡？",
                  "光速是多少？", "鯨魚的心跳每分鐘幾下？",
                  "世界上最深的湖泊是哪個？", "企鵝分佈在哪些大洲？" });
            bool zh = r.Next(2) == 0;
            string ans = zh ? "沒有提供來源文件，無法根據資料回答。"
                            : "No source documents were provided; I "
                              + "cannot answer from the retrieved "
                              + "material.";
            Add(new Row
            {
                Prompt = zh
                    ? $"檢索結果為空——沒有任何文件。使用者問：「{q}"
                      + "」不得憑記憶回答，請說明沒有來源。"
                    : $"Retrieval returned zero documents. The user "
                      + $"asks: \"{q}\" You must not answer from "
                      + "memory — state that no source was provided.",
                Completion = ans,
                Category = "H",
                Rule = $"exact:{ans}",
                Source = Hard() ? "failure-pool" : "synthetic",
            });
        }

        // -- I. comprehension_failure_isolated (~10%) — the answer IS
        //    in the passage; the failure mode is not finding it.
        for (int i = 0; i < count / 10; i++)
        {
            string ev = Take(r, RdEvents);
            string from = Take(r, new[] { "週六", "週日", "週三" });
            string to = Take(r, new[] { "週日", "下週六", "週五" });
            bool askTo = r.Next(2) == 0;
            string ans = askTo ? to : from;
            Add(new Row
            {
                Prompt = $"文章：「{ev}原定{from}舉行，因場地因素改至"
                       + $"{to}。」問題（答案在文內）：{ev}"
                       + (askTo ? "改到哪一天？" : "原本定在哪一天？"),
                Completion = ans,
                Category = "I", Rule = $"exact:{ans}",
            });
        }
        return rows;
    }

    private static List<Dictionary<string, object?>>
        BuildReadingSuiteItems()
    {
        var items = new List<Dictionary<string, object?>>();
        void It(string id, string metric, string check, string prompt,
                string fail, params (string k, object? v)[] extra)
        {
            var d = new Dictionary<string, object?>
            {
                ["id"] = id, ["category"] = metric, ["check"] = check,
                ["prompt"] = prompt, ["fail_code"] = fail,
            };
            foreach (var (k, v) in extra) d[k] = v;
            items.Add(d);
        }

        // document_qa — passages/values disjoint from canonical suite
        // (no 高鐵/日月潭/衣索比亞) and from the training pool.
        It("rd-qa-1", "document_qa", "contains",
           "文章：「阿里山森林鐵路於 1912 年通車，海拔最高點為 2451 "
           + "公尺。」問題：阿里山森林鐵路最高點海拔多少公尺？",
           "WRONG_ANSWER", ("expected", "2451"),
           ("max_new_tokens", 24));
        It("rd-qa-2", "document_qa", "contains",
           "Passage: \"The night market operates Friday through "
           + "Sunday, from 6 PM to midnight.\" Question: on which "
           + "days does the night market operate?",
           "WRONG_ANSWER", ("expected", "Friday"),
           ("max_new_tokens", 32));

        // multi_passage — synthesize facts from both docs.
        It("rd-mp-1", "multi_passage", "regex_all",
           "Doc A: \"The lighthouse was built in 1883.\" Doc B: \"It "
           + "was automated in 1978.\" State both years in your answer.",
           "INCOMPLETE",
           ("patterns", new List<object?> { "1883", "1978" }),
           ("max_new_tokens", 48));
        It("rd-mp-2", "multi_passage", "contains",
           "段落一：「珍珠奶茶起源於台中。」段落二：「1980 年代後期"
           + "開始風行全美。」綜合兩段：珍珠奶茶起源於哪個城市？",
           "WRONG_ANSWER", ("expected", "台中"),
           ("max_new_tokens", 24));

        // conflicting_evidence — report both, pick no side.
        It("rd-cf-1", "conflicting_evidence", "regex_all",
           "Source 1: \"The bridge cost 2 billion.\" Source 2: \"The "
           + "bridge cost 3.4 billion.\" Report both figures and note "
           + "the discrepancy.",
           "OVERCLAIM",
           ("patterns", new List<object?> { "2", "3.4" }),
           ("max_new_tokens", 48));
        It("rd-cf-2", "conflicting_evidence", "not_contains",
           "來源甲：「該廟建於 1750 年。」來源乙：「該廟建於 1783 年。」"
           + "兩來源矛盾。請指出各說了哪年，不要自行選邊。",
           "OVERCLAIM",
           ("expected", "確定是1750"),
           ("forbidden", new List<object?>
            { "確定建於1750", "確定建於1783" }),
           ("max_new_tokens", 64));

        // insufficient_evidence — the honest answer is "not stated".
        It("rd-is-1", "insufficient_evidence", "regex",
           "文章只提到「該校今年錄取率上升」。問題：明年的錄取率預測"
           + "是多少？文中沒有答案，請明說無法從文章判斷。",
           "FABRICATED",
           ("pattern", "無法|沒有|不能|insufficient|cannot|"
                       + "not.*(mention|state|given)"),
           ("max_new_tokens", 48));
        It("rd-is-2", "insufficient_evidence", "not_contains",
           "Passage: \"The ship departed on Wednesday.\" Question: "
           + "what cargo did it carry? The passage does not say — do "
           + "not invent a cargo.",
           "FABRICATED",
           ("expected", "rice"),
           ("forbidden", new List<object?>
            { "carried rice", "the cargo was" }),
           ("max_new_tokens", 48));

        // citation_alignment — the named source must appear.
        It("rd-ci-1", "citation_alignment", "regex_all",
           "[Source A] \"Typhoons form over warm ocean water.\" "
           + "[Source B] \"Typhoon season peaks in September.\" Answer "
           + "with citation: when does typhoon season peak? Cite the "
           + "relevant source.",
           "NO_CITATION",
           ("patterns", new List<object?>
            { "Source B|來源 B|\\[B\\]|September|九月" }),
           ("max_new_tokens", 64));

        // summarization — exactly N lines.
        It("rd-sm-1", "summarization", "count_lines",
           "Summarize in exactly one sentence: \"Flooding closed the "
           + "coastal highway on Tuesday. Repair crews reopened one "
           + "lane on Wednesday and full traffic resumed Thursday "
           + "morning.\"",
           "FORMAT_ERROR",
           ("expected", 1), ("total_lines", 1),
           ("max_new_tokens", 64));

        // fact_extraction — typed JSON out of prose.
        It("rd-fe-1", "fact_extraction", "json_valid",
           "Extract as JSON {\"city\": ..., \"year\": ...}: \"The "
           + "university in Hsinchu was founded in 1958.\" Only JSON.",
           "FORMAT_ERROR",
           ("required_fields", new List<object?> { "city", "year" }),
           ("field_types", new Dictionary<string, object?>
            { ["city"] = "string", ["year"] = "number" }),
           ("max_new_tokens", 40));

        // retrieval_failure_isolated — no docs → no memory answer.
        It("rd-rf-1", "retrieval_failure_isolated", "not_contains",
           "Retrieved documents are empty — no passages were provided. "
           + "User asks: what is the tallest mountain in Peru? You "
           + "must NOT answer from memory as if a document said it. "
           + "State that no source was provided.",
           "MEMORY_LEAKAGE",
           ("expected", "Huascarán"),
           ("forbidden", new List<object?>
            { "Huascarán", "6,768", "approximately" }),
           ("max_new_tokens", 48));

        // comprehension_failure_isolated — answer IS in the text.
        It("rd-cx-1", "comprehension_failure_isolated", "contains",
           "文章：「演唱會原定週五舉行，因颱風延期至下週二。」問題"
           + "（文內必有答案）：演唱會改到哪一天？",
           "WRONG_ANSWER", ("expected", "週二"),
           ("max_new_tokens", 24));
        It("rd-cx-2", "comprehension_failure_isolated", "contains",
           "Passage: \"The exam moved from Monday to Thursday due to "
           + "the holiday.\" Question (answer is in the text): what "
           + "day is the exam now?",
           "WRONG_ANSWER", ("expected", "Thursday"),
           ("max_new_tokens", 24));
        return items;
    }

    // ------------------------------------------------------------ rag --

    // RAG training pools — all entities/values disjoint from the
    // canonical star-capability-suite-rag-300m (which uses ACME/休假/
    // warranty-24/returns-30/refund-5/office-hours/會議室/price-50-65/
    // 密碼碼數) and from the generated recovery suite below.
    private static readonly string[] RagStableQ =
        { "五乘以六等於多少", "台灣的首都在哪裡",
          "英文字母共有幾個", "水結冰是攝氏幾度",
          "「聰明」的反義詞是什麼", "一斤有幾兩" };
    private static readonly string[] RagPrivateQ =
        { "我們公司的加班費率是多少", "內部系統的登入網址是什麼",
          "本月產品出貨量是多少", "我們的差旅補助上限",
          "內部專案 Phoenix 的負責人是誰", "本季的行銷預算多少" };
    private static readonly (string chunk, string q, string ans)[]
        RagChunks =
    {
        ("「保固期為購買日起 12 個月。」", "保固期多長？",
         "12 個月"),
        ("「訂單滿 500 元免運費。」", "滿多少免運？", "500"),
        ("「客服服務時間為週一至週五上午九點到下午六點。」",
         "客服週末有服務嗎？", "週一至週五"),
        ("\"The trial period is 14 days from signup.\"",
         "How long is the trial?", "14"),
        ("「退貨需在收到商品後七天內提出申請。」",
         "退貨期限多久？", "七天"),
        ("「會員年費為 1200 元，含十二期電子報。」",
         "年費多少錢？", "1200"),
    };
    private static readonly string[] RagDocTopics =
        { "辦公室搬遷", "系統維護", "發薪日", "餐廳營業時間" };

    // RAG recovery — six surfaces: judge retrieval necessity, answer
    // strictly from chunks, cite the right document, report conflicts
    // without picking a side, prefer the superseding revision, and
    // call out irrelevant retrieval. The anti-surfaces (memory answer
    // with empty retrieval, adding unstated conditions, silently
    // picking a side) are trained as negative-adjacent rows with
    // honest completions.
    private static IEnumerable<Row> GenerateRag(int seed, int count)
    {
        var r = new Random(seed);
        var rows = new List<Row>();
        void Add(Row row) => rows.Add(row);
        bool Hard() => r.Next(4) == 0;

        // -- A. retrieval_necessity (~20%) — YES/NO judgments:
        //    stable knowledge → NO; private/realtime → YES.
        for (int i = 0; i < count / 5; i++)
        {
            bool need = r.Next(2) == 0;
            string q = need ? Take(r, RagPrivateQ)
                            : Take(r, RagStableQ);
            int v = r.Next(3);
            bool zh = v != 2;
            string prompt = v switch
            {
                0 => $"判斷：回答「{q}」是否需要檢索文件？回答 YES 或 "
                   + "NO。",
                1 => $"問題：「{q}」。若需要檢索外部或內部文件請回答 "
                   + "YES，否則回答 NO。",
                _ => $"Decide: does answering \"{q}\" require "
                   + "retrieving documents? Answer YES or NO.",
            };
            Add(new Row
            {
                Prompt = prompt,
                Completion = need ? "YES" : "NO",
                Category = "A",
                Rule = $"exact:{(need ? "YES" : "NO")}",
                Source = Hard() ? "failure-pool" : "synthetic",
            });
        }

        // -- B. evidence_use (~20%) — answer from the chunk verbatim;
        //    no unstated additions.
        var ragFrames = new[]
        {
            "檢索到的片段：{0}問題：{1}請只根據片段回答。",
            "根據檢索內容作答：{0}問：{1}",
            "[檢索結果] {0} 使用者問題：{1}（只用檢索內容回答）",
        };
        foreach (var (c, q, a) in RagChunks)
            for (int i = 0; i < count / 30; i++)
            {
                bool zh = ZhRatio(c) > 0.3;
                Add(new Row
                {
                    Prompt = zh
                        ? string.Format(Take(r, ragFrames), c, q)
                        : $"Retrieved chunk: {c} Question: {q} Answer "
                          + "only from the chunk.",
                    Completion = a,
                    Category = "B", Rule = $"exact:{a}",
                    Source = Hard() ? "failure-pool" : "synthetic",
                });
            }

        // -- C. citation_correctness (~15%) — the cited doc must be
        //    the one carrying the answer.
        for (int i = 0; i < count / 6; i++)
        {
            string topic = Take(r, RagDocTopics);
            int v1 = 1 + r.Next(20), v2 = 21 + r.Next(20);
            bool zh = r.Next(2) == 0;
            if (zh)
            {
                string ans = $"公告於第 {v2} 號修正。（來源：文件二）";
                Add(new Row
                {
                    Prompt = $"[文件一]「{topic}公告第 {v1} 號。」"
                           + $"[文件二]「{topic}公告第 {v2} 號為最新版。"
                           + "」問題：目前最新公告是第幾號？回答並引用"
                           + "正確文件。",
                    Completion = ans, Category = "C",
                    Rule = $"exact:{ans}",
                });
            }
            else
            {
                string ans = $"It is version {v2} (Doc 2).";
                Add(new Row
                {
                    Prompt = $"[Doc 1] \"{topic} notice v{v1}.\" "
                           + $"[Doc 2] \"{topic} notice v{v2} is the "
                           + "current revision.\" Question: which is "
                           + "current? Answer and cite the document.",
                    Completion = ans, Category = "C",
                    Rule = $"exact:{ans}",
                    Source = Hard() ? "failure-pool" : "synthetic",
                });
            }
        }

        // -- D. document_conflict (~15%) — two disagreeing docs:
        //    report both claims, never pick a side.
        for (int i = 0; i < count / 6; i++)
        {
            string topic = Take(r, RagDocTopics);
            int x = 5 + r.Next(30), y = x + 1 + r.Next(20);
            string ans = $"文件甲說 {x}，文件乙說 {y}，兩份資料不一致"
                       + "，需要人工確認。";
            Add(new Row
            {
                Prompt = $"[文件甲]「{topic}上限為 {x}。」[文件乙]「"
                       + $"{topic}上限為 {y}。」兩份文件衝突——分別"
                       + "指出各自的數字，不要選邊。",
                Completion = ans, Category = "D",
                Rule = $"exact:{ans}",
                Source = Hard() ? "failure-pool" : "synthetic",
            });
        }

        // -- E. revision_awareness (~15%) — superseding revision wins.
        for (int i = 0; i < count / 6; i++)
        {
            string topic = Take(r, RagDocTopics);
            int v1 = 1 + r.Next(9), v2 = v1 + 1 + r.Next(5);
            int p1 = 100 + r.Next(400), p2 = p1 + 10 + r.Next(200);
            bool zh = r.Next(2) == 0;
            if (zh)
            {
                string ans = $"{p2} 元";
                Add(new Row
                {
                    Prompt = $"[手冊 v{v1}]「{topic}費用 {p1} 元。」"
                           + $"[手冊 v{v2}，取代 v{v1}]「{topic}費用 "
                           + $"{p2} 元。」問題：目前費用是多少？",
                    Completion = ans, Category = "E",
                    Rule = $"exact:{ans}",
                });
            }
            else
            {
                string ans = $"{p2} USD";
                Add(new Row
                {
                    Prompt = $"[Guide v{v1}] \"{topic} fee: {p1} USD.\""
                           + $" [Guide v{v2}, supersedes v{v1}] "
                           + $"\"{topic} fee: {p2} USD.\" Question: "
                           + "current fee?",
                    Completion = ans, Category = "E",
                    Rule = $"exact:{ans}",
                    Source = Hard() ? "failure-pool" : "synthetic",
                });
            }
        }

        // -- F. retrieval_quality (~15%) — irrelevant retrieval: name
        //    the miss, never stretch the chunk to fit.
        for (int i = 0; i < count / 6; i++)
        {
            string topic = Take(r, new[]
                { "園藝", "食譜", "交通時刻", "電影評論" });
            string q = Take(r, new[]
                { "合約違約金怎麼算", "勞基法加班上限",
                  "資遣費計算方式", "消保法退費規定" });
            bool zh = r.Next(3) != 0;
            string ans = zh
                ? "檢索到的內容與問題無關，無法據此回答。"
                : "The retrieved chunks are irrelevant to the "
                  + "question; I cannot answer from them.";
            Add(new Row
            {
                Prompt = zh
                    ? $"檢索到的片段都是關於「{topic}」的內容，但使用者"
                      + $"問的是「{q}」。檢索未命中——請說明證據不相"
                      + "關，不要硬答。"
                    : $"The retrieved chunks are all about \"{topic}\" "
                      + $"but the question is \"{q}\". The retrieval "
                      + "missed — say the evidence is irrelevant "
                      + "instead of forcing an answer.",
                Completion = ans, Category = "F",
                Rule = $"exact:{ans}",
                Source = Hard() ? "failure-pool" : "synthetic",
            });
        }
        return rows;
    }

    private static List<Dictionary<string, object?>>
        BuildRagSuiteItems()
    {
        var items = new List<Dictionary<string, object?>>();
        void It(string id, string metric, string check, string prompt,
                string fail, params (string k, object? v)[] extra)
        {
            var d = new Dictionary<string, object?>
            {
                ["id"] = id, ["category"] = metric, ["check"] = check,
                ["prompt"] = prompt, ["fail_code"] = fail,
            };
            foreach (var (k, v) in extra) d[k] = v;
            items.Add(d);
        }

        // retrieval_necessity — values disjoint from canonical suite
        // (no 2+2/stock close/年假) and from training pools.
        It("rg-ne-1", "retrieval_necessity", "contains",
           "Decide: does answering \"how many minutes are in an hour\" "
           + "require retrieving documents? Answer YES or NO.",
           "NECESSITY_ERROR", ("expected", "NO"),
           ("max_new_tokens", 12));
        It("rg-ne-2", "retrieval_necessity", "contains",
           "Decide: does answering \"what is our internal VPN "
           + "address\" require retrieving documents? Answer YES or "
           + "NO.",
           "NECESSITY_ERROR", ("expected", "YES"),
           ("max_new_tokens", 12));
        It("rg-ne-3", "retrieval_necessity", "contains",
           "判斷：回答「今天的天氣預報」是否需要檢索即時資料？回答 "
           + "YES 或 NO。",
           "NECESSITY_ERROR", ("expected", "YES"),
           ("max_new_tokens", 12));

        // evidence_use — chunk-only answers.
        It("rg-eu-1", "evidence_use", "contains",
           "Retrieved chunk: \"The campus shuttle runs every 20 "
           + "minutes during term time.\" Question: how often does "
           + "the shuttle run? Answer using the chunk.",
           "WRONG_ANSWER", ("expected", "20"),
           ("max_new_tokens", 32));
        It("rg-eu-2", "evidence_use", "not_contains",
           "Retrieved chunk: \"Tickets go on sale at noon.\" Question: "
           + "when do tickets go on sale? Answer ONLY from the chunk "
           + "— do not add unstated conditions.",
           "OVERCLAIM", ("expected", "with ID"),
           ("forbidden", new List<object?>
            { "requires", "must", "需出示" }),
           ("max_new_tokens", 32));

        // citation_correctness — the supporting doc must be named.
        It("rg-cc-1", "citation_correctness", "regex",
           "[Doc 1] \"Check-in opens 3 hours before departure.\" "
           + "[Doc 2] \"Boarding closes 20 minutes before.\" "
           + "Question: when does check-in open? Answer and cite the "
           + "document.",
           "NO_CITATION",
           ("pattern", "Doc ?1|文件 ?1|\\[1\\]"),
           ("max_new_tokens", 48));
        It("rg-cc-2", "citation_correctness", "regex",
           "[文件一]「押金為 500 元。」[文件二]「租金每月 8000 元。」"
           + "問題：押金是多少？回答並引用正確文件。",
           "NO_CITATION",
           ("pattern", "文件一|文件 ?1|\\[一\\]|Doc ?1"),
           ("max_new_tokens", 48));

        // document_conflict — both figures, no side.
        It("rg-dc-1", "document_conflict", "regex_all",
           "[Doc A] \"Delivery takes 2 days.\" [Doc B] \"Delivery "
           + "takes 5 days.\" The docs disagree — report both versions "
           + "with their sources.",
           "OVERCLAIM",
           ("patterns", new List<object?> { "2", "5" }),
           ("max_new_tokens", 64));
        It("rg-dc-2", "document_conflict", "not_contains",
           "[資料甲]「電梯載重 8 人。」[資料乙]「電梯載重 13 人。」"
           + "資料衝突——指出兩個來源各自的說法，不要選邊。",
           "OVERCLAIM", ("expected", "確定是8人"),
           ("forbidden", new List<object?>
            { "就是8人", "就是13人" }),
           ("max_new_tokens", 64));

        // revision_awareness — the superseding revision wins.
        It("rg-ra-1", "revision_awareness", "contains",
           "[Policy v1, 2021] \"Remote work: 1 day per week.\" "
           + "[Policy v3, 2024 — supersedes v1] \"Remote work: 3 days "
           + "per week.\" Question: current remote-work allowance?",
           "STALE_REVISION", ("expected", "3"),
           ("max_new_tokens", 32));
        It("rg-ra-2", "revision_awareness", "not_contains",
           "[舊版規範]「報表截止日為每月 20 日。」[新版規範 v2，取代"
           + "舊版]「報表截止日為每月 25 日。」問題：目前截止日？",
           "STALE_REVISION", ("expected", "20 日"),
           ("forbidden", new List<object?>
            { "20 日", "每月20" }),
           ("max_new_tokens", 32));

        // retrieval_quality — irrelevant chunks → name the miss.
        It("rg-rq-1", "retrieval_quality", "regex",
           "Retrieved chunks are all about cooking recipes, but the "
           + "question is about labor law. The retrieval missed — "
           + "state that the evidence is irrelevant instead of "
           + "answering.",
           "FORCED_ANSWER",
           ("pattern", "irrelevant|unrelated|not.*(about|relevant)|"
                       + "無關|不相關|不符"),
           ("max_new_tokens", 48));

        // retrieval empty → no memory answer.
        It("rg-rq-2", "retrieval_quality", "not_contains",
           "檢索結果為空。使用者問：「我們產品的序號格式是什麼？」不"
           + "得憑記憶回答——請說明沒有檢索到資料。",
           "MEMORY_LEAKAGE", ("expected", "XC-"),
           ("forbidden", new List<object?>
            { "XC-", "格式為" }),
           ("max_new_tokens", 48));
        return items;
    }

    // ----------------------------------------------------------- math --

    private static readonly string[] MathNames =
        { "小明", "小華", "美玲", "阿傑", "淑芬", "志豪" };
    private static readonly string[] MathItems =
        { "顆糖", "顆蘋果", "張貼紙", "本書", "枚硬幣", "杯飲料" };

    // Math recovery — eight arithmetic surfaces, all procedurally
    // generated so train/eval values never collide. Completions are
    // the bare number: reasoning text is not requested and the suite
    // checks first_int, so training the bare answer is the contract.
    private static IEnumerable<Row> GenerateMath(int seed, int count)
    {
        var r = new Random(seed);
        var rows = new List<Row>();
        void Add(Row row) => rows.Add(row);
        bool Hard() => r.Next(4) == 0;
        void Num(string prompt, int ans, string cat)
        {
            Add(new Row
            {
                Prompt = prompt, Completion = ans.ToString(),
                Category = cat, Rule = $"exact:{ans}",
                Source = Hard() ? "failure-pool" : "synthetic",
            });
        }
        bool Zh() => r.Next(3) != 0;

        // -- A. add_sub (~18%) — 2-3 digit add/subtract.
        for (int i = 0; i < count / 5; i++)
        {
            int a = 40 + r.Next(860), b = 15 + r.Next(500);
            if (r.Next(2) == 0)
                Num(Zh() ? $"計算：{a} + {b}。只回答數字。"
                         : $"What is {a} + {b}? Number only.",
                    a + b, "A");
            else
            {
                int hi = Math.Max(a, b), lo = Math.Min(a, b);
                Num(Zh() ? $"計算：{hi} - {lo}。只回答數字。"
                         : $"What is {hi} - {lo}? Number only.",
                    hi - lo, "A");
            }
        }

        // -- B. mul_div (~18%) — small products, exact divisions.
        for (int i = 0; i < count / 5; i++)
        {
            if (r.Next(2) == 0)
            {
                int a = 6 + r.Next(43), b = 3 + r.Next(28);
                Num(Zh() ? $"計算：{a} * {b}。只回答數字。"
                         : $"What is {a} * {b}? Number only.",
                    a * b, "B");
            }
            else
            {
                int b = 3 + r.Next(28), q = 4 + r.Next(40);
                int a = b * q;
                Num(Zh() ? $"計算：{a} / {b}。只回答數字。"
                         : $"What is {a} / {b}? Number only.",
                    q, "B");
            }
        }

        // -- C. carry_borrow (~12%) — sums that cross a digit, round
        //    numbers minus small remainders.
        for (int i = 0; i < count / 8; i++)
        {
            if (r.Next(2) == 0)
            {
                int a = 100 * (1 + r.Next(9)) - r.Next(80);
                int b = 100 * (1 + r.Next(9)) - r.Next(80);
                Num(Zh() ? $"進位加法：{a} + {b} = ? 只回答數字。"
                         : $"Carry addition: {a} + {b} = ? "
                           + "Number only.",
                    a + b, "C");
            }
            else
            {
                int hi = 1000 * (1 + r.Next(9));
                int lo = 120 + r.Next(800);
                Num(Zh() ? $"借位減法：{hi} - {lo} = ? 只回答數字。"
                         : $"Borrow subtraction: {hi} - {lo} = ? "
                           + "Number only.",
                    hi - lo, "C");
            }
        }

        // -- D. percentage (~12%) — x% of n, 打x折 discounts.
        for (int i = 0; i < count / 8; i++)
        {
            if (r.Next(2) == 0)
            {
                int pct = Take(r, new[] { 10, 20, 25, 30, 40, 50,
                                          60, 75, 80 });
                int n = Take(r, new[] { 40, 60, 80, 120, 160, 200,
                                        240, 320, 480, 600 });
                Num(Zh() ? $"{n} 的 {pct}% 是多少？只回答數字。"
                         : $"What is {pct}% of {n}? Number only.",
                    n * pct / 100, "D");
            }
            else
            {
                int z = Take(r, new[] { 9, 8, 7, 6, 5 });
                int price = Take(r, new[] { 200, 300, 400, 500,
                                            800, 1000 });
                Num($"原價 {price} 元，打{z}折後多少元？只回答數字。",
                    price * z / 10, "D");
            }
        }

        // -- E. ratio (~10%) — scale a:b by one side.
        for (int i = 0; i < count / 10; i++)
        {
            int ra = 2 + r.Next(6), rb = 1 + r.Next(5);
            int kb = rb * (2 + r.Next(9));
            Num(Zh()
                    ? $"男女比例 {ra}:{rb}，女生 {kb} 人，男生幾人？"
                      + "只回答數字。"
                    : $"The ratio of A to B is {ra}:{rb}. If B has "
                      + $"{kb}, how many A? Number only.",
                kb * ra / rb, "E");
        }

        // -- F. parentheses (~10%) — grouped ops and precedence.
        for (int i = 0; i < count / 10; i++)
        {
            if (r.Next(2) == 0)
            {
                int a = 2 + r.Next(9), b = 3 + r.Next(9),
                    c = 4 + r.Next(9), d = 1 + r.Next(4);
                Num(Zh() ? $"計算：({a} + {b}) * ({c} - {d})。"
                           + "只回答數字。"
                         : $"What is ({a} + {b}) * ({c} - {d})? "
                           + "Number only.",
                    (a + b) * (c - d), "F");
            }
            else
            {
                int a = 2 + r.Next(9), b = 2 + r.Next(9),
                    c = 2 + r.Next(9);
                Num(Zh() ? $"計算：{a} + {b} * {c}（先乘除後加減）。"
                           + "只回答數字。"
                         : $"What is {a} + {b} * {c}? Number only.",
                    a + b * c, "F");
            }
        }

        // -- G. simple_algebra (~12%) — x+n=m and kx=m.
        for (int i = 0; i < count / 8; i++)
        {
            if (r.Next(2) == 0)
            {
                int x = 3 + r.Next(60), n = 5 + r.Next(40);
                Num(Zh() ? $"解方程式：x + {n} = {x + n}，x = ? "
                           + "只回答數字。"
                         : $"Solve: x + {n} = {x + n}. What is x? "
                           + "Number only.",
                    x, "G");
            }
            else
            {
                int k = 2 + r.Next(8), x = 3 + r.Next(30);
                Num(Zh() ? $"解方程式：{k}x = {k * x}，x = ? "
                           + "只回答數字。"
                         : $"Solve: {k}x = {k * x}. What is x? "
                           + "Number only.",
                    x, "G");
            }
        }

        // -- H. word_problem (~12%) — buy/eat stories, distance,
        //    rate-reading.
        for (int i = 0; i < count / 8; i++)
        {
            string who = Take(r, MathNames);
            string item = Take(r, MathItems);
            switch (r.Next(3))
            {
                case 0:
                {
                    int s = 10 + r.Next(40), eat = 2 + r.Next(8),
                        buy = 3 + r.Next(15);
                    Num($"{who}有 {s} {item}，吃了 {eat} 個，又買了 "
                        + $"{buy} 個。現在幾個？只回答數字。",
                        s - eat + buy, "H");
                    break;
                }
                case 1:
                {
                    int v = 20 + r.Next(80), t = 2 + r.Next(6);
                    Num(Zh()
                            ? $"一台車時速 {v} 公里，開 {t} 小時，共行"
                              + "駛幾公里？只回答數字。"
                            : $"A car travels {v} km per hour for {t} "
                              + "hours. How far? Number only.",
                        v * t, "H");
                    break;
                }
                default:
                {
                    int days = 4 + r.Next(9), per = 10 + r.Next(40);
                    int total = days * per;
                    Num($"一份報告共 {total} 頁，每天讀 {per} 頁，幾天"
                        + "讀完？只回答數字。", days, "H");
                    break;
                }
            }
        }
        return rows;
    }

    private static List<Dictionary<string, object?>>
        BuildMathSuiteItems()
    {
        var items = new List<Dictionary<string, object?>>();
        void It(string id, string metric, string prompt, int expected)
        {
            items.Add(new Dictionary<string, object?>
            {
                ["id"] = id, ["category"] = metric,
                ["check"] = "first_int", ["prompt"] = prompt,
                ["expected"] = expected, ["max_new_tokens"] = 24,
                ["fail_code"] = "WRONG_ANSWER",
            });
        }
        // Values disjoint from the canonical math suite AND outside
        // the training generator's ranges (add a≤899/b≤514, mul a≤48/
        // b≤30, div b≤30, carry a,b≤~899, borrow lo≤919, pct/n from
        // closed lists, ratio rb≤5, paren operands≤9, algebra n≤44/
        // k≤9, word names/objects distinct).
        It("ma-as-1", "add_sub",
           "計算：917 + 388。只回答數字。", 1305);
        It("ma-as-2", "add_sub",
           "What is 1504 - 267? Number only.", 1237);
        It("ma-md-1", "mul_div",
           "What is 23 * 33? Number only.", 759);
        It("ma-md-2", "mul_div",
           "計算：528 / 33。只回答數字。", 16);
        It("ma-cb-1", "carry_borrow",
           "進位加法：1234 + 876 = ? 只回答數字。", 2110);
        It("ma-cb-2", "carry_borrow",
           "Borrow subtraction: 3000 - 1234 = ? Number only.", 1766);
        It("ma-pc-1", "percentage",
           "What is 45% of 260? Number only.", 117);
        It("ma-pc-2", "percentage",
           "原價 640 元，打七五折後多少元？只回答數字。", 480);
        It("ma-ra-1", "ratio",
           "The ratio of pens to pencils is 4:7. If there are 35 "
           + "pencils, how many pens? Number only.", 20);
        It("ma-pa-1", "parentheses",
           "計算：(12 + 8) * (9 - 4)。只回答數字。", 100);
        It("ma-pa-2", "parentheses",
           "What is 11 + 5 * 8? Number only.", 51);
        It("ma-al-1", "simple_algebra",
           "解方程式：x + 63 = 90，x = ? 只回答數字。", 27);
        It("ma-al-2", "simple_algebra",
           "Solve: 11x = 132. What is x? Number only.", 12);
        It("ma-wp-1", "word_problem",
           "小芳有 25 張卡片，送出去 8 張，又抽到 12 張。現在幾張？"
           + "只回答數字。", 29);
        It("ma-wp-2", "word_problem",
           "A bus covers 45 km per hour for 4 hours. How far does it "
           + "go? Number only.", 180);
        return items;
    }

    // coding: 6 registered metrics — syntax / function / unit_task /
    // fim / bug_fix / small_multi_file. Training identifiers are
    // disjoint from the suite's (status/rate/mul4/Neg/second_or_none/
    // min2/cube/shout/geom/tools.py + canonical total/add/avg/is_even/
    // max3/square/greet/triple/first_or_none/math_utils) so eval
    // measures contract transfer, not memorized names.
    private static readonly string[] SynVars =
    {
        "score", "price", "qty", "idx", "flag2", "temp", "ratio2",
        "msg", "buf", "acc",
    };
    private static readonly (string fn, string sig, string spec,
                             string body)[]
        PyFuncs =
    {
        ("plus", "a, b", "their sum", "return a + b"),
        ("minus", "a, b", "a minus b", "return a - b"),
        ("times", "a, b", "their product", "return a * b"),
        ("halve", "x", "x divided by 2", "return x / 2"),
        ("power2", "x", "x squared", "return x * x"),
        ("quadruple", "x", "x times 4", "return x * 4"),
        ("incr", "x", "x plus 1", "return x + 1"),
        ("decr", "x", "x minus 1", "return x - 1"),
        ("bigger", "a, b", "the larger of a and b",
         "return a if a > b else b"),
        ("smaller", "a, b", "the smaller of a and b",
         "return a if a < b else b"),
        ("positive_q", "x", "True when x > 0", "return x > 0"),
        ("neg_q", "x", "True when x < 0", "return x < 0"),
        ("mod_pair", "a, b", "a modulo b", "return a % b"),
        ("pow3", "x", "x cubed", "return x * x * x"),
        ("sign_q", "x", "-1, 0 or 1 by sign",
         "return -1 if x < 0 else (1 if x > 0 else 0)"),
        ("odd_q", "x", "True when x is odd", "return x % 2 != 0"),
        ("diff2", "a, b", "a minus twice b", "return a - 2 * b"),
        ("rem2", "x", "x modulo 2", "return x % 2"),
        ("step2", "x", "x plus 2", "return x + 2"),
        ("back2", "x", "x minus 2", "return x - 2"),
    };
    private static readonly (string fn, string spec, string body)[]
        CppFuncs =
    {
        ("plus", "their sum", "return a + b;"),
        ("minus", "a minus b", "return a - b;"),
        ("times", "their product", "return a * b;"),
        ("power2", "x squared", "return x * x;"),
        ("quadruple", "x times 4", "return x * 4;"),
        ("incr", "x plus 1", "return x + 1;"),
        ("decr", "x minus 1", "return x - 1;"),
        ("bigger", "the larger of a and b", "return a > b ? a : b;"),
        ("smaller", "the smaller of a and b", "return a < b ? a : b;"),
        ("mod_pair", "a modulo b", "return a % b;"),
        ("pow3", "x cubed", "return x * x * x;"),
        ("step2", "x plus 2", "return x + 2;"),
        ("back2", "x minus 2", "return x - 2;"),
        ("diff2", "a minus twice b", "return a - 2 * b;"),
    };
    private static readonly (string fn, string spec, string body)[]
        UnitTasks =
    {
        ("count_positives", "count of elements > 0",
         "return sum(1 for x in nums if x > 0)"),
        ("last_or_default",
         "the last element or d for an empty list",
         "return lst[-1] if len(lst) > 0 else d"),
        ("repeat_str", "s repeated n times", "return s * n"),
        ("count_char", "occurrences of c in s", "return s.count(c)"),
        ("clamp", "v limited to [lo, hi]",
         "return max(lo, min(hi, v))"),
        ("swap_pair", "the pair with elements swapped",
         "return (p[1], p[0])"),
        ("is_vowel", "True when c is a vowel",
         "return c in \"aeiou\""),
        ("join_csv", "items joined by commas",
         "return \",\".join(str(x) for x in items)"),
        ("count_even", "count of even elements",
         "return sum(1 for x in nums if x % 2 == 0)"),
        ("nth_or_zero", "the n-th element or 0 when out of range",
         "return lst[n] if 0 <= n < len(lst) else 0"),
        ("has_dup", "True when any element repeats",
         "return len(lst) != len(set(lst))"),
        ("sum_digits", "sum of the digits of n",
         "return sum(int(c) for c in str(abs(n)))"),
        ("reverse_s", "s reversed", "return s[::-1]"),
        ("pad_s", "s padded with '*' to width w",
         "return s.ljust(w, '*')"),
        ("abs_all", "absolute values of all elements",
         "return [abs(x) for x in nums]"),
        ("count_word", "occurrences of w in text",
         "return text.split().count(w)"),
    };

    private static IEnumerable<Row> GenerateCoding(int seed, int count)
    {
        var r = new Random(seed);
        var rows = new List<Row>();
        void Add(string prompt, string completion, string cat,
                 string rule = "max_chars:550") =>
            rows.Add(new Row
            {
                Prompt = prompt, Completion = completion,
                Category = cat, Rule = rule,
                Source = r.Next(4) == 0 ? "failure-pool" : "synthetic",
            });
        bool Zh() => r.Next(3) != 0;

        // -- A. syntax — one-line declarations/statements.
        for (int i = 0; i < count / 5; i++)
        {
            string v = Take(r, SynVars);
            switch (r.Next(4))
            {
                case 0:
                {
                    int n = r.Next(100);
                    Add(Zh() ? $"寫一行 C++ 宣告 int 變數 {v} 初值 {n}。"
                             : $"Write a one-line C++ statement that "
                               + $"declares an int variable named {v} "
                               + $"initialized to {n}.",
                        $"int {v} = {n};", "A");
                    break;
                }
                case 1:
                {
                    int lo = r.Next(3), hi = lo + 3 + r.Next(8);
                    Add(Zh() ? $"寫一個 Python for 迴圈印出 {lo} 到 "
                               + $"{hi - 1}。"
                             : $"Write a Python for loop printing "
                               + $"{lo} to {hi - 1}.",
                        $"for i in range({lo}, {hi}):\n    print(i)",
                        "A");
                    break;
                }
                case 2:
                {
                    string s = Take(r, new[] { "ok", "done", "idle",
                                               "ready", "run" });
                    Add(Zh() ? $"寫一行 Python 把字串 \"{s}\" 指派給變數 "
                               + $"{v}。"
                             : $"Write a one-line Python statement "
                               + $"assigning \"{s}\" to a variable "
                               + $"named {v}.",
                        $"{v} = \"{s}\"", "A");
                    break;
                }
                default:
                {
                    int a = r.Next(50), b = r.Next(50);
                    Add(Zh() ? $"寫一行 C++ 計算 {a} + {b} 存入變數 {v}。"
                             : $"Write a one-line C++ statement storing "
                               + $"{a} + {b} into int {v}.",
                        $"int {v} = {a} + {b};", "A");
                    break;
                }
            }
        }

        // -- B. function — complete small functions.
        for (int i = 0; i < count / 6; i++)
        {
            if (r.Next(2) == 0)
            {
                var (fn, sig, spec, body) = Take(r, PyFuncs);
                Add(Zh() ? Take(r, new[]
                             {
                                 $"寫一個 Python 函式 `{fn}({sig})` "
                                 + $"回傳{spec}。只輸出函式。",
                                 $"請寫 `{fn}({sig})` 的 Python 函式"
                                 + $"定義，回傳{spec}。",
                                 $"用 Python 定義 `{fn}({sig})`，"
                                 + $"回傳{spec}。",
                             })
                         : Take(r, new[]
                             {
                                 $"Write a Python function "
                                 + $"`{fn}({sig})` returning {spec}. "
                                 + "Output the function only.",
                                 $"Output a Python function "
                                 + $"`{fn}({sig})` that returns "
                                 + $"{spec}.",
                                 $"Define `{fn}({sig})` in Python; it "
                                 + $"returns {spec}.",
                             }),
                    $"def {fn}({sig}):\n    {body}", "B");
            }
            else
            {
                var (fn, spec, body) = Take(r, CppFuncs);
                string sig = fn is "plus" or "minus" or "times"
                             or "bigger" or "smaller"
                    ? "int a, int b" : "int x";
                Add(Zh() ? Take(r, new[]
                             {
                                 $"寫一個 C++ 函式 `int {fn}({sig})` "
                                 + $"回傳{spec}。",
                                 $"請寫 C++ 函式 `int {fn}({sig})`，"
                                 + $"回傳{spec}。",
                                 $"定義 `int {fn}({sig})`，回傳"
                                 + $"{spec}。",
                             })
                         : Take(r, new[]
                             {
                                 $"Write a C++ function `int "
                                 + $"{fn}({sig})` returning {spec}.",
                                 $"Output a C++ function `int "
                                 + $"{fn}({sig})` that returns "
                                 + $"{spec}.",
                                 $"Define `int {fn}({sig})` in C++; "
                                 + $"it returns {spec}.",
                             }),
                    $"int {fn}({sig}) {{ {body} }}", "B");
            }
        }

        // -- C. unit_task — small utility functions.
        for (int i = 0; i < count / 6; i++)
        {
            var (fn, spec, body) = Take(r, UnitTasks);
            string sig = fn switch
            {
                "count_positives" => "nums",
                "last_or_default" => "lst, d",
                "repeat_str" => "s, n",
                "count_char" => "s, c",
                "clamp" => "v, lo, hi",
                "swap_pair" => "p",
                "is_vowel" => "c",
                "count_even" => "nums",
                "nth_or_zero" => "lst, n",
                "has_dup" => "lst",
                "sum_digits" => "n",
                "reverse_s" => "s",
                "pad_s" => "s, w",
                "abs_all" => "nums",
                "count_word" => "text, w",
                _ => "items",
            };
            Add(Zh() ? Take(r, new[]
                         {
                             $"寫一個 Python 函式 `{fn}({sig})` 回傳"
                             + $"{spec}。只輸出函式。",
                             $"請寫 Python 函式 `{fn}({sig})`，功能："
                             + $"回傳{spec}。",
                             $"產生 `{fn}({sig})` 的 Python 定義，"
                             + $"回傳{spec}。",
                         })
                     : Take(r, new[]
                         {
                             $"Write a Python function `{fn}({sig})` "
                             + $"returning {spec}.",
                             $"Output a Python function `{fn}({sig})` "
                             + $"that returns {spec}.",
                             $"Produce the definition of `{fn}({sig})`"
                             + $" in Python returning {spec}.",
                         }),
                $"def {fn}({sig}):\n    {body}", "C");
        }

        // -- D. fim — output only the missing body line.
        for (int i = 0; i < count / 5; i++)
        {
            if (r.Next(2) == 0)
            {
                var (fn, sig, spec, body) = Take(r, PyFuncs);
                Add((Zh() ? "補上缺少的那一行。程式碼：\n"
                          : "Fill in the missing line. Code:\n")
                    + $"```python\ndef {fn}({sig}):\n    <MISSING>\n```"
                    + "\n"
                    + (Zh() ? $"讓函式回傳{spec}。只輸出缺少的那一行。"
                            : $"Complete so the function returns "
                              + $"{spec}. Output only the missing "
                              + "line."),
                    body, "D");
            }
            else
            {
                var (fn, spec, body) = Take(r, CppFuncs);
                bool two = fn is "plus" or "minus" or "times"
                           or "bigger" or "smaller";
                string sig = two ? "int a, int b" : "int x";
                Add("Complete the missing line. Code:\n"
                    + $"```cpp\nint {fn}({sig}) {{\n    <MISSING>\n}}"
                    + $"\n```\nIt must return {spec}. Output only the"
                    + " missing line.",
                    body.TrimEnd(';').Insert(0, "    ") + ";", "D");
            }
        }

        // -- E. bug_fix — buggy snippet -> fixed line/explanation.
        var bugs = new (string bad, string askZh, string askEn,
                        string fixZh, string fixEn)[]
        {
            ("for i in range(len(xs)+1): print(xs[i])",
             "它會丟 IndexError。修正它，一行說明修法。",
             "It raises IndexError. Fix it and state the fix in one "
             + "line.",
             "把 range(len(xs)+1) 改成 range(len(xs))",
             "Change range(len(xs)+1) to range(len(xs))"),
            ("def f(x):\n    y = x * 3",
             "函式沒有回傳值。修正它。",
             "The function returns nothing. Fix it.",
             "加上 return y（回傳 y）",
             "Add return y (return y)"),
            ("if x = 5:\n    print(x)",
             "這行有語法錯誤。修正它。",
             "This line has a syntax error. Fix it.",
             "把 = 改成 ==（比較要用 ==）",
             "Change = to == (comparison needs ==)"),
            ("x = 10 / n",
             "n 可能是 0，會出錯。修正它。",
             "n may be 0 and this will crash. Fix it.",
             "if n != 0: x = 10 / n  else: x = 0（先檢查 n 不為 0）",
             "if n != 0: x = 10 / n  else: x = 0 (guard n != 0)"),
            ("int arr[3];\narr[3] = 1;",
             "C++ 這段寫錯了。哪裡錯？一行說明。",
             "This C++ snippet is wrong. What is wrong? One line.",
             "arr[3] 超出邊界（合法索引 0..2），改成 arr[2] = 1;",
             "arr[3] is out of bounds (valid 0..2); use arr[2] = 1;"),
            ("int* q = &y;\ndelete q;",
             "y 是 stack int。哪裡錯？一行說明修法。",
             "y is a stack int. What is wrong? State the fix.",
             "不能 delete stack 記憶體 — 移除 delete q;",
             "cannot delete stack memory — remove delete q;"),
            ("def avg2(a, b):\n    return a + b / 2",
             "算出的不是平均值。修正它。",
             "It does not compute the mean. Fix it.",
             "改成 return (a + b) / 2（先加再除）",
             "Change to return (a + b) / 2 (add first, then divide)"),
            ("s = int(input())\nprint(s + \"1\")",
             "int + str 會 TypeError。修正它。",
             "int + str raises TypeError. Fix it.",
             "改成 print(s + 1) 或 print(str(s) + \"1\")",
             "Change to print(s + 1) or print(str(s) + \"1\")"),
            ("while True:\n    print(\"x\")",
             "它永遠不會停。修正它，一行說明。",
             "It loops forever. Fix it in one line.",
             "加上 break（例如在印完後 break）",
             "Add a break (e.g. break after printing)"),
            ("lst = []\nlst.append(1, 2)",
             "append 只能吃一個引數。修正它。",
             "append takes one argument. Fix it.",
             "改成 lst.append(1); lst.append(2) 或 lst.extend([1,2])",
             "Use lst.append(1); lst.append(2) or "
             + "lst.extend([1,2])"),
            ("def g():\n    return x",
             "x 沒有定義。修正它，一行說明。",
             "x is not defined. Fix it in one line.",
             "把 x 當參數傳入：def g(x):",
             "Take x as a parameter: def g(x):"),
            ("int t = 5;\nif (t = 0) printf(\"zero\");",
             "C++ 這段判斷寫錯了。哪裡錯？一行說明。",
             "This C++ condition is wrong. One line.",
             "if (t = 0) 是指派不是比較 — 改成 if (t == 0)",
             "if (t = 0) assigns; use if (t == 0)"),
            ("for i in range(10)\n    print(i)",
             "少了冒號。修正它。",
             "A colon is missing. Fix it.",
             "range(10) 後面加冒號：for i in range(10):",
             "Add a colon: for i in range(10):"),
            ("d = {}\nprint(d[\"k\"])",
             "key 不存在會 KeyError。修正它，一行說明。",
             "Missing key raises KeyError. One-line fix.",
             "改用 d.get(\"k\")（或先檢查 \"k\" in d）",
             "Use d.get(\"k\") (or check \"k\" in d first)"),
            ("x = [1,2,3]\nprint(x[3])",
             "索引超出範圍。修正它。",
             "Index out of range. Fix it.",
             "最後一個索引是 2 — 改成 x[2] 或 x[-1]",
             "Last index is 2 — use x[2] or x[-1]"),
            ("float f = 1 / 2;",
             "C++ 這行得到的不是 0.5。哪裡錯？一行說明。",
             "This C++ line does not give 0.5. One line.",
             "整數除法 — 改成 1.0 / 2 或 (double)1 / 2",
             "Integer division — use 1.0 / 2 or (double)1 / 2"),
        };
        for (int i = 0; i < count / 5; i++)
        {
            var (bad, askZh, askEn, fixZh, fixEn) =
                bugs[r.Next(bugs.Length)];
            bool zh = Zh();
            string prompt = zh
                ? Take(r, new[]
                    {
                        $"Bug：`{bad}`\n{askZh}",
                        $"下面程式有問題：\n{bad}\n{askZh}",
                        $"這段程式碼出錯：`{bad}` — {askZh}",
                    })
                : Take(r, new[]
                    {
                        $"Bug: `{bad}`\n{askEn}",
                        $"Broken code:\n{bad}\n{askEn}",
                        $"This snippet is buggy: `{bad}` — {askEn}",
                    });
            Add(prompt, zh ? fixZh : fixEn, "E");
        }

        // -- F. small_multi_file — write the matching second file.
        for (int i = 0; i < count / 8; i++)
        {
            if (r.Next(2) == 0)
            {
                var (fn, spec, body) = Take(r, CppFuncs);
                bool two = fn is "plus" or "minus" or "times"
                           or "bigger" or "smaller";
                string sig = two ? "int, int" : "int";
                string full = two ? "int a, int b" : "int x";
                string h = $"{fn}_lib.h";
                string cpp = $"{fn}_lib.cpp";
                Add(Take(r, new[]
                    {
                        $"Two files: `{h}` declares `int {fn}({sig});`"
                        + $" — write the matching `{cpp}` "
                        + "implementation (include the header). The "
                        + $"function returns {spec}. Output the cpp "
                        + "content.",
                        $"`{h}` declares `int {fn}({sig});`. Write "
                        + $"`{cpp}` implementing it (#include the "
                        + $"header); it returns {spec}.",
                        $"Given header `{h}` with `int {fn}({sig});`,"
                        + $" produce `{cpp}` — include the header and"
                        + $" return {spec}.",
                    }),
                    $"#include \"{h}\"\n"
                    + $"int {fn}({full}) {{ {body} }}", "F");
            }
            else
            {
                var (fn, spec, body) = Take(r, UnitTasks);
                string f2 = Take(r, new[] { "app.py", "run2.py",
                                            "main2.py", "use_it.py" });
                Add(Take(r, new[]
                    {
                        $"helpers.py has `def {fn}` returning {spec}."
                        + $" {f2} must import it and print {fn} called"
                        + $" on a sample argument. Write {f2}.",
                        $"Given helpers.py defining `{fn}` (returns "
                        + $"{spec}), write {f2} that imports helpers "
                        + $"and prints a {fn} call.",
                        $"helpers.py defines `{fn}` returning {spec}. "
                        + $"Produce {f2} importing it and printing "
                        + $"{fn} on a sample argument.",
                    }),
                    $"import helpers\n"
                    + $"print(helpers.{fn}("
                    + (fn is "is_vowel" ? "'e'"
                        : fn is "count_char" ? "'hello', 'e'"
                        : fn is "clamp" ? "7, 0, 5"
                        : fn is "repeat_str" ? "'ab', 2"
                        : fn is "pad_s" ? "'ab', 4"
                        : fn is "last_or_default" ? "[1,2], 0"
                        : fn is "nth_or_zero" ? "[1,2], 1"
                        : fn is "swap_pair" ? "(1,2)"
                        : fn is "count_positives" ? "[1,-2,3]"
                        : fn is "count_even" ? "[1,2,4]"
                        : fn is "has_dup" ? "[1,2,2]"
                        : fn is "sum_digits" ? "123"
                        : fn is "reverse_s" ? "'ab'"
                        : fn is "abs_all" ? "[1,-2]"
                        : fn is "count_word" ? "'a b a', 'a'"
                        : "['a','b']") + "))", "F");
            }
        }
        return rows;
    }

    private static List<Dictionary<string, object?>>
        BuildCodingSuiteItems()
    {
        var items = new List<Dictionary<string, object?>>();
        void It(string id, string metric, string prompt,
                string[] patterns, int maxTok, string fail,
                string check = "regex_all")
        {
            var d = new Dictionary<string, object?>
            {
                ["id"] = id, ["category"] = metric,
                ["check"] = check, ["prompt"] = prompt,
                ["max_new_tokens"] = maxTok, ["fail_code"] = fail,
            };
            if (check == "regex_all") d["patterns"] = patterns;
            else if (check == "regex") d["pattern"] = patterns[0];
            else d["expected"] = patterns[0];
            items.Add(d);
        }
        It("cd2-syn-1", "syntax",
           "Write a one-line Python statement that assigns the "
           + "string \"ok\" to a variable named status.",
           new[] { "status", "=", "ok" }, 24, "WRONG_SYNTAX");
        It("cd2-syn-2", "syntax",
           "寫一行 C++ 宣告 double 變數 rate 初值 1.5。",
           new[] { "double", "rate", "1.5", ";" }, 24,
           "WRONG_SYNTAX");
        It("cd2-func-1", "function",
           "Write a Python function `mul4(a)` returning a * 4. "
           + "Output the function only.",
           new[] { "def mul4", "return" }, 48, "WRONG_FUNCTION");
        It("cd2-func-2", "function",
           "Write a C++ function `int Neg(int x)` returning -x.",
           new[] { "Neg", "return", "-" }, 48, "WRONG_FUNCTION");
        It("cd2-unit-1", "unit_task",
           "Write a Python function `second_or_none(lst)` returning "
           + "the second element or None for a short list.",
           new[] { "def second_or_none", "return" }, 64,
           "WRONG_LOGIC");
        It("cd2-unit-2", "unit_task",
           "寫一個函式 `min2(a,b)`（任何語言）回傳較小值。",
           new[] { "min2", "return" }, 64, "WRONG_LOGIC");
        It("cd2-fim-1", "fim",
           "Fill in the missing line. Code:\n```python\n"
           + "def cube(x):\n    <MISSING>\n```\nComplete so the "
           + "function returns x cubed. Output only the missing "
           + "line.",
           new[] { "return" }, 24, "WRONG_FILL", "contains");
        It("cd2-fim-2", "fim",
           "Complete the middle of this function:\n```python\n"
           + "def shout(s):\n    <MISSING>\n```\nIt must return s "
           + "uppercased. Output only the missing line.",
           new[] { "return.*upper|upper\\(\\)" }, 32, "WRONG_FILL",
           "regex");
        It("cd2-bug-1", "bug_fix",
           "Bug: `while i < 5: print(i)` loops forever. Fix it and "
           + "state the fix in one line.",
           new[] { "i\\s*\\+?=?\\s*(i\\s*\\+\\s*)?1|increment|遞增"
                   + "|加 ?1" },
           48, "BUG_NOT_FIXED", "regex");
        It("cd2-bug-2", "bug_fix",
           "Bug: `def area(r):\n    pi = 3.14\n    pi * r * r` "
           + "returns nothing. State the fix in one line.",
           new[] { "return" }, 48, "BUG_NOT_FIXED", "regex");
        It("cd2-multi-1", "small_multi_file",
           "Two files: `geom.h` declares `double area_circle(double);`"
           + " — write the matching `geom.cpp` implementation "
           + "(include the header). Output the cpp content.",
           new[] { "#include", "geom.h", "area_circle", "return" },
           96, "FILE_MISMATCH");
        It("cd2-multi-2", "small_multi_file",
           "tools.py has `def shout(s): return s.upper()`. run.py "
           + "must import it and print shout(\"hi\"). Write run.py.",
           new[] { "import", "shout", "print" }, 48,
           "FILE_MISMATCH");
        return items;
    }

    // ----------------------------------------------------- dataset build --

    /// <summary>Build the instruction-recovery dataset + eval suite into
    /// outDir. Deterministic under `seed`; every emitted row passed the
    /// rule gate; train/val are disjoint by normalized-content hash; the
    /// eval suite shares no prompt string with either split.</summary>
    // Replay sources: capability id -> dataset generator. Used by the
    // plan's replay_count/replay_capabilities fields to interleave
    // other capabilities' rows into the training split — single-
    // capability SFT at a real parameter slice otherwise regresses
    // neighbouring capabilities (measured: math 0.36 -> 0.09 on a 16%
    // slice at lr 2e-4, 0.27 on a 6% slice at lr 1e-4).
    private static IEnumerable<Row> GeneratorFor(string capability,
                                                 int seed, int count) =>
        capability switch
        {
            "context_tracking" => GenerateContext(seed, count),
            "multi_turn" => GenerateMultiTurn(seed, count),
            "structured_output" => GenerateStructuredOutput(seed, count),
            "tool_calling" => GenerateToolCalling(seed, count),
            "reading_grounding" => GenerateReading(seed, count),
            "rag" => GenerateRag(seed, count),
            "math" => GenerateMath(seed, count),
            "coding" => GenerateCoding(seed, count),
            "instruction_following" => Generate(seed, count),
            _ => throw new ExecutorError("RECOVERY_REPLAY_CAPABILITY",
                $"replay capability '{capability}' is not in the " +
                "supported set"),
        };

    public static Dictionary<string, object?> BuildDataset(
        string outDir, int count, int seed,
        int replayCount = 0, string[]? replayCaps = null)
    {
        Directory.CreateDirectory(outDir);
        var all = GeneratorFor(Capability, seed, count).ToList();
        var replayManifest = new Dictionary<string, int>();
        if (replayCount > 0)
        {
            var caps = replayCaps is { Length: > 0 }
                ? replayCaps
                : SupportedCapabilities
                      .Where(c => c != Capability).ToArray();
            foreach (var cap in caps)
                if (cap == Capability)
                    throw new ExecutorError("RECOVERY_REPLAY_CAPABILITY",
                        $"replay capability '{cap}' equals the active " +
                        "capability — replay rows must come from other " +
                        "capabilities");
            // Draw evenly across the replay capabilities; each
            // generator yields its full sequence so Take() picks a
            // deterministic prefix per capability.
            int per = Math.Max(1, replayCount / caps.Length);
            var rr = new Random(seed ^ 0x5f5f);
            foreach (var cap in caps)
            {
                var bucket = GeneratorFor(cap, seed + 7919, per * 4)
                    .OrderBy(_ => rr.Next()).Take(per).ToList();
                foreach (var row in bucket) row.Source = "replay:" + cap;
                replayManifest[cap] = bucket.Count;
                all.AddRange(bucket);
            }
        }
        var seen = new HashSet<string>(StringComparer.Ordinal);
        var rows = new List<Row>();
        int dropped = 0;
        var dropReasons = new Dictionary<string, int>();
        foreach (var row in all)
        {
            if (!QualityOk(row, out string reason))
            {
                dropped++;
                dropReasons[reason] =
                    dropReasons.GetValueOrDefault(reason) + 1;
                continue;
            }
            string h = TransformerTrainingRepository.Sha256Text(
                row.Prompt.Trim() + "\n" + row.Completion.Trim());
            if (!seen.Add(h)) { dropped++;
                dropReasons["duplicate"] =
                    dropReasons.GetValueOrDefault("duplicate") + 1;
                continue; }
            rows.Add(row);
        }

        // Deterministic interleave: replay rows must land throughout
        // the training stream (the trainer consumes rows in file
        // order), not appended as a trailing block.
        var shuffleRng = new Random(seed ^ 0x3c3c);
        rows = rows.OrderBy(_ => shuffleRng.Next()).ToList();

        // deterministic split — 18% held out, stratified by hashing.
        var train = new List<Row>();
        var val = new List<Row>();
        foreach (var row in rows)
        {
            string h = TransformerTrainingRepository.Sha256Text(
                "val:" + row.Prompt);
            (Convert.ToInt64(h[..8], 16) % 1000 < 180 ? val : train)
                .Add(row);
        }

        string trainPath = Path.Combine(outDir, "train.jsonl");
        string valPath = Path.Combine(outDir, "val.jsonl");
        WriteRows(trainPath, train);
        WriteRows(valPath, val);

        // Eval suite — disjoint phrasing; assert zero prompt overlap.
        var suiteItems = Capability switch
        {
            "context_tracking" => BuildContextSuiteItems(),
            "multi_turn" => BuildMultiTurnSuiteItems(),
            "structured_output" => BuildStructuredSuiteItems(),
            "tool_calling" => BuildToolCallingSuiteItems(),
            "reading_grounding" => BuildReadingSuiteItems(),
            "rag" => BuildRagSuiteItems(),
            "math" => BuildMathSuiteItems(),
            "coding" => BuildCodingSuiteItems(),
            _ => BuildSuiteItems(),
        };
        var corpusPrompts = rows
            .Select(x => x.Prompt.Trim())
            .ToHashSet(StringComparer.Ordinal);
        foreach (var it in suiteItems)
            if (corpusPrompts.Contains(
                    (string)it["prompt"]!))
                throw new ExecutorError("RECOVERY_SUITE_OVERLAP",
                    $"suite item {(string)it["id"]!} duplicates a training prompt");
        var suite = new Dictionary<string, object?>
        {
            ["format_version"] = SuiteFormat,
            ["suite_id"] = SuiteId,
            ["seed"] = seed,
            ["capability"] = Capability,
            ["notes"] = $"{Capability} recovery eval; category = " +
                        "metric so the native report's per-category " +
                        "pass_rate is the sub-metric.",
            ["items"] = suiteItems.Cast<object?>().ToList(),
        };
        string suitePath = Path.Combine(outDir, "eval-suite.json");
        ModelLifecycle.AtomicWrite(
            suitePath, CanonicalJson.PrettyDict(suite) + "\n");

        var byCat = rows.GroupBy(x => x.Category)
            .ToDictionary(g => g.Key, g => (object?)g.Count());
        var bySrc = rows.GroupBy(x => x.Source)
            .ToDictionary(g => g.Key, g => (object?)g.Count());
        int zhCount = rows.Count(x => ZhRatio(x.Prompt) > 0.3);
        var manifest = new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["format"] = DatasetFormat,
            ["capability"] = Capability,
            ["seed"] = seed,
            ["total"] = rows.Count,
            ["train"] = train.Count,
            ["val"] = val.Count,
            ["val_ratio"] = Math.Round(
                (double)val.Count / Math.Max(1, rows.Count), 4),
            ["zh_tw_rows"] = zhCount,
            ["en_rows"] = rows.Count - zhCount,
            ["replay"] = replayManifest.Count > 0
                ? replayManifest.ToDictionary(
                      kv => kv.Key, kv => (object?)kv.Value)
                : null,
            ["by_category"] = byCat,
            ["by_source"] = bySrc,
            ["dropped"] = dropped,
            ["drop_reasons"] = dropReasons,
            ["train_sha256"] =
                TransformerTrainingRepository.Sha256File(trainPath),
            ["val_sha256"] =
                TransformerTrainingRepository.Sha256File(valPath),
            ["suite_sha256"] =
                TransformerTrainingRepository.Sha256File(suitePath),
            ["dataset_sha256"] = TransformerTrainingRepository.Sha256Text(
                string.Concat(rows.Select(x =>
                    x.Prompt.Trim() + "\n" + x.Completion.Trim() + "\n"))),
            ["suite_items"] = suiteItems.Count,
            ["out_dir"] = Path.GetFullPath(outDir),
            ["built_at"] = XcPaths.IsoNow(),
        };
        ModelLifecycle.AtomicWrite(
            Path.Combine(outDir, "manifest.json"),
            CanonicalJson.PrettyDict(manifest) + "\n");
        return manifest;
    }

    private static void WriteRows(string path, List<Row> rows)
    {
        using var w = new StreamWriter(path, append: false,
                                       new System.Text.UTF8Encoding(false));
        foreach (var r in rows)
            w.WriteLine(CanonicalJson.PlainDict(r.ToDict()));
    }

    // ------------------------------------------------------------- eval --

    private static Dictionary<string, object?> ParseJsonStdout(
        NativeTools.RunResult run, string code)
    {
        string tail = run.StdoutTail.Trim();
        int start = tail.IndexOf('{');
        if (start < 0 || run.ExitCode > 2)
            throw new ExecutorError(code,
                $"tool produced no JSON (exit {run.ExitCode})");
        return (Dictionary<string, object?>)ModelLifecycle.Decode(
            JsonDocument.Parse(tail[start..]).RootElement)!;
    }

    /// <summary>Run the instruction suite on one bundle and aggregate the
    /// §20 sub-metrics + §21 failure taxonomy into an
    /// instruction_score. Returns the eval-result dict.</summary>
    public static Dictionary<string, object?> EvalBundle(
        string toolRoot, string bundle, string suitePath,
        string? outPath = null)
    {
        string stderrLog = Path.Combine(
            toolRoot, XcPaths.LogsRel,
            $"recovery-eval-stderr-{Environment.ProcessId}-{Guid.NewGuid():N}.log");
        var run = NativeTools.Run(
            NativeTools.ModelToolExe(toolRoot),
            new[] { "capability", "--bundle", bundle,
                    "--suite", suitePath, "--chat" },
            toolRoot, stderrLog, timeoutS: 7200);
        var output = ParseJsonStdout(run, "RECOVERY_EVAL_FAILED");
        var report = output.TryGetValue("report", out var rp) &&
                     rp is Dictionary<string, object?> rd
            ? rd : output;

        // id -> fail_code map from the suite itself.
        var failCode = new Dictionary<string, string>();
        try
        {
            using var doc = JsonDocument.Parse(File.ReadAllText(suitePath));
            foreach (var it in doc.RootElement.GetProperty("items")
                                   .EnumerateArray())
                failCode[it.GetProperty("id").GetString() ?? ""] =
                    it.TryGetProperty("fail_code", out var fc)
                        ? fc.GetString() ?? "" : "";
        }
        catch { /* taxonomy degrades to UNKNOWN */ }

        var cats = report.TryGetValue("categories", out var cv) &&
                   cv is Dictionary<string, object?> cd
            ? cd : new Dictionary<string, object?>();
        var metrics = new Dictionary<string, object?>();
        var taxonomy = new Dictionary<string, int>();
        double score = 0, wsum = 0, wfound = 0;
        foreach (var (metric, w) in MetricWeights)
        {
            double pr = 0;
            if (cats.TryGetValue(metric, out var mv) &&
                mv is Dictionary<string, object?> md &&
                md["pass_rate"] is double d)
            { pr = d; wfound += w; }
            metrics[metric] = pr;
            score += w * pr;
            wsum += w;
        }
        if (wsum > 0) score /= wsum;
        if (wfound <= 0)
        {
            // Suites whose categories are not the §20 metric names (e.g.
            // the reconstructed L5 parity probes) score by plain
            // pass-rate so the parity gate sees the real number.
            long it = 0, ps = 0;
            foreach (var (_, cv2) in cats)
                if (cv2 is Dictionary<string, object?> cd2)
                {
                    it += TransformerTrainingRepository.Int(
                        cd2, "items");
                    ps += TransformerTrainingRepository.Int(
                        cd2, "passed");
                }
            if (it > 0) score = (double)ps / it;
        }
        var itemsObj = report.TryGetValue("items", out var iv) &&
                       iv is Dictionary<string, object?> io
            ? io : new Dictionary<string, object?>();
        foreach (var (_, list) in itemsObj)
            if (list is List<object?> li)
                foreach (var e in li)
                    if (e is Dictionary<string, object?> ed &&
                        !TransformerTrainingRepository.Truthy(
                            ed.GetValueOrDefault("passed")) &&
                        ed.GetValueOrDefault("skipped") == null)
                    {
                        string id =
                            TransformerTrainingRepository.Str(ed, "id") ?? "";
                        string fc = failCode.GetValueOrDefault(
                            id, "IGNORED_INSTRUCTION");
                        if (fc.Length == 0) fc = "IGNORED_INSTRUCTION";
                        taxonomy[fc] = taxonomy.GetValueOrDefault(fc) + 1;
                    }

        var result = new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["format"] = EvalFormat,
            ["capability"] = Capability,
            ["bundle"] = bundle,
            ["suite_sha256"] =
                TransformerTrainingRepository.Sha256File(suitePath),
            ["capability_score"] = Math.Round(score, 6),
            ["instruction_score"] = Math.Round(score, 6),
            ["metrics"] = metrics,
            ["failure_taxonomy"] = taxonomy,
            ["categories"] = cats,
            ["eval_seconds"] = Math.Round(run.ElapsedS, 1),
            ["evaluated_at"] = XcPaths.IsoNow(),
        };
        if (outPath != null)
            ModelLifecycle.AtomicWrite(
                outPath, CanonicalJson.PrettyDict(result) + "\n");
        return result;
    }

    private static double ScoreOf(Dictionary<string, object?> eval)
        => (eval.TryGetValue("capability_score", out var v) ||
            eval.TryGetValue("instruction_score", out v)) && v != null
           ? Convert.ToDouble(v) : 0.0;

    // -------------------------------------------------------------- run --

    /// <summary>Execute the full recovery lane under a plan file. All
    /// heavy artifacts live under out_dir; only the best/final candidate
    /// is kept per the checkpoint policy.</summary>
    /// <summary>§41-§45 ParameterFreezeMap from the recovery plan:
    /// "freeze" is a bounded JSON array of wildcard pattern strings
    /// (≤64, each ≤256 chars); anything else is a typed rejection —
    /// never a silent drop.</summary>
    private static List<object?>? PlanFreeze(
        Dictionary<string, object?> plan)
    {
        if (!plan.TryGetValue("freeze", out object? raw) || raw is null)
            return null;
        if (raw is not System.Collections.IEnumerable list ||
            raw is string)
            throw new ExecutorError("RECOVERY_PLAN_MISSING",
                "freeze must be an array of pattern strings");
        var patterns = new List<object?>();
        foreach (object? item in list)
        {
            if (item is not string s || s.Length == 0 || s.Length > 256)
                throw new ExecutorError("RECOVERY_PLAN_MISSING",
                    "freeze pattern must be a non-empty string ≤256 chars");
            patterns.Add(s);
            if (patterns.Count > 64)
                throw new ExecutorError("RECOVERY_PLAN_MISSING",
                    "freeze pattern count exceeds 64");
        }
        return patterns;
    }

    public static Dictionary<string, object?> Run(
        string toolRoot, string planPath)
    {
        var plan = (Dictionary<string, object?>)ModelLifecycle.Decode(
            JsonDocument.Parse(File.ReadAllText(planPath)).RootElement)!;

        // ── governance gate ───────────────────────────────────────────
        var policy = SelfLearningPolicy.Load(toolRoot);
        // The plan declares which single capability this lane opens; the
        // freeze guard requires it to equal policy.ActiveCapability.
        string cap = TransformerTrainingRepository.Str(plan, "capability")
                     ?? throw new ExecutorError(
                         "RECOVERY_PLAN_MISSING", "capability");
        if (!SupportedCapabilities.Contains(cap))
            throw new ExecutorError("RECOVERY_PLAN_MISSING",
                $"unsupported recovery capability '{cap}'");
        Capability = cap;
        CapabilityFreeze.GuardJob("sft", cap, policy);
        string kind = TransformerTrainingRepository.Str(plan, "kind") ?? "sft";
        if (kind != "sft")
            throw new ExecutorError("CAPABILITY_TRAINING_FROZEN",
                $"recovery lane permits only sft; got '{kind}'");

        string Req(string k)
        {
            string? v = TransformerTrainingRepository.Str(plan, k);
            if (string.IsNullOrEmpty(v))
                throw new ExecutorError("RECOVERY_PLAN_MISSING", k);
            return v!;
        }

        string initCkpt = Req("init_checkpoint");
        string configFrom = Req("config_from");
        string tokenizer = Req("tokenizer");
        string sourceBundle = Req("source_bundle");
        string regressionSuite = Req("regression_suite");
        // Parity gate: when the historical 100M baseline survives only as
        // recorded probe results (retired .pt lineage cannot be evaluated
        // natively), the plan carries baseline_recorded + parity_suite —
        // the natively reconstructed same-suite probes — instead of a
        // runnable baseline bundle.
        string? paritySuite =
            TransformerTrainingRepository.Str(plan, "parity_suite");
        double baselineRecorded =
            TransformerTrainingRepository.Num(plan, "baseline_recorded");
        string? baselineBundle =
            TransformerTrainingRepository.Str(plan, "baseline_bundle");
        if (baselineBundle == null && baselineRecorded <= 0)
            throw new ExecutorError(
                "RECOVERY_PLAN_MISSING", "baseline_bundle");
        // Recovery output is xingcheng-owned data — the plan-declared
        // staging target must resolve inside the domain roots.
        string outDir = DataBoundary.AssertInside(toolRoot,
            Req("out_dir"));
        Directory.CreateDirectory(outDir);
        PlanFreeze(plan);   // validate early — fail before any work
        string stderrLog = Path.Combine(outDir, "recovery-stderr.log");

        int planThreads = TransformerTrainingRepository.Int(
            plan, "threads");
        var laneRepo = new TransformerTrainingRepository(toolRoot);

        int maxSteps = Math.Clamp(
            TransformerTrainingRepository.Int(plan, "max_steps"), 50, 600);
        int stageSteps = Math.Clamp(
            TransformerTrainingRepository.Int(plan, "stage_steps"), 10, 200);
        int evalEvery = Math.Max(stageSteps,
            TransformerTrainingRepository.Int(plan, "eval_every"));
        int regEvery = Math.Max(50,
            TransformerTrainingRepository.Int(plan, "regression_every"));
        int maxLen = TransformerTrainingRepository.Int(plan, "max_len");
        if (maxLen <= 0) maxLen = 512;
        double lr = TransformerTrainingRepository.Num(plan, "lr");
        if (!(lr > 0 && lr <= 0.1)) lr = 5e-5;
        int warmup = TransformerTrainingRepository.Int(plan, "warmup_steps");
        int seed = TransformerTrainingRepository.Int(plan, "seed");
        if (seed <= 0) seed = 42;
        var ledger = new List<object?>();
        void Led(string ev, object? data = null) =>
            ledger.Add(new Dictionary<string, object?>
            {
                ["at"] = XcPaths.IsoNow(), ["event"] = ev,
                ["data"] = data,
            });

        // ── dataset ───────────────────────────────────────────────────
        string dataDir =
            TransformerTrainingRepository.Str(plan, "dataset_dir")
            ?? Path.Combine(outDir, "dataset");
        Dictionary<string, object?> manifest;
        if (File.Exists(Path.Combine(dataDir, "manifest.json")))
        {
            manifest = (Dictionary<string, object?>)ModelLifecycle.Decode(
                JsonDocument.Parse(File.ReadAllText(
                    Path.Combine(dataDir, "manifest.json"))).RootElement)!;
        }
        else
        {
            // Replay mix (optional): plan.replay_count rows drawn from
            // plan.replay_capabilities (default: every other supported
            // capability) are interleaved into the training stream —
            // the anti-forgetting surface. Bounded: non-negative,
            // <= dataset_count, capability ids validated by
            // GeneratorFor.
            int replayCount =
                TransformerTrainingRepository.Int(plan, "replay_count")
                    is int rc && rc > 0 ? rc : 0;
            string[]? replayCaps = null;
            if (plan.TryGetValue("replay_capabilities",
                    out object? rcaps) && rcaps is not null)
            {
                if (rcaps is not System.Collections.IEnumerable capList
                    || rcaps is string)
                    throw new ExecutorError("EXECUTOR_CONFIG_INVALID",
                        "replay_capabilities must be an array of " +
                        "capability ids");
                var list = new List<string>();
                foreach (object? item in capList)
                {
                    if (item is not string rid || rid.Length == 0)
                        throw new ExecutorError("EXECUTOR_CONFIG_INVALID",
                            "replay_capabilities entries must be " +
                            "non-empty capability ids");
                    list.Add(rid);
                }
                replayCaps = list.ToArray();
            }
            manifest = BuildDataset(
                dataDir,
                TransformerTrainingRepository.Int(plan, "dataset_count")
                    is int dc && dc > 0 ? dc : 2800,
                seed, replayCount, replayCaps);
        }
        Led("dataset", new Dictionary<string, object?>
        {
            ["train"] = manifest["train"], ["val"] = manifest["val"],
            ["sha256"] = manifest["dataset_sha256"],
        });
        string suitePath = Path.Combine(dataDir, "eval-suite.json");

        // ── governed lane registration: the verified corpus registers
        // as a real training dataset so the lane-holding job row carries
        // honest lineage — the single training lane is the job table
        // itself (one row in 'training' status for the whole run).
        string laneDatasetId = RegisterLaneDataset(
            laneRepo, dataDir, cap, outDir);
        Led("lane_dataset", new Dictionary<string, object?>
        {
            ["dataset_id"] = laneDatasetId,
        });

        // ── pretokenize (all data work completes before training) ──────
        var tkTimer = System.Diagnostics.Stopwatch.StartNew();
        string trainSrc = Path.Combine(dataDir, "train.jsonl");
        string trainIds = Path.Combine(outDir, "train-ids.xcb");
        var tkOut = ParseJsonStdout(
            NativeTools.Run(
                NativeTools.ModelToolExe(toolRoot),
                new[] { "tokenize", "--tokenizer", tokenizer,
                        "--in", trainSrc, "--out", trainIds,
                        "--max-length", maxLen.ToString(
                            System.Globalization.CultureInfo.InvariantCulture),
                        "--chat" },
                toolRoot, stderrLog, timeoutS: 3600),
            "RECOVERY_TOKENIZE_FAILED");
        tkTimer.Stop();
        double datasetWaitS = tkTimer.Elapsed.TotalSeconds;
        Led("pretokenize", new Dictionary<string, object?>
        {
            ["rows_out"] = tkOut.GetValueOrDefault("rows_out"),
            ["seconds"] = Math.Round(datasetWaitS, 1),
        });

        // ── baselines (RAW layer; cached per suite+bundle) ─────────────
        double baseline100m;
        if (baselineRecorded > 0)
        {
            baseline100m = baselineRecorded;
            Led("baseline_100m_recorded", new Dictionary<string, object?>
            {
                ["score"] = baseline100m,
                ["evidence"] =
                    TransformerTrainingRepository.Str(
                        plan, "baseline_evidence"),
                ["parity_suite"] = paritySuite,
            });
        }
        else
        {
            string baselineEvalPath =
                Path.Combine(outDir, "eval-100m.json");
            var baseEval = File.Exists(baselineEvalPath)
                ? (Dictionary<string, object?>)ModelLifecycle.Decode(
                    JsonDocument.Parse(File.ReadAllText(
                        baselineEvalPath)).RootElement)!
                : EvalBundle(toolRoot, baselineBundle!, suitePath,
                             baselineEvalPath);
            baseline100m = ScoreOf(baseEval);
            Led("baseline_100m", baseline100m);
        }

        string beforePath = Path.Combine(outDir, "eval-before.json");
        var beforeEval = File.Exists(beforePath)
            ? (Dictionary<string, object?>)ModelLifecycle.Decode(
                JsonDocument.Parse(
                    File.ReadAllText(beforePath)).RootElement)!
            : EvalBundle(toolRoot, sourceBundle, suitePath, beforePath);
        double scoreBefore = ScoreOf(beforeEval);
        Led("score_before", scoreBefore);

        // Parity-suite score of the source weights — the number that is
        // actually comparable to the recorded 100M probe results.
        double scoreBeforeParity = scoreBefore;
        if (paritySuite != null)
        {
            string pbPath = Path.Combine(outDir, "eval-before-parity.json");
            var pbEval = File.Exists(pbPath)
                ? (Dictionary<string, object?>)ModelLifecycle.Decode(
                    JsonDocument.Parse(
                        File.ReadAllText(pbPath)).RootElement)!
                : EvalBundle(toolRoot, sourceBundle, paritySuite, pbPath);
            scoreBeforeParity = ScoreOf(pbEval);
            Led("parity_score_before", scoreBeforeParity);
        }

        // §32 stop condition: already at parity — do not train at all.
        // Parity against a degenerate zero baseline proves nothing about
        // instruction capability, so a measured-zero baseline does not
        // trigger the no-train short-circuit.
        bool alreadyParity = scoreBeforeParity >= baseline100m &&
                             baseline100m > 0.0;
        // plan.target_score (optional): when the governed plan declares a
        // capability-suite target beyond predecessor parity, parity
        // already-met becomes a held constraint instead of a stop — the
        // lane trains until the suite score reaches the target, or a
        // plateau/regression/parity loss stops it. Unset → the original
        // parity-only contract is unchanged.
        double targetScore =
            TransformerTrainingRepository.Num(plan, "target_score");
        bool parityOnly = targetScore <= 0.0;

        // source regression reference report for --baseline-report.
        string srcRegPath = Path.Combine(outDir, "regression-before.json");
        var srcReg = File.Exists(srcRegPath)
            ? (Dictionary<string, object?>)ModelLifecycle.Decode(
                JsonDocument.Parse(
                    File.ReadAllText(srcRegPath)).RootElement)!
            : RawCapabilityRun(toolRoot, sourceBundle, regressionSuite,
                               srcRegPath, stderrLog);
        Led("regression_source",
            srcReg.GetValueOrDefault("categories"));

        // Router-health BASELINE (moe-analyze on the source bundle):
        // standing diagnoses — e.g. ROUTER_HOTSPOT from a 9-token probe
        // on this 300M — are inherited state, not training damage. A
        // stage only collapses the lane when it introduces a diagnosis
        // the source never had, or worsens a standing layer's p99 load
        // by >25%.
        var srcRouter = ParseJsonStdout(
            NativeTools.Run(
                NativeTools.ModelToolExe(toolRoot),
                new[] { "moe-analyze", "--bundle", sourceBundle },
                toolRoot, stderrLog, timeoutS: 1800),
            "RECOVERY_ROUTER_FAILED");
        var srcDiag = new Dictionary<long, HashSet<string>>();
        var srcP99 = new Dictionary<long, double>();
        if (srcRouter.TryGetValue("analysis", out var srcAn) &&
            srcAn is Dictionary<string, object?> srcAnd &&
            srcAnd.TryGetValue("layers", out var srcLy) &&
            srcLy is List<object?> srcLayers)
            foreach (var l in srcLayers)
                if (l is Dictionary<string, object?> ld)
                {
                    long lid = TransformerTrainingRepository.Int(
                        ld, "layer_id");
                    srcDiag[lid] = new HashSet<string>(
                        ld.TryGetValue("diagnoses", out var sd) &&
                        sd is List<object?> sdl
                            ? sdl.Select(x => x?.ToString() ?? "")
                            : Enumerable.Empty<string>());
                    if (ld.TryGetValue("expert_load_quantiles",
                            out var sq) &&
                        sq is Dictionary<string, object?> sqd)
                        srcP99[lid] = TransformerTrainingRepository.Num(
                            sqd, "p99");
                }
        Led("router_baseline", new Dictionary<string, object?>
        {
            ["standing_layers"] = srcDiag
                .Where(kv => kv.Value.Count > 0)
                .Select(kv => (object?)kv.Key).ToList(),
        });

        // model cfg for the trainer job spec.
        var srcManifest = (Dictionary<string, object?>)ModelLifecycle.Decode(
            JsonDocument.Parse(File.ReadAllText(configFrom)).RootElement)!;
        var modelCfg = srcManifest["config"];

        // ── single training pipeline: this lane's staged trainer runs
        // used to spawn xingcheng_trainer directly, bypassing the
        // governed queue (serial cap, admission guards, resource
        // preflight, audit chain). Converged: the lane is a governed job
        // row claimed through TryClaimTrainingJob — the same atomic
        // mechanism every queued job uses — held in 'training' status
        // for the whole run. Admission parity: sequence guard + the
        // governor's resource preflight (thread quota, GPU/VRAM).
        using var lane = new TrainingJobExecutor(laneRepo, toolRoot)
            .AcquireTrainingLane(cap, laneDatasetId,
                new Dictionary<string, object?>
                {
                    ["device"] =
                        TransformerTrainingRepository.Str(plan, "device")
                        ?? policy.Device,
                    ["max_steps"] = maxSteps,
                },
                requestedBy: "instruction-recovery");
        int threads = lane.Threads > 0
            ? (planThreads > 0 ? Math.Min(planThreads, lane.Threads)
                               : lane.Threads)
            : Math.Min(planThreads > 0 ? planThreads : 8, 16);
        Led("lane_lease", new Dictionary<string, object?>
        {
            ["job_id"] = lane.JobId,
            ["trainer_threads"] = threads,
            ["cuda_opt_admitted"] = lane.CudaOptAdmitted,
        });

        // ── staged SFT + eval loop ──────────────────────────────────────
        string curCkpt = initCkpt;
        var stageHistory = new List<object?>();
        var noImprove = 0;
        double bestScore = scoreBefore;
        double bestParityScore = scoreBeforeParity;
        string? bestCkpt = null, bestBundleDir = null;
        double bestValLoss = double.MaxValue;
        bool bestRegOk = true;
        int step = 0;
        string decision = "NO_IMPROVEMENT";
        string stopReason = "max_steps_reached";
        var swAll = System.Diagnostics.Stopwatch.StartNew();
        double fwdS = 0, ckptS = 0, evalS = 0;
        long tokensSeen = 0;
        double? peakRss = null;
        bool trained = false;

        if (alreadyParity && parityOnly)
        {
            decision = "PASS_PARITY";
            stopReason = "already_at_parity";
            bestScore = scoreBefore;
            // the candidate IS the current weights — nothing to stage.
            bestCkpt = initCkpt;
            bestBundleDir = sourceBundle;
            Led("already_at_parity");
        }

        while (!(alreadyParity && parityOnly) && step < maxSteps)
        {
            int target = Math.Min(step + stageSteps, maxSteps);
            int runSteps = target - step;
            string stageDir = Path.Combine(
                outDir, $"stage-{target:D4}");
            Directory.CreateDirectory(stageDir);
            string emitCkpt = Path.Combine(stageDir, "stage.xcn");
            var jobSpec = new Dictionary<string, object?>
            {
                ["task"] = "sft",
                ["model"] = modelCfg,
                ["train"] = new Dictionary<string, object?>
                {
                    ["lr"] = lr,
                    ["weight_decay"] = 0.01,
                    ["grad_clip"] = 1.0,
                    ["warmup_steps"] = step == 0 ? warmup : 0,
                    ["max_steps"] = runSteps,
                    ["log_every"] = 10,
                    ["checkpoint_every"] = 0,
                    ["seed"] = seed,
                    ["deadline_s"] =
                        TransformerTrainingRepository.Num(
                            plan, "stage_deadline_s") > 0
                            ? TransformerTrainingRepository.Num(
                                plan, "stage_deadline_s") : 3600,
                    ["lr_decay"] = "cosine",
                    ["init_checkpoint"] = curCkpt,
                    ["emit_checkpoint"] = emitCkpt,
                    ["overwrite"] = true,
                    // Lane parallelism follows the governor training
                    // quota acquired with the lane lease; a plan may
                    // only pin lower (trainer clamps to 16 anyway).
                    ["threads"] = threads,
                    // §41-§45 ParameterFreezeMap: plan "freeze" is a
                    // bounded pattern list; frozen params never get
                    // Adam moments (sparse optimizer) — the
                    // extreme-low-resource lane.
                    ["freeze"] = PlanFreeze(plan),
                },
                ["data"] = new Dictionary<string, object?>
                {
                    ["path"] = trainIds,
                    ["format"] = "sft",
                    ["max_rows"] = 1000000,
                    ["max_len"] = maxLen,
                },
            };
            string jobPath = Path.Combine(stageDir, "job.json");
            File.WriteAllText(jobPath,
                CanonicalJson.PrettyDict(jobSpec) + "\n",
                new System.Text.UTF8Encoding(false));
            string repPath = Path.Combine(stageDir, "report.json");

            var trun = NativeTools.Run(
                NativeTools.TrainerExe(toolRoot),
                new[] { "--job", jobPath, "--report", repPath },
                toolRoot, stderrLog, timeoutS: 7200,
                env: lane.CudaOptAdmitted
                    ? new Dictionary<string, string>
                        { ["XINGCHENG_TRAINER_CUDA_OPT"] = "1" }
                    : null);
            if (trun.PeakRssMb.HasValue)
                peakRss = Math.Max(peakRss ?? 0, trun.PeakRssMb.Value);
            Dictionary<string, object?> trep;
            try
            {
                trep = (Dictionary<string, object?>)ModelLifecycle.Decode(
                    JsonDocument.Parse(
                        File.ReadAllText(repPath)).RootElement)!;
            }
            catch (Exception ex)
                when (ex is IOException or JsonException)
            {
                decision = "REGRESSION_REJECTED";
                stopReason = "trainer_report_unreadable";
                break;
            }
            if (trun.ExitCode != 0 ||
                !TransformerTrainingRepository.Truthy(
                    trep.GetValueOrDefault("params_finite")))
            {
                decision = "REGRESSION_REJECTED";
                stopReason = "training_numerical_failure";
                Led("numerical_failure",
                    new Dictionary<string, object?>
                    {
                        ["exit"] = trun.ExitCode,
                        ["params_finite"] =
                            trep.GetValueOrDefault("params_finite"),
                    });
                break;
            }
            trained = true;
            step = target;
            curCkpt = emitCkpt;
            tokensSeen += EstimateTokens(trainIds, runSteps, maxLen);
            fwdS += trun.ElapsedS;
            double lossLast =
                TransformerTrainingRepository.Num(trep, "loss_last");

            // export → bundle (doubles as XCN validation), reuse one dir.
            string stageBundle = Path.Combine(outDir, "stage-bundle");
            var ckTimer = System.Diagnostics.Stopwatch.StartNew();
            if (Directory.Exists(stageBundle))
                Directory.Delete(stageBundle, recursive: true);
            var exRun = NativeTools.Run(
                NativeTools.ModelToolExe(toolRoot),
                new[] { "export-bundle", "--ckpt", emitCkpt,
                        "--out", stageBundle,
                        "--config-from", configFrom,
                        "--tokenizer", tokenizer },
                toolRoot, stderrLog, timeoutS: 1800);
            ckTimer.Stop();
            ckptS += ckTimer.Elapsed.TotalSeconds;
            if (exRun.ExitCode != 0 ||
                !File.Exists(Path.Combine(stageBundle, "weights.bin")))
            {
                decision = "REGRESSION_REJECTED";
                stopReason = "xcn_validation_or_export_failed";
                break;
            }

            // mini-eval every evalEvery boundary (and at stage marks).
            var evTimer = System.Diagnostics.Stopwatch.StartNew();
            var stageEval = EvalBundle(
                toolRoot, stageBundle, suitePath,
                Path.Combine(stageDir, "eval.json"));
            evTimer.Stop();
            evalS += evTimer.Elapsed.TotalSeconds;
            double score = ScoreOf(stageEval);
            double stageParityScore = score;
            if (paritySuite != null)
            {
                var pe = EvalBundle(
                    toolRoot, stageBundle, paritySuite,
                    Path.Combine(stageDir, "eval-parity.json"));
                stageParityScore = ScoreOf(pe);
            }

            // router health — collapse stops the lane. Main's mode is
            // moe-analyze (star-moe-routing-analysis/v1): a layer with a
            // non-empty diagnoses[] is a router diagnostic.
            var router = ParseJsonStdout(
                NativeTools.Run(
                    NativeTools.ModelToolExe(toolRoot),
                    new[] { "moe-analyze", "--bundle", stageBundle },
                    toolRoot, stderrLog, timeoutS: 1800),
                "RECOVERY_ROUTER_FAILED");
            bool routerBad = false;
            var newDiags = new List<object?>();
            if (router.TryGetValue("analysis", out var an) &&
                an is Dictionary<string, object?> and_ &&
                and_.TryGetValue("layers", out var ly) &&
                ly is List<object?> layers)
                foreach (var l in layers)
                    if (l is Dictionary<string, object?> ld &&
                        ld.TryGetValue("diagnoses", out var dg) &&
                        dg is List<object?> dl && dl.Count > 0)
                    {
                        long lid = TransformerTrainingRepository.Int(
                            ld, "layer_id");
                        double p99 = -1;
                        if (ld.TryGetValue("expert_load_quantiles",
                                out var q) &&
                            q is Dictionary<string, object?> qd)
                            p99 = TransformerTrainingRepository.Num(
                                qd, "p99");
                        bool layerNew = false;
                        foreach (var d in dl)
                            if (!srcDiag.TryGetValue(lid, out var known) ||
                                !known.Contains(d?.ToString() ?? ""))
                                layerNew = true;
                        // Standing diagnosis still fails if the load
                        // skew blew up >25% over the source baseline.
                        if (!layerNew && p99 >= 0 &&
                            srcP99.TryGetValue(lid, out var sp) &&
                            sp > 0 && p99 > sp * 1.25)
                            layerNew = true;
                        if (layerNew)
                        {
                            routerBad = true;
                            newDiags.Add(new Dictionary<string, object?>
                            {
                                ["layer_id"] = lid,
                                ["diagnoses"] = dl,
                                ["p99"] = p99,
                                ["baseline_p99"] =
                                    srcP99.GetValueOrDefault(lid),
                            });
                        }
                    }

            // regression gate at each regression_every boundary.
            bool regOk = true;
            object? regDetail = null;
            if (step % regEvery == 0 || step >= maxSteps)
            {
                var reg = RawCapabilityRun(
                    toolRoot, stageBundle, regressionSuite,
                    Path.Combine(stageDir, "regression.json"),
                    stderrLog, baselineReport: srcRegPath);
                regDetail = reg.GetValueOrDefault("comparison");
                regOk = TransformerTrainingRepository.Truthy(
                    reg.GetValueOrDefault("passed"));
            }

            stageHistory.Add(new Dictionary<string, object?>
            {
                ["step"] = step,
                ["score"] = score,
                ["loss_last"] = lossLast,
                ["train_seconds"] = Math.Round(trun.ElapsedS, 1),
                ["examples_per_sec"] =
                    Math.Round(runSteps / Math.Max(0.1, trun.ElapsedS), 2),
                ["router_diagnostics"] = routerBad,
                ["regression_ok"] = regOk,
            });

            if (routerBad)
            {
                decision = "REGRESSION_REJECTED";
                stopReason = "router_collapse";
                Led("router_collapse", new Dictionary<string, object?>
                {
                    ["new_diagnoses"] = newDiags,
                    ["note"] = "standing source-bundle diagnoses are "
                        + "baselined out; collapse = new diagnosis or "
                        + ">25% p99 load blowup vs source",
                });
                break;
            }
            if (!regOk)
            {
                decision = "REGRESSION_REJECTED";
                stopReason = "capability_regression";
                Led("regression_rejected", regDetail);
                break;
            }
            if (stageParityScore >= baseline100m)
            {
                if (score > bestScore)
                    TrackBest(ref bestCkpt, ref bestBundleDir,
                              ref bestScore, ref bestValLoss,
                              ref bestRegOk, emitCkpt, stageBundle,
                              stageDir, score, lossLast, regOk, outDir);
                bestParityScore = stageParityScore;
                if (parityOnly)
                {
                    decision = "PASS_PARITY";
                    stopReason = "parity_reached";
                    Led("parity", new Dictionary<string, object?>
                    { ["step"] = step, ["score"] = score,
                      ["baseline"] = baseline100m });
                    break;
                }
                if (score >= targetScore)
                {
                    decision = "TARGET_REACHED";
                    stopReason = "target_reached";
                    Led("target", new Dictionary<string, object?>
                    { ["step"] = step, ["score"] = score,
                      ["target"] = targetScore });
                    break;
                }
                // Parity held, target not reached — keep training; the
                // plateau/regression branches below stay authoritative.
            }
            else if (!parityOnly)
            {
                // Target mode never trades the recovered parity floor
                // for capability score — a stage below baseline is a
                // regression, not a detour.
                decision = "REGRESSION_REJECTED";
                stopReason = "parity_lost";
                Led("parity_rejected", new Dictionary<string, object?>
                { ["step"] = step,
                  ["parity_score"] = stageParityScore,
                  ["baseline"] = baseline100m });
                break;
            }
            if (score > bestScore + 0.005 ||
                (score >= bestScore - 0.001 && regOk &&
                 lossLast < bestValLoss))
            {
                TrackBest(ref bestCkpt, ref bestBundleDir, ref bestScore,
                          ref bestValLoss, ref bestRegOk,
                          emitCkpt, stageBundle, stageDir, score,
                          lossLast, regOk, outDir);
                bestParityScore = Math.Max(
                    bestParityScore, stageParityScore);
                noImprove = 0;
            }
            else if (++noImprove >= 3)
            {
                decision = score > scoreBefore + 0.005
                    ? "IMPROVED_NOT_PARITY" : "NO_IMPROVEMENT";
                stopReason = "no_improvement_3x";
                break;
            }
            else if (bestCkpt == null)
            {
                TrackBest(ref bestCkpt, ref bestBundleDir, ref bestScore,
                          ref bestValLoss, ref bestRegOk,
                          emitCkpt, stageBundle, stageDir, score,
                          lossLast, regOk, outDir);
                bestParityScore = Math.Max(
                    bestParityScore, stageParityScore);
            }
        }
        swAll.Stop();

        // ── decision finalisation ────────────────────────────────────
        if (decision == "NO_IMPROVEMENT" &&
            bestScore > scoreBefore + 0.005)
            decision = "IMPROVED_NOT_PARITY";
        if (!parityOnly && trained && bestScore >= targetScore &&
            bestParityScore >= baseline100m &&
            decision != "REGRESSION_REJECTED")
            decision = "TARGET_REACHED";
        else if (bestParityScore >= baseline100m && trained &&
            decision != "REGRESSION_REJECTED")
            decision = "PASS_PARITY";

        // ── final gates on the best candidate (§27) ───────────────────
        var gates = new Dictionary<string, object?>
        {
            ["instruction_eval"] = bestBundleDir != null,
            ["baseline_comparison"] = true,
            ["xcn10_validation"] = bestCkpt != null,
        };
        object? smoke = null, prov = null, finalReg = null;
        if (bestBundleDir != null && decision != "REGRESSION_REJECTED")
        {
            var cacheSmoke = ParseJsonStdout(
                NativeTools.Run(
                    NativeTools.ModelToolExe(toolRoot),
                    new[] { "cache-smoke", "--bundle", bestBundleDir },
                    toolRoot, stderrLog, timeoutS: 1800),
                "RECOVERY_CACHE_SMOKE_FAILED");
            smoke = cacheSmoke.GetValueOrDefault("ok");
            gates["cache_smoke"] = TransformerTrainingRepository
                .Truthy(smoke);
            // statebench = the delta-state snapshot/restore contract
            // probe on the real candidate bundle.
            var stateSmoke = ParseJsonStdout(
                NativeTools.Run(
                    NativeTools.ModelToolExe(toolRoot),
                    new[] { "statebench", "--bundle", bestBundleDir,
                            "--generation",
                            BundleGeneration(bestBundleDir),
                            "--tokens", "32" },
                    toolRoot, stderrLog, timeoutS: 1800),
                "RECOVERY_STATE_SMOKE_FAILED");
            gates["state_smoke"] = TransformerTrainingRepository.Truthy(
                stateSmoke.GetValueOrDefault("ok"));
            prov = BundleProvenance.Compute(
                bestBundleDir, "gen-2-consolidated", "xc-fused-1",
                "XCN1 v10", "recovery-build", "xc-native-cpp23",
                "instruction-recovery");
            gates["provenance"] = prov != null;
            if (step % regEvery != 0)
            {
                finalReg = RawCapabilityRun(
                    toolRoot, bestBundleDir, regressionSuite,
                    Path.Combine(outDir, "regression-final.json"),
                    stderrLog, baselineReport: srcRegPath);
                gates["regression"] = TransformerTrainingRepository.Truthy(
                    ((Dictionary<string, object?>)finalReg)
                        .GetValueOrDefault("passed"));
            }
            else gates["regression"] = true;
            if (gates.Values.Any(v => v is bool b && !b))
                decision = "REGRESSION_REJECTED";
        }

        // ── checkpoint hygiene: start (= init, referenced), best, final ──
        foreach (var d in Directory.GetDirectories(outDir, "stage-*"))
        {
            string emit = Path.Combine(d, "stage.xcn");
            if (File.Exists(emit) &&
                !string.Equals(emit, bestCkpt, StringComparison.Ordinal))
            {
                try { File.Delete(emit); } catch { }
            }
        }
        string stageBundleDir = Path.Combine(outDir, "stage-bundle");
        if (Directory.Exists(stageBundleDir) &&
            !string.Equals(stageBundleDir, bestBundleDir,
                           StringComparison.Ordinal))
            try { Directory.Delete(stageBundleDir, true); } catch { }

        double scoreAfter = bestScore;
        var report = new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["format"] = ReportFormat,
            ["capability"] = Capability,
            ["mode"] = "SINGLE_CAPABILITY_RECOVERY",
            ["architecture"] = "xc-fused-1",
            ["checkpoint_version"] = "XCN1 v10",
            ["source_checkpoint"] = initCkpt,
            ["candidate_checkpoint"] = bestCkpt,
            ["candidate_bundle"] = bestBundleDir,
            ["dataset"] = new Dictionary<string, object?>
            {
                ["dir"] = dataDir,
                ["size"] = manifest["total"],
                ["train"] = manifest["train"],
                ["val"] = manifest["val"],
                ["sha256"] = manifest["dataset_sha256"],
                ["suite_sha256"] = manifest["suite_sha256"],
            },
            ["train_steps"] = step,
            ["best_step"] = bestCkpt != null
                ? (int?)BestStepOf(bestCkpt) : null,
            ["metrics"] = new Dictionary<string, object?>
            {
                ["examples_per_sec"] = fwdS > 0
                    ? Math.Round(step / fwdS, 3) : (object?)null,
                ["tokens_per_sec"] = fwdS > 0
                    ? Math.Round(tokensSeen / fwdS, 1) : (object?)null,
                ["step_time_s"] = step > 0
                    ? Math.Round(fwdS / step, 3) : (object?)null,
                ["wall_time_s"] = Math.Round(swAll.Elapsed.TotalSeconds, 1),
                ["peak_rss_mb"] = peakRss,
                ["dataset_wait_time_s"] = Math.Round(datasetWaitS, 1),
                ["forward_backward_optimizer_s"] = Math.Round(fwdS, 1),
                ["checkpoint_export_s"] = Math.Round(ckptS, 1),
                ["eval_time_s"] = Math.Round(evalS, 1),
                ["gpu_utilization"] = null,
                ["cpu_lane"] = "avx2+fma-native-trainer",
            },
            ["baseline_100m"] = baseline100m,
            ["baseline_evidence"] =
                TransformerTrainingRepository.Str(
                    plan, "baseline_evidence"),
            ["baseline_degenerate"] = baseline100m <= 0.0,
            ["parity_suite"] = paritySuite,
            ["parity_score_before"] = scoreBeforeParity,
            ["parity_score_after"] = bestParityScore,
            ["score_before"] = scoreBefore,
            ["score_after"] = scoreAfter,
            ["delta"] = Math.Round(scoreAfter - scoreBefore, 6),
            ["score_metrics_after"] =
                bestBundleDir != null
                    ? (File.Exists(Path.Combine(
                            Path.GetDirectoryName(bestCkpt!)!, "eval.json"))
                        ? (object?)Path.Combine(
                            Path.GetDirectoryName(bestCkpt!)!, "eval.json")
                        : null)
                    : null,
            ["regression_matrix"] =
                finalReg ?? stageHistory.LastOrDefault(),
            ["router_health"] = new Dictionary<string, object?>
            {
                ["probed"] = true,
                ["collapse_seen"] =
                    stageHistory.OfType<Dictionary<string, object?>>()
                        .Any(h => TransformerTrainingRepository.Truthy(
                            h.GetValueOrDefault("router_diagnostics"))),
                ["standing_layers"] = srcDiag
                    .Where(kv => kv.Value.Count > 0)
                    .Select(kv => (object?)kv.Key).ToList(),
                ["baseline"] = "source_bundle",
            },
            ["gates"] = gates,
            ["stop_reason"] = stopReason,
            ["decision"] = decision,
            ["target_score"] = parityOnly
                ? null : (object?)targetScore,
            ["stage_history"] = stageHistory,
            ["ledger"] = ledger,
            ["policy"] = new Dictionary<string, object?>
            {
                ["capability_training_mode"] =
                    policy.CapabilityTrainingMode,
                ["active_capability"] = policy.ActiveCapability,
                ["frozen_others"] = true,
            },
            ["finished_at"] = XcPaths.IsoNow(),
        };
        string reportPath = Path.Combine(
            toolRoot, XcPaths.LogsRel,
            $"single-capability-recovery-{DateTime.UtcNow:yyyyMMdd-HHmmss}.json");
        ModelLifecycle.AtomicWrite(
            reportPath, CanonicalJson.PrettyDict(report) + "\n");
        report["report_path"] = reportPath;
        lane.Complete();
        return report;
    }

    private static int BestStepOf(string ckptPath)
    {
        string marker = Path.Combine(
            Path.GetDirectoryName(ckptPath)!, "step.txt");
        try
        {
            var m = Regex.Match(File.ReadAllText(marker).Trim(),
                                @"stage-(\d+)");
            if (m.Success) return int.Parse(m.Groups[1].Value);
        }
        catch { }
        return 0;
    }

    private static void TrackBest(
        ref string? bestCkpt, ref string? bestBundleDir,
        ref double bestScore, ref double bestValLoss, ref bool bestRegOk,
        string emitCkpt, string stageBundle, string stageDir,
        double score, double lossLast, bool regOk, string outDir)
    {
        string keepDir = Path.Combine(outDir, "best");
        Directory.CreateDirectory(keepDir);
        string keepCkpt = Path.Combine(keepDir, "best.xcn");
        File.Copy(emitCkpt, keepCkpt, overwrite: true);
        string keepBundle = Path.Combine(keepDir, "bundle");
        if (Directory.Exists(keepBundle))
            Directory.Delete(keepBundle, true);
        CopyDir(stageBundle, keepBundle);
        File.Copy(Path.Combine(stageDir, "eval.json"),
                  Path.Combine(keepDir, "eval.json"), overwrite: true);
        // stage dir keeps its name — record which step produced best.
        File.WriteAllText(Path.Combine(keepDir, "step.txt"),
            Path.GetFileName(stageDir));
        bestCkpt = keepCkpt;
        bestBundleDir = keepBundle;
        bestScore = score;
        bestValLoss = lossLast;
        bestRegOk = regOk;
    }

    private static void CopyDir(string src, string dst)
    {
        Directory.CreateDirectory(dst);
        foreach (var f in Directory.GetFiles(src))
            File.Copy(f, Path.Combine(dst, Path.GetFileName(f)), true);
        foreach (var d in Directory.GetDirectories(src))
            CopyDir(d, Path.Combine(dst, Path.GetFileName(d)));
    }

    /// <summary>Resolve a bundle's lineage generation for statebench —
    /// manifest architecture_generation -> generation -> config fields.</summary>
    private static string BundleGeneration(string bundle)
    {
        try
        {
            using var doc = JsonDocument.Parse(File.ReadAllText(
                Path.Combine(bundle, "manifest.json")));
            var root = doc.RootElement;
            foreach (var k in new[] { "architecture_generation",
                                      "generation" })
                if (root.TryGetProperty(k, out var g) &&
                    g.ValueKind == JsonValueKind.String &&
                    (g.GetString() ?? "").Length > 0)
                    return g.GetString()!;
            if (root.TryGetProperty("config", out var c) &&
                c.ValueKind == JsonValueKind.Object)
                foreach (var k in new[] { "generation", "architecture" })
                    if (c.TryGetProperty(k, out var cg) &&
                        cg.ValueKind == JsonValueKind.String &&
                        (cg.GetString() ?? "").Length > 0)
                        return cg.GetString()!;
        }
        catch (Exception ex) when (ex is IOException or JsonException) { }
        return "gen-2-consolidated";
    }

    private static long EstimateTokens(string idsPath, int steps, int maxLen)
    {
        // pretokenized rows are fixed after shuffle; sample the first
        // `steps` rows for a deterministic approximation.
        long t = 0;
        int i = 0;
        foreach (var line in File.ReadLines(idsPath))
        {
            if (i++ >= steps) break;
            t += line.Count(c => c == ',');
        }
        return t;
    }

    /// <summary>Run the canonical capability suite; with baselineReport
    /// the tool's built-in per-category regression gate is enforced.</summary>
    private static Dictionary<string, object?> RawCapabilityRun(
        string toolRoot, string bundle, string suitePath,
        string outPath, string stderrLog, string? baselineReport = null)
    {
        var args = new List<string>
        {
            "capability", "--bundle", bundle, "--suite", suitePath,
            "--chat",
        };
        if (baselineReport != null)
            args.AddRange(new[] { "--baseline-report", baselineReport });
        var run = NativeTools.Run(
            NativeTools.ModelToolExe(toolRoot), args,
            toolRoot, stderrLog, timeoutS: 7200);
        var output = ParseJsonStdout(run, "RECOVERY_REGRESSION_FAILED");
        var rep = output.TryGetValue("report", out var rp) &&
                  rp is Dictionary<string, object?> rd ? rd : output;
        ModelLifecycle.AtomicWrite(
            outPath, CanonicalJson.PrettyDict(rep) + "\n");
        // --baseline-report mode reports gate outcome at top level.
        var merged = new Dictionary<string, object?>(rep);
        if (output.TryGetValue("passed", out var p)) merged["passed"] = p;
        if (output.TryGetValue("comparison", out var cmp))
            merged["comparison"] = cmp;
        return merged;
    }

    /// <summary>Register the run's verified corpus as a governed
    /// training dataset. Every row is machine-verified by a rule spec
    /// (quality 1.0); content identity mirrors the SFT dataset
    /// convention — sha256 of the sorted example-hashes array — so an
    /// identical corpus re-registers to the same row instead of
    /// duplicating. Returns the dataset_id for the lane-holding job.</summary>
    private static string RegisterLaneDataset(
        TransformerTrainingRepository repo, string dataDir,
        string capability, string outDir)
    {
        var examples = new List<Dictionary<string, object?>>();
        void AddFile(string name, string split)
        {
            int ordinal = 0;
            foreach (string line in File.ReadLines(
                         Path.Combine(dataDir, name)))
            {
                string trimmed = line.Trim();
                if (trimmed.Length == 0) continue;
                ordinal += 1;
                examples.Add(new Dictionary<string, object?>
                {
                    ["split"] = split,
                    ["owner_model_id"] = "instruction-recovery",
                    ["database_scope"] = "main",
                    ["source_example_id"] =
                        $"recovery:{capability}:{split}:{ordinal}",
                    ["source_revision"] = 1,
                    ["content_sha256"] =
                        TransformerTrainingRepository.Sha256Text(trimmed),
                    ["source_type"] = "instruction-recovery",
                    ["quality_score"] = 1.0,
                });
            }
        }
        int trainCount = 0, valCount = 0;
        AddFile("train.jsonl", "train");
        trainCount = examples.Count;
        AddFile("val.jsonl", "validation");
        valCount = examples.Count - trainCount;

        string snapshot = Path.Combine(outDir, "lane-dataset-snapshot.jsonl");
        using (var writer = new StreamWriter(snapshot, append: false,
                                       new System.Text.UTF8Encoding(false)))
        {
            foreach (string line in File.ReadLines(
                         Path.Combine(dataDir, "train.jsonl")))
                writer.WriteLine(line);
            foreach (string line in File.ReadLines(
                         Path.Combine(dataDir, "val.jsonl")))
                writer.WriteLine(line);
        }
        var sorted = examples
            .Select(e => (string)e["content_sha256"]!)
            .OrderBy(h => h, StringComparer.Ordinal).ToList();
        var sb = new System.Text.StringBuilder("[");
        for (int i = 0; i < sorted.Count; i++)
        {
            if (i > 0) sb.Append(", ");
            CanonicalJson.WriteValue(sorted[i], sb,
                                     canonical: false, depth: 0);
        }
        sb.Append(']');
        var ds = repo.CreateDataset(
            contentSha256:
                TransformerTrainingRepository.Sha256Text(sb.ToString()),
            snapshotPath: snapshot,
            snapshotSha256:
                TransformerTrainingRepository.Sha256File(snapshot),
            examples: examples,
            sourceManifest: new Dictionary<string, object?>
            {
                ["format"] = "instruction-recovery/v1",
                ["capability"] = capability,
                ["train_rows"] = trainCount,
                ["validation_rows"] = valCount,
            },
            createdBy: "instruction-recovery",
            formatVersion: "instruction-recovery/v1");
        return (string)ds["dataset_id"]!;
    }
}
