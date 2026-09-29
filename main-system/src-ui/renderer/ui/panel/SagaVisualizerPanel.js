import { createBasePanel } from "./BasePanel.js";
import { mainSystemLocale } from "@/locales/main-system";
import { h, renderList } from "@/shared/mini/dom.js";
import "./SagaVisualizerPanel.css";
const s = mainSystemLocale.sagaVisualizer;
const tb = mainSystemLocale.toolbox;
const STATUS_COLORS = {
	PENDING: "#94a3b8",
	RUNNING: "#3b82f6",
	COMPENSATING: "#f59e0b",
	COMPLETED: "#22c55e",
	FAILED: "#ef4444",
	REQUIRES_RECONCILE: "#a855f7",
	QUARANTINED: "#6b7280"
};
const ENGINE_COLORS = {
	postgresql: "#336791",
	sqlite: "#003b57",
	qdrant: "#f59e0b",
	filesystem: "#6b7280",
	model: "#a855f7"
};
function buildNodes(op) {
	const nodes = [{
		id: `op:${op.operation_id}`,
		label: `${op.operation_type}<br/>(${op.module_id})`,
		type: "operation",
		status: op.status,
		engine: ""
	}];
	for (const step of op.nodes.filter((n) => n.type === "step")) {
		nodes.push({
			id: step.id,
			label: `${step.id}<br/>(${step.label})`,
			type: "step",
			status: step.status,
			engine: step.engine
		});
	}
	return nodes;
}
function buildEdges(op) {
	const edges = [];
	const steps = op.nodes.filter((n) => n.type === "step");
	for (const step of steps) {
		edges.push({
			from: `op:${op.operation_id}`,
			to: step.id,
			type: "depends_on",
			label: ""
		});
	}
	for (let i = 1; i < steps.length; i++) {
		edges.push({
			from: steps[i - 1].id,
			to: steps[i].id,
			type: "depends_on",
			label: ""
		});
	}
	return edges;
}
function renderMermaid(op) {
	const lines = ["```mermaid", "flowchart TD"];
	for (const [status, color] of Object.entries(STATUS_COLORS)) {
		lines.push(`    classDef ${status.toLowerCase()} fill:${color},stroke:#333,stroke-width:2px`);
	}
	for (const [engine, color] of Object.entries(ENGINE_COLORS)) {
		lines.push(`    classDef ${engine.toLowerCase()} fill:${color},stroke:#333,stroke-width:1px,color:#fff`);
	}
	for (const node of buildNodes(op)) {
		const statusClass = node.status.toLowerCase().replace("_", "-");
		const engineClass = node.engine?.toLowerCase().replace("-", "-") || "";
		const classes = [
			statusClass,
			engineClass,
			node.type
		].filter(Boolean).join(" ");
		const label = node.label.replace("\n", "<br/>");
		lines.push(`    ${node.id}["${label}"]:::${classes}`);
	}
	for (const edge of buildEdges(op)) {
		const style = edge.type === "compensation" ? "-.->" : "-->";
		const label = edge.label ? `|"${edge.label}"|` : "";
		lines.push(`    ${edge.from} ${style}${label} ${edge.to}`);
	}
	lines.push("```");
	return lines.join("\n");
}
function renderAscii(op) {
	const lines = [
		`Saga Operation: ${op.operation_id}`,
		`Type: ${op.operation_type} | Module: ${op.module_id} | Status: ${op.status}`,
		"",
		"Steps:"
	];
	const steps = op.nodes.filter((n) => n.type === "step");
	for (let i = 0; i < steps.length; i++) {
		const prefix = i === steps.length - 1 ? "└──" : "├──";
		lines.push(`  ${prefix} ${steps[i].label} [${steps[i].status}]`);
	}
	return lines.join("\n");
}
function renderTimeline(op) {
	const lines = [
		`Timeline for ${op.operation_id}`,
		`Generated: ${new Date().toISOString()}`,
		""
	];
	if (op.timeline.length === 0) {
		lines.push("  (no events)");
		return lines.join("\n");
	}
	for (const event of op.timeline) {
		const time = new Date(event.timestamp * 1e3).toISOString().slice(11, 23);
		const step = event.step_id ? ` [${event.step_id}]` : "";
		lines.push(`  ${time} ${event.event_type}${step} ${event.status}`);
	}
	return lines.join("\n");
}
function renderJson(op) {
	return JSON.stringify(op, null, 2);
}
function renderGraphviz(op) {
	const lines = ["digraph Saga {", "    rankdir=LR;"];
	for (const node of buildNodes(op)) {
		const color = STATUS_COLORS[node.status] ?? "#94a3b8";
		const shape = node.type === "operation" ? "box" : node.type === "compensation" ? "diamond" : "ellipse";
		const label = node.label.replace("<br/>", "\\n");
		lines.push(`    "${node.id}" [label="${label}", fillcolor="${color}", style="filled,rounded", shape="${shape}"];`);
	}
	for (const edge of buildEdges(op)) {
		const style = edge.type === "compensation" ? "dashed" : "solid";
		lines.push(`    "${edge.from}" -> "${edge.to}" [style="${style}"];`);
	}
	lines.push("}");
	return lines.join("\n");
}

/**
* createSagaVisualizerPanel — former SagaVisualizerPanel.jsx.
* deps: {onClose, sendCommand, waitForIpcEvent, getConnected}
* Returns {setOpen(open), destroy()}.
*/
export function createSagaVisualizerPanel({ onClose, sendCommand, waitForIpcEvent, getConnected }) {
	let operations = [];
	let selectedOpId = null;
	let format = "mermaid";
	let loading = false;
	let error = "";

	const formatSelect = h("select", {
		className: "base-panel-btn base-panel-btn--secondary",
		"aria-label": s.formatLabel,
		onChange: (e) => {
			format = e.target.value;
			update();
		}
	},
		h("option", { value: "mermaid" }, s.mermaid),
		h("option", { value: "graphviz" }, s.graphviz),
		h("option", { value: "json" }, s.json),
		h("option", { value: "ascii" }, s.ascii),
		h("option", { value: "timeline" }, s.timeline));
	const refreshBtn = h("button", {
		className: "base-panel-btn base-panel-btn--secondary",
		onClick: () => void loadOperations()
	}, s.refresh);
	const clearBtn = h("button", {
		className: "base-panel-btn base-panel-btn--secondary",
		disabled: true,
		onClick: () => {
			operations = [];
			selectedOpId = null;
			update();
		}
	}, s.clear);
	const headerActions = h("div", { className: "base-panel-actions" }, formatSelect, refreshBtn, clearBtn);

	const disconnectedEl = h("div", { className: "base-panel__disconnected", style: { display: "none" } }, tb.disconnected);
	const errorEl = h("div", { className: "base-panel__error", style: { display: "none" } });
	const opsListHost = h("div", { className: "saga-visualizer__operations-list" }, h("h4", null, s.selectOperation));
	const opsList = h("div");
	opsListHost.appendChild(opsList);
	const vizHost = h("div", { className: "saga-visualizer__visualization" });
	const toolbar = h("div", { className: "saga-visualizer__toolbar" }, opsListHost, vizHost);

	const panel = createBasePanel({
		onClose,
		title: s.title,
		eyebrow: s.subtitle,
		icon: s.icon,
		headerActions,
		getConnected,
		content: () => ({
			el: h("div", null, disconnectedEl, errorEl, toolbar),
			update,
			destroy: () => {}
		})
	});
	panel.bind(sendCommand, waitForIpcEvent);

	const loadOperations = async () => {
		if (!getConnected?.()) return;
		loading = true;
		error = "";
		update();
		try {
			const result = sendCommand("app:get-saga-operations", {});
			if (!result.ok && !result.queued) {
				throw new Error(result.message || "Failed to load operations");
			}
			const payload = await waitForIpcEvent("app:get-saga-operations_result", 1e4);
			if (payload.ok) {
				operations = payload.operations ?? [];
			} else {
				error = payload.message || "Failed to load operations";
			}
		} catch (e) {
			error = e instanceof Error ? e.message : "Unknown error";
		} finally {
			loading = false;
		}
		update();
	};

	const loadOperationDetails = async (operationId) => {
		if (!getConnected?.()) return;
		loading = true;
		error = "";
		update();
		try {
			const result = sendCommand("app:get-saga-operation", { operation_id: operationId });
			if (!result.ok && !result.queued) {
				throw new Error(result.message || "Failed to load operation");
			}
			const payload = await waitForIpcEvent("app:get-saga-operation_result", 1e4, (p) => p.operation?.operation_id === operationId || p.ok === false);
			if (payload.ok && payload.operation) {
				const operation = payload.operation;
				const existing = operations.find((o) => o.operation_id === operationId);
				if (existing) {
					operations = operations.map((o) => o.operation_id === operationId ? operation : o);
				} else {
					operations = [...operations, operation];
				}
			} else {
				error = payload.message || "Failed to load operation";
			}
		} catch (e) {
			error = e instanceof Error ? e.message : "Unknown error";
		} finally {
			loading = false;
		}
		update();
	};

	const handleSelectOperation = (opId) => {
		selectedOpId = opId;
		update();
		void loadOperationDetails(opId);
	};

	function update() {
		formatSelect.disabled = loading;
		refreshBtn.disabled = loading;
		refreshBtn.textContent = loading ? s.loading : s.refresh;
		clearBtn.disabled = operations.length === 0;
		disconnectedEl.style.display = getConnected?.() ? "none" : "";
		if (error) {
			errorEl.style.display = "";
			errorEl.textContent = error;
		} else {
			errorEl.style.display = "none";
		}
		if (operations.length === 0) {
			opsList.replaceChildren(h("p", { className: "saga-visualizer__empty" }, s.noData));
		} else {
			opsList.replaceChildren(h("ul", { className: "saga-visualizer__operations" },
				operations.map((op) => h("li", {
					className: `saga-visualizer__op-item ${selectedOpId === op.operation_id ? "selected" : ""}`,
					onClick: () => handleSelectOperation(op.operation_id)
				},
					h("div", { className: "saga-visualizer__op-header" },
						h("span", { className: "saga-visualizer__op-id" }, op.operation_id),
						h("span", { className: `saga-visualizer__status saga-visualizer__status--${op.status.toLowerCase()}` }, op.status)),
					h("div", { className: "saga-visualizer__op-meta" },
						h("span", null, op.operation_type),
						h("span", null, op.module_id))))));
		}
		const selected = selectedOpId ? operations.find((o) => o.operation_id === selectedOpId) : null;
		if (!selectedOpId) {
			vizHost.replaceChildren(h("div", { className: "saga-visualizer__empty-state" }, h("p", null, s.noSelection)));
		} else if (selected) {
			let rendered;
			switch (format) {
				case "mermaid": rendered = renderMermaid(selected); break;
				case "graphviz": rendered = renderGraphviz(selected); break;
				case "json": rendered = renderJson(selected); break;
				case "ascii": rendered = renderAscii(selected); break;
				case "timeline": rendered = renderTimeline(selected); break;
			}
			vizHost.replaceChildren(h("pre", { className: `saga-visualizer__${format}` }, rendered));
		} else {
			vizHost.replaceChildren(h("div", { className: "saga-visualizer__loading" }, s.loading));
		}
	}

	const originalSetOpen = panel.setOpen.bind(panel);
	panel.setOpen = (open) => {
		originalSetOpen(open);
		if (open) {
			update();
			void loadOperations();
		}
	};
	update();
	return panel;
}
export { createSagaVisualizerPanel as default };
