//! ImportPage.js — 資料匯入中心 (React-free, E180/C116).
//! Client-side preview only — parses the chosen file into rows for the
//! mapping/preview/diff steps.  Actual import executes in the engine;
//! the original file is never modified here.
import { createStore, h, rerender } from "../../../../../../shared-layer/src/ui/toolWindow/dom.js";
import { dataFreshnessIndicator } from "../freshness.js";
import { badge, dataTable, emptyState, section } from "../common.js";

const arr = (v) => Array.isArray(v) ? v : [];
const TARGETS = [
	{ key: "tw", label: "國泰台股持倉" },
	{ key: "us", label: "富邦美股持倉" },
	{ key: "fund", label: "共同基金" },
	{ key: "txn", label: "交易歷史" },
	{ key: "dividend", label: "股息" },
	{ key: "distribution", label: "配息" }
];
function parsePreview(name, text) {
	if (name.endsWith(".json")) {
		try {
			const d = JSON.parse(text);
			return arr(Array.isArray(d) ? d : d.rows || d.records).slice(0, 50);
		} catch {
			return [];
		}
	}
	// CSV preview (XLSX needs the engine-side parser — preview is text-based)
	const lines = text.split(/\r?\n/).filter(Boolean);
	if (!lines.length) return [];
	const head = lines[0].split(",").map((s) => s.trim());
	return lines.slice(1, 51).map((l) => {
		const cells = l.split(",");
		return Object.fromEntries(head.map((hd, i) => [hd, cells[i]?.trim() ?? ""]));
	});
}
const STEPS = ["選擇檔案", "格式辨識", "欄位對應", "資料預覽", "錯誤檢查", "差異顯示", "使用者確認", "正式匯入"];

export function mount(ctx) {
	const local = createStore({
		step: 0, target: "tw", fileName: "", rows: [], mapping: {}, submitResult: ""
	});
	const history = ctx.query("investment_broker_imports");
	const onFile = async (f) => {
		const text = await f.text();
		const isXlsx = f.name.endsWith(".xlsx");
		local.merge({
			fileName: f.name,
			rows: isXlsx ? [] : parsePreview(f.name, text),
			step: isXlsx ? 1 : 2,
			submitResult: isXlsx ? "XLSX 需由引擎端解析器處理——此介面僅能預覽 CSV/JSON。" : local.get().submitResult
		});
	};
	const submit = () => {
		const s = local.get();
		// The governed channel: broker imports run in investment-mobile.
		// ai-assistant holds no inbound route — we submit the intent and
		// surface the honest result.
		const ack = ctx.send("investment_broker_import_submit", {
			target: s.target,
			file_name: s.fileName,
			mapping: s.mapping,
			preview_rows: s.rows.length
		});
		const result = ack.ok ? "已送出——等待引擎端結果。" : `無法送出：${ack.message}（整合未完成——匯入執行於投資引擎端）`;
		local.merge({ submitResult: result });
		ctx.notify(result, ack.ok ? "INFO" : "WARNING");
	};
	const el = h("div");
	const render = () => rerender(el, () => {
		const s = local.get();
		const columns = s.rows.length ? Object.keys(s.rows[0]) : [];
		return h("div", { className: "inv-page" },
			h("header", { className: "inv-page-head" },
				h("h2", null, "資料匯入中心"),
				h("span", { className: "inv-badge inv-badge-info" }, "支援 CSV / XLSX / JSON")),
			section({ title: "匯入流程" },
				h("ol", { className: "inv-steps" },
					STEPS.map((st, i) => h("li", {
						className: i === s.step ? "active" : i < s.step ? "done" : ""
					}, st))),
				h("div", { className: "inv-form" },
					h("label", null, "匯入目標",
						h("select", {
							dataset: { k: "import-target" },
							onChange: (e) => local.merge({ target: e.target.value })
						}, TARGETS.map((t) => h("option", { value: t.key, selected: t.key === s.target }, t.label)))),
					h("label", null, "檔案",
						h("input", {
							type: "file", accept: ".csv,.json,.xlsx",
							onChange: (e) => { const f = e.target.files?.[0]; if (f) void onFile(f); }
						}))),
				s.step >= 2 && s.rows.length > 0 ? [
					section({ title: "欄位對應" },
						h("div", { className: "inv-form" },
							columns.map((c) => h("label", null, c,
								h("input", {
									value: s.mapping[c] || c,
									dataset: { k: `map-${c}` },
									onInput: (e) => local.merge({ mapping: { ...local.get().mapping, [c]: e.target.value } })
								}))))),
					section({ title: "資料預覽（前 50 筆）" },
						dataTable({ rows: s.rows, columns: columns.map((c) => ({ key: c, label: c })), pageSize: 10 }),
						h("div", { className: "inv-actions" },
							h("button", { className: "inv-primary", onClick: () => local.merge({ step: 6 }) }, "檢查完成——前往確認")))
				] : null,
				s.step >= 6 ? section({ title: "確認匯入" },
					h("p", null, `檔案：${s.fileName}　目標：${TARGETS.find((t) => t.key === s.target)?.label}　預覽筆數：${s.rows.length}`),
					h("p", { className: "inv-note" }, "重複資料與衝突警告由引擎端檢查；原始檔案不會被改寫。"),
					h("button", { className: "inv-primary", onClick: submit }, "確認匯入")) : null,
				s.submitResult ? h("p", { className: "inv-note inv-ctrl-result" }, s.submitResult) : null),
			section({ title: "匯入歷史" },
				dataTable({
					rows: arr(history.get().data?.imports || history.get().data?.batches),
					columns: [
						{ key: "batch_id", label: "批次" },
						{ key: "source", label: "來源" },
						{ key: "target", label: "目標" },
						{ key: "rows", label: "筆數" },
						{ key: "warnings", label: "警告" },
						{ key: "status", label: "狀態", render: (r) => badge({ text: String(r.status || "—") }) },
						{ key: "at", label: "時間", render: (r) => dataFreshnessIndicator({ record: r }) },
						{
							key: "rollback", label: "",
							render: (r) => r.rollback_available
								? h("button", { onClick: () => ctx.send("investment_broker_import_rollback", { batch_id: r.batch_id }) }, "受控復原")
								: null
						}
					],
					empty: emptyState({ detail: "尚無匯入歷史。" })
				})));
	});
	const unsubs = [local.subscribe(render), history.subscribe(render)];
	render();
	return {
		el,
		destroy() {
			unsubs.forEach((u) => u());
			history.destroy();
		}
	};
}
