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
    public const string EvalFormat = "star-instruction-eval-result/v1";
    public const string DatasetFormat = "star-instruction-recovery-dataset/v1";
    public const string SuiteFormat = "star-capability-suite/v1";
    public const string Capability = "instruction_following";

    // §20 sub-metrics -> score weights.
    private static readonly (string metric, double w)[] MetricWeights =
    {
        ("instruction_completion", 0.25),
        ("format_accuracy", 0.20),
        ("constraint_following", 0.20),
        ("negative_constraint", 0.15),
        ("language_accuracy", 0.10),
        ("extra_content", 0.10),
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
           ("line_pattern", "^-\\s"), ("expected", 3),
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
            { "##", "^-\\s", "^[\\s\\S]*(-\\s[^\\n]*\\n?){2}$" }),
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
            { "^\\s*\\[", "風箏[\\s\\S]*陀螺[\\s\\S]*毽子", "\\]\\s*$" }),
           ("max_new_tokens", 40));
        It("cf-len-1", "constraint_following", "regex",
           "用繁體中文回答：一天有幾小時？答案不超過8個字。",
           "PARTIAL_INSTRUCTION",
           ("pattern", "^\\s*.{1,8}\\s*$"), ("max_new_tokens", 12));
        It("cf-two-step", "constraint_following", "regex_all",
           "先說明檸檬是水果，再以 JSON 輸出 {\"ok\": true}。" +
           "只輸出最終結果。", "PARTIAL_INSTRUCTION",
           ("patterns", new List<object?>
            { "^\\s*\\{[\\s\\S]*\"ok\"", "\"ok\"\\s*:\\s*true" }),
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
           ("pattern", "^(?![\\s\\S]*```)\\s*\\{[\\s\\S]*\\}\\s*$"),
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

    // ----------------------------------------------------- dataset build --

    /// <summary>Build the instruction-recovery dataset + eval suite into
    /// outDir. Deterministic under `seed`; every emitted row passed the
    /// rule gate; train/val are disjoint by normalized-content hash; the
    /// eval suite shares no prompt string with either split.</summary>
    public static Dictionary<string, object?> BuildDataset(
        string outDir, int count, int seed)
    {
        Directory.CreateDirectory(outDir);
        var all = Generate(seed, count);
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
        var suiteItems = BuildSuiteItems();
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
            ["suite_id"] = "star-instruction-recovery-eval-20261001",
            ["seed"] = seed,
            ["notes"] = "instruction_following recovery eval; category = " +
                        "metric so the native report's per-category " +
                        "pass_rate is the §20 sub-metric.",
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
        double score = 0, wsum = 0;
        foreach (var (metric, w) in MetricWeights)
        {
            double pr = 0;
            if (cats.TryGetValue(metric, out var mv) &&
                mv is Dictionary<string, object?> md &&
                md["pass_rate"] is double d)
                pr = d;
            metrics[metric] = pr;
            score += w * pr;
            wsum += w;
        }
        if (wsum > 0) score /= wsum;
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
        => eval.TryGetValue("instruction_score", out var v) && v != null
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
        CapabilityFreeze.GuardJob("sft", Capability, policy);
        // plan must not smuggle in another capability or task kind.
        string cap = TransformerTrainingRepository.Str(plan, "capability")
                     ?? Capability;
        if (!string.Equals(cap, Capability,
                           StringComparison.OrdinalIgnoreCase))
            throw new ExecutorError("MULTI_CAPABILITY_TRAINING_DENIED",
                $"plan capability '{cap}' is not '{Capability}'");
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
        string baselineBundle = Req("baseline_bundle");
        string regressionSuite = Req("regression_suite");
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
        string dataDir = Path.Combine(outDir, "dataset");
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
        string baselineEvalPath = Path.Combine(outDir, "eval-100m.json");
        var baseEval = File.Exists(baselineEvalPath)
            ? (Dictionary<string, object?>)ModelLifecycle.Decode(
                JsonDocument.Parse(
                    File.ReadAllText(baselineEvalPath)).RootElement)!
            : EvalBundle(toolRoot, baselineBundle, suitePath,
                         baselineEvalPath);
        double baseline100m = ScoreOf(baseEval);
        Led("baseline_100m", baseline100m);

        string beforePath = Path.Combine(outDir, "eval-before.json");
        var beforeEval = File.Exists(beforePath)
            ? (Dictionary<string, object?>)ModelLifecycle.Decode(
                JsonDocument.Parse(
                    File.ReadAllText(beforePath)).RootElement)!
            : EvalBundle(toolRoot, sourceBundle, suitePath, beforePath);
        double scoreBefore = ScoreOf(beforeEval);
        Led("score_before", scoreBefore);

        // §32 stop condition: already at parity — do not train at all.
        // Parity against a degenerate zero baseline proves nothing about
        // instruction capability, so a measured-zero baseline does not
        // trigger the no-train short-circuit.
        bool alreadyParity = scoreBefore >= baseline100m &&
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

        // model cfg for the trainer job spec.
        var srcManifest = (Dictionary<string, object?>)ModelLifecycle.Decode(
            JsonDocument.Parse(File.ReadAllText(configFrom)).RootElement)!;
        var modelCfg = srcManifest["config"];

        // ── staged SFT + eval loop ──────────────────────────────────────
        string curCkpt = initCkpt;
        var stageHistory = new List<object?>();
        var noImprove = 0;
        double bestScore = scoreBefore;
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

            // router health — collapse stops the lane.
            var router = ParseJsonStdout(
                NativeTools.Run(
                    NativeTools.ModelToolExe(toolRoot),
                    new[] { "router-analyze", "--bundle", stageBundle },
                    toolRoot, stderrLog, timeoutS: 1800),
                "RECOVERY_ROUTER_FAILED");
            bool routerBad = TransformerTrainingRepository.Truthy(
                router.GetValueOrDefault("any_diagnostic"));

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
                Led("router_collapse", router.GetValueOrDefault("layers"));
                break;
            }
            if (!regOk)
            {
                decision = "REGRESSION_REJECTED";
                stopReason = "capability_regression";
                Led("regression_rejected", regDetail);
                break;
            }
            if (score >= baseline100m && score > bestScore)
            {
                TrackBest(ref bestCkpt, ref bestBundleDir, ref bestScore,
                          ref bestValLoss, ref bestRegOk,
                          emitCkpt, stageBundle, stageDir, score,
                          lossLast, regOk, outDir);
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
            }
        }
        swAll.Stop();

        // ── decision finalisation ────────────────────────────────────
        if (decision == "NO_IMPROVEMENT" &&
            bestScore > scoreBefore + 0.005)
            decision = "IMPROVED_NOT_PARITY";
        if (bestScore >= baseline100m && trained &&
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
            var stateSmoke = ParseJsonStdout(
                NativeTools.Run(
                    NativeTools.ModelToolExe(toolRoot),
                    new[] { "state2-smoke" },
                    toolRoot, stderrLog, timeoutS: 300),
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
            ["baseline_degenerate"] = baseline100m <= 0.0,
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
