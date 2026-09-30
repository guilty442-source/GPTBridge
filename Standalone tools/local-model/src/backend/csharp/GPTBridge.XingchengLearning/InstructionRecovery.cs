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
        _ => "star-instruction-eval-result/v1",
    };
    public static string DatasetFormat => Capability switch
    {
        "context_tracking" => "star-context-recovery-dataset/v1",
        "multi_turn" => "star-multiturn-recovery-dataset/v1",
        "structured_output" =>
            "star-structured-recovery-dataset/v1",
        _ => "star-instruction-recovery-dataset/v1",
    };
    private static string SuiteId => Capability switch
    {
        "context_tracking" => "star-context-recovery-eval-20261001",
        "multi_turn" => "star-multiturn-recovery-eval-20261001",
        "structured_output" =>
            "star-structured-recovery-eval-20261001",
        _ => "star-instruction-recovery-eval-20261001",
    };

    private static readonly string[] SupportedCapabilities =
        { "instruction_following", "context_tracking", "multi_turn",
          "structured_output" };

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
    private static (string metric, double w)[] MetricWeights =>
        Capability switch
        {
            "context_tracking" => ContextMetricWeights,
            "multi_turn" => MultiTurnMetricWeights,
            "structured_output" => StructuredMetricWeights,
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

    // ----------------------------------------------------- dataset build --

    /// <summary>Build the instruction-recovery dataset + eval suite into
    /// outDir. Deterministic under `seed`; every emitted row passed the
    /// rule gate; train/val are disjoint by normalized-content hash; the
    /// eval suite shares no prompt string with either split.</summary>
    public static Dictionary<string, object?> BuildDataset(
        string outDir, int count, int seed)
    {
        Directory.CreateDirectory(outDir);
        var all = Capability switch
        {
            "context_tracking" => GenerateContext(seed, count),
            "multi_turn" => GenerateMultiTurn(seed, count),
            "structured_output" => GenerateStructuredOutput(seed, count),
            _ => Generate(seed, count),
        };
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
            toolRoot, XcPaths.LogsRel, "recovery-eval-stderr.log");
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
        string outDir = Req("out_dir");
        Directory.CreateDirectory(outDir);
        string stderrLog = Path.Combine(outDir, "recovery-stderr.log");

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
            manifest = BuildDataset(
                dataDir,
                TransformerTrainingRepository.Int(plan, "dataset_count")
                    is int dc && dc > 0 ? dc : 2800,
                seed);
        }
        Led("dataset", new Dictionary<string, object?>
        {
            ["train"] = manifest["train"], ["val"] = manifest["val"],
            ["sha256"] = manifest["dataset_sha256"],
        });
        string suitePath = Path.Combine(dataDir, "eval-suite.json");

        // ── pretokenize (all data work completes before training) ──────
        var tkTimer = System.Diagnostics.Stopwatch.StartNew();
        string trainSrc = Path.Combine(dataDir, "train.jsonl");
        string trainIds = Path.Combine(outDir, "train-ids.jsonl");
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

        if (alreadyParity)
        {
            decision = "PASS_PARITY";
            stopReason = "already_at_parity";
            bestScore = scoreBefore;
            // the candidate IS the current weights — nothing to stage.
            bestCkpt = initCkpt;
            bestBundleDir = sourceBundle;
            Led("already_at_parity");
        }

        while (!alreadyParity && step < maxSteps)
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
                toolRoot, stderrLog, timeoutS: 7200);
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
                decision = "PASS_PARITY";
                stopReason = "parity_reached";
                Led("parity", new Dictionary<string, object?>
                { ["step"] = step, ["score"] = score,
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
        if (bestParityScore >= baseline100m && trained &&
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
}
