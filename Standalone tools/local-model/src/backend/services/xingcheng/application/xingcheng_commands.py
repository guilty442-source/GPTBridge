"""Xingcheng Command Registry.

Registers all Xingcheng commands with the enhanced command parser.
This module defines all commands with their specifications, parameters,
and handlers.
"""

from __future__ import annotations

from typing import Any

from .command_parser import (
    CommandCategory,
    CommandRegistry,
    CommandSpec,
    ParameterSpec,
    ParameterType,
    register_command,
    resolve_command,
)


def _create_git_commands() -> list[CommandSpec]:
    """Create Git command specifications."""
    return [
        CommandSpec(
            name="xingcheng_git_status",
            handler="_handle_git",
            category="git",
            description="顯示 Git 倉庫狀態",
            aliases=("git_status", "gs"),
            parameters=(),
            examples=(
                "xingcheng_git_status",
                "xingcheng_git_status --detailed",
            ),
        ),
        CommandSpec(
            name="xingcheng_git_history",
            handler="_handle_git",
            category="git",
            description="顯示 Git 提交歷史",
            aliases=("git_log", "gh"),
            parameters=(
                ParameterSpec(
                    name="limit",
                    type="integer",
                    required=False,
                    default=20,
                    description="顯示筆數限制",
                ),
            ),
            examples=(
                "xingcheng_git_history",
                "xingcheng_git_history --limit 50",
            ),
        ),
        CommandSpec(
            name="xingcheng_git_stage",
            handler="_handle_git",
            category="git",
            description="暫存檔案到 Git 暫存區",
            aliases=("git_add", "ga"),
            parameters=(
                ParameterSpec(
                    name="paths",
                    type="array",
                    required=True,
                    description="要暫存的檔案路徑列表",
                ),
                ParameterSpec(
                    name="confirmed",
                    type="boolean",
                    required=False,
                    default=False,
                    description="確認操作",
                ),
            ),
            examples=(
                "xingcheng_git_stage --paths '[\"src/main.py\"]'",
                "xingcheng_git_stage --paths '[\"*.py\"]' --confirmed true",
            ),
        ),
        CommandSpec(
            name="xingcheng_git_commit",
            handler="_handle_git",
            category="git",
            description="提交 Git 變更",
            aliases=("git_commit", "gc"),
            parameters=(
                ParameterSpec(
                    name="message",
                    type="string",
                    required=True,
                    description="提交訊息",
                ),
                ParameterSpec(
                    name="confirmed",
                    type="boolean",
                    required=False,
                    default=False,
                    description="確認提交",
                ),
                ParameterSpec(
                    name="amend",
                    type="boolean",
                    required=False,
                    default=False,
                    description="修正上一次提交",
                ),
            ),
            examples=(
                'xingcheng_git_commit --message "feat: 新增功能"',
                'xingcheng_git_commit --message "fix: 修復錯誤" --amend true',
            ),
        ),
    ]


def _create_platform_commands() -> list:
    """Create platform command specifications."""
    return [
        CommandSpec(
            name="xingcheng_platform_status",
            handler="_handle_platform",
            category="platform",
            description="顯示平台狀態",
            aliases=("platform_status", "ps"),
            parameters=(),
            examples=("xingcheng_platform_status",),
        ),
    ]


def _create_rag_commands() -> list:
    """Create RAG command specifications."""
    return [
        CommandSpec(
            name="xingcheng_rag_status",
            handler="_handle_rag",
            category="rag",
            description="顯示 RAG 服務狀態",
            aliases=("rag_status",),
            parameters=(),
            examples=("xingcheng_rag_status",),
        ),
        CommandSpec(
            name="xingcheng_rag_ingest",
            handler="_handle_rag",
            category="rag",
            description="導入文件到 RAG 知識庫",
            aliases=("rag_ingest",),
            parameters=(
                ParameterSpec(
                    name="documents",
                    type="array",
                    required=True,
                    description="要導入的文件列表",
                ),
                ParameterSpec(
                    name="metadata",
                    type="object",
                    required=False,
                    description="文件元數據",
                ),
            ),
            examples=(
                'xingcheng_rag_ingest --documents \'[{"content": "內容", "metadata": {"source": "web"}}\']',
            ),
        ),
        CommandSpec(
            name="xingcheng_rag_query",
            handler="_handle_rag",
            category="rag",
            description="查詢 RAG 知識庫",
            aliases=("rag_query", "rq"),
            parameters=(
                ParameterSpec(
                    name="query",
                    type="string",
                    required=True,
                    description="查詢字串",
                ),
                ParameterSpec(
                    name="limit",
                    type="integer",
                    required=False,
                    default=8,
                    description="返回結果數量限制",
                ),
            ),
            examples=(
                'xingcheng_rag_query --query "如何優化資料庫" --limit 5',
            ),
        ),
        CommandSpec(
            name="xingcheng_knowledge_unified_search",
            handler="_handle_rag",
            category="rag",
            description="統一知識搜尋",
            aliases=("knowledge_search", "ks"),
            parameters=(
                ParameterSpec(
                    name="query",
                    type="string",
                    required=True,
                    description="搜尋查詢",
                ),
                ParameterSpec(
                    name="limit",
                    type="integer",
                    required=False,
                    default=8,
                    description="返回結果數量限制",
                ),
            ),
            examples=(
                'xingcheng_knowledge_unified_search --query "投資策略"',
            ),
        ),
    ]


def _create_sql_commands() -> list:
    """Create SQL command specifications."""
    return [
        CommandSpec(
            name="xingcheng_sql_status",
            handler="_handle_sql",
            category="sql",
            description="顯示 SQL 服務狀態",
            aliases=("sql_status",),
            parameters=(),
            examples=("xingcheng_sql_status",),
        ),
        CommandSpec(
            name="xingcheng_sql_get_personality",
            handler="_handle_sql",
            category="sql",
            description="獲取 SQL 人格設定",
            aliases=("sql_get_personality",),
            parameters=(),
            examples=("xingcheng_sql_get_personality",),
        ),
        CommandSpec(
            name="xingcheng_sql_save_personality",
            handler="_handle_sql",
            category="sql",
            description="保存 SQL 人格設定",
            aliases=("sql_save_personality",),
            parameters=(
                ParameterSpec(
                    name="personality",
                    type="object",
                    required=False,
                    description="人格設定對象",
                ),
                ParameterSpec(
                    name="value",
                    type="object",
                    required=False,
                    description="人格設定值",
                ),
                ParameterSpec(
                    name="confirmed",
                    type="boolean",
                    required=False,
                    default=False,
                    description="確認操作",
                ),
            ),
            examples=(
                'xingcheng_sql_save_personality --personality \'{"tone": "professional"}\' --confirmed true',
            ),
        ),
        CommandSpec(
            name="xingcheng_sql_list_knowledge",
            handler="_handle_sql",
            category="sql",
            description="列出 SQL 知識",
            aliases=("sql_list_knowledge",),
            parameters=(
                ParameterSpec(
                    name="knowledge_type",
                    type="string",
                    required=False,
                    default="",
                    description="知識類型過濾",
                ),
                ParameterSpec(
                    name="limit",
                    type="integer",
                    required=False,
                    default=100,
                    description="返回筆數限制",
                ),
            ),
            examples=(
                "xingcheng_sql_list_knowledge --knowledge_type investment --limit 50",
            ),
        ),
        CommandSpec(
            name="xingcheng_sql_save_knowledge",
            handler="_handle_sql",
            category="sql",
            description="保存 SQL 知識",
            aliases=("sql_save_knowledge",),
            parameters=(
                ParameterSpec(
                    name="knowledge",
                    type="object",
                    required=True,
                    description="知識內容",
                ),
                ParameterSpec(
                    name="confirmed",
                    type="boolean",
                    required=False,
                    default=False,
                    description="確認操作",
                ),
            ),
            examples=(
                'xingcheng_sql_save_knowledge --knowledge \'{"type": "investment", "content": "..."}\' --confirmed true',
            ),
        ),
    ]


def _create_diagnostics_commands() -> list:
    """Create diagnostics command specifications."""
    return [
        CommandSpec(
            name="xingcheng_diagnose_fault",
            handler="_handle_diagnostics",
            category="diagnostics",
            description="故障診斷",
            aliases=("diagnose", "df"),
            parameters=(
                ParameterSpec(
                    name="symptom",
                    type="string",
                    required=False,
                    description="症狀描述",
                ),
                ParameterSpec(
                    name="question",
                    type="string",
                    required=False,
                    description="問題描述",
                ),
                ParameterSpec(
                    name="prompt",
                    type="string",
                    required=False,
                    description="提示詞",
                ),
                ParameterSpec(
                    name="instruction",
                    type="string",
                    required=False,
                    description="指令",
                ),
                ParameterSpec(
                    name="external_research",
                    type="boolean",
                    required=False,
                    default=False,
                    description="是否進行外部研究",
                ),
            ),
            examples=(
                'xingcheng_diagnose_fault --symptom "模型推理速度過慢"',
                'xingcheng_diagnose_fault --question "為什麼模型回覆錯誤" --external_research true',
            ),
        ),
    ]


def _create_status_commands() -> list:
    """Create status command specifications."""
    return [
        CommandSpec(
            name="xingcheng_status",
            handler="_handle_status",
            category="status",
            description="顯示星澄狀態",
            aliases=("status", "s"),
            parameters=(
                ParameterSpec(
                    name="prepare_mode",
                    type="string",
                    required=False,
                    default="",
                    enum_values=("chat", "coding"),
                    description="準備模式: chat 或 coding",
                ),
            ),
            examples=(
                "xingcheng_status",
                "xingcheng_status --prepare_mode coding",
            ),
        ),
    ]


def _create_memory_upgrade_commands() -> list:
    """Create memory/upgrade command specifications."""
    return [
        CommandSpec(
            name="xingcheng_evaluate_upgrade",
            handler="_handle_upgrade_memory",
            category="upgrade_memory",
            description="評估模型升級",
            aliases=("evaluate_upgrade", "eu"),
            parameters=(),
            examples=("xingcheng_evaluate_upgrade",),
        ),
        CommandSpec(
            name="xingcheng_memory_list",
            handler="_handle_upgrade_memory",
            category="upgrade_memory",
            description="列出記憶",
            aliases=("memory_list", "ml"),
            parameters=(
                ParameterSpec(
                    name="include_inactive",
                    type="boolean",
                    required=False,
                    default=False,
                    description="包含非活躍記憶",
                ),
                ParameterSpec(
                    name="limit",
                    type="integer",
                    required=False,
                    default=100,
                    description="返回筆數限制",
                ),
            ),
            examples=(
                "xingcheng_memory_list",
                "xingcheng_memory_list --include_inactive true --limit 50",
            ),
        ),
        CommandSpec(
            name="xingcheng_memory_review",
            handler="_handle_upgrade_memory",
            category="upgrade_memory",
            description="審核記憶",
            aliases=("memory_review", "mr"),
            parameters=(
                ParameterSpec(
                    name="memory_id",
                    type="string",
                    required=True,
                    description="記憶 ID",
                ),
                ParameterSpec(
                    name="action",
                    type="string",
                    required=True,
                    description="動作: approve/reject",
                    enum_values=("approve", "reject"),
                ),
                ParameterSpec(
                    name="reviewer",
                    type="string",
                    required=False,
                    default="star-owner",
                    description="審核者",
                ),
                ParameterSpec(
                    name="reason",
                    type="string",
                    required=False,
                    default="",
                    description="審核理由",
                ),
            ),
            examples=(
                'xingcheng_memory_review --memory_id "mem_123" --action approve --reason "內容正確"',
            ),
        ),
    ]


def _create_tune_mobile_commands() -> list:
    """Create mobile tune command specifications."""
    return [
        CommandSpec(
            name="xingcheng_tune_investment_parameters",
            handler="_handle_tune_mobile",
            category="tune_mobile",
            description="調整投資參數",
            aliases=("tune_params",),
            parameters=(
                ParameterSpec(
                    name="parameters",
                    type="object",
                    required=True,
                    description="要調整的參數",
                ),
            ),
            examples=(
                'xingcheng_tune_investment_parameters --parameters \'{"risk_level": "high"}\'',
            ),
        ),
        CommandSpec(
            name="xingcheng_mobile_get_investment_snapshot",
            handler="_handle_tune_mobile",
            category="tune_mobile",
            description="獲取投資快照",
            aliases=("mobile_snapshot",),
            parameters=(),
            examples=("xingcheng_mobile_get_investment_snapshot",),
        ),
        CommandSpec(
            name="xingcheng_mobile_submit_investment_instruction",
            handler="_handle_tune_mobile",
            category="tune_mobile",
            description="提交投資指令",
            aliases=("mobile_invest",),
            parameters=(
                ParameterSpec(
                    name="instruction",
                    type="string",
                    required=True,
                    description="投資指令",
                ),
            ),
            examples=(
                'xingcheng_mobile_submit_investment_instruction --instruction "買入 AAPL 10 股"',
            ),
        ),
    ]


def _create_investment_commands() -> list:
    """Create investment command specifications."""
    return [
        CommandSpec(
            name="xingcheng_search_investments",
            handler="_handle_investments",
            category="investment",
            description="搜尋投資",
            aliases=("search_investments", "si"),
            parameters=(
                ParameterSpec(
                    name="query",
                    type="string",
                    required=True,
                    description="搜尋查詢",
                ),
                ParameterSpec(
                    name="limit",
                    type="integer",
                    required=False,
                    default=10,
                    description="返回筆數限制",
                ),
            ),
            examples=(
                'xingcheng_search_investments --query "台積電" --limit 5',
            ),
        ),
        CommandSpec(
            name="xingcheng_analyze_investments",
            handler="_handle_investments",
            category="investment",
            description="分析投資",
            aliases=("analyze_investments", "ai"),
            parameters=(
                ParameterSpec(
                    name="symbols",
                    type="array",
                    required=True,
                    description="股票代碼列表",
                ),
            ),
            examples=(
                'xingcheng_analyze_investments --symbols \'["2330.TW", "AAPL"]\'',
            ),
        ),
        CommandSpec(
            name="xingcheng_manage_investment_accounting",
            handler="_handle_investments",
            category="investment",
            description="管理投資會計",
            aliases=("manage_accounting",),
            parameters=(
                ParameterSpec(
                    name="action",
                    type="string",
                    required=True,
                    enum_values=("record", "reconcile", "report"),
                    description="動作類型",
                ),
            ),
            examples=(
                'xingcheng_manage_investment_accounting --action reconcile',
            ),
        ),
        CommandSpec(
            name="xingcheng_manage_investment_accounting",
            handler="_handle_investments",
            category="investment",
            description="管理投資會計",
            aliases=("manage_accounting",),
            parameters=(
                ParameterSpec(
                    name="action",
                    type="string",
                    required=True,
                    enum_values=("record", "reconcile", "report"),
                    description="動作類型",
                ),
            ),
            examples=(
                'xingcheng_manage_investment_accounting --action reconcile',
            ),
        ),
    ]


def _create_tune_mobile_commands() -> list:
    """Create tune mobile command specifications."""
    return [
        CommandSpec(
            name="xingcheng_tune_investment_parameters",
            handler="_handle_tune_mobile",
            category="tune_mobile",
            description="調整投資參數",
            aliases=("tune_params",),
            parameters=(
                ParameterSpec(
                    name="parameters",
                    type="object",
                    required=True,
                    description="要調整的參數",
                ),
            ),
            examples=(
                'xingcheng_tune_investment_parameters --parameters \'{"risk_level": "high"}\'',
            ),
        ),
        CommandSpec(
            name="xingcheng_mobile_get_investment_snapshot",
            handler="_handle_tune_mobile",
            category="tune_mobile",
            description="獲取投資快照",
            aliases=("mobile_snapshot",),
            parameters=(),
            examples=("xingcheng_mobile_get_investment_snapshot",),
        ),
        CommandSpec(
            name="xingcheng_mobile_submit_investment_instruction",
            handler="_handle_tune_mobile",
            category="tune_mobile",
            description="提交投資指令",
            aliases=("mobile_invest",),
            parameters=(
                ParameterSpec(
                    name="instruction",
                    type="string",
                    required=True,
                    description="投資指令",
                ),
            ),
            examples=(
                'xingcheng_mobile_submit_investment_instruction --instruction "買入 AAPL 10 股"',
            ),
        ),
    ]


def _create_investment_commands() -> list:
    """Create investment command specifications."""
    return [
        CommandSpec(
            name="xingcheng_search_investments",
            handler="_handle_investments",
            category="investment",
            description="搜尋投資",
            aliases=("search_investments", "si"),
            parameters=(
                ParameterSpec(
                    name="query",
                    type="string",
                    required=True,
                    description="搜尋查詢",
                ),
                ParameterSpec(
                    name="limit",
                    type="integer",
                    required=False,
                    default=10,
                    description="返回筆數限制",
                ),
            ),
            examples=(
                'xingcheng_search_investments --query "台積電" --limit 5',
            ),
        ),
        CommandSpec(
            name="xingcheng_analyze_investments",
            handler="_handle_investments",
            category="investment",
            description="分析投資",
            aliases=("analyze_investments", "ai"),
            parameters=(
                ParameterSpec(
                    name="symbols",
                    type="array",
                    required=True,
                    description="股票代碼列表",
                ),
            ),
            examples=(
                'xingcheng_analyze_investments --symbols \'["2330.TW", "AAPL"]\'',
            ),
        ),
        CommandSpec(
            name="xingcheng_manage_investment_accounting",
            handler="_handle_investments",
            category="investment",
            description="管理投資會計",
            aliases=("manage_accounting",),
            parameters=(
                ParameterSpec(
                    name="action",
                    type="string",
                    required=True,
                    enum_values=("record", "reconcile", "report"),
                    description="動作類型",
                ),
            ),
            examples=(
                'xingcheng_manage_investment_accounting --action reconcile',
            ),
        ),
    ]


def _create_diagnostics_commands() -> list:
    """Create diagnostics command specifications."""
    return [
        CommandSpec(
            name="xingcheng_diagnose_fault",
            handler="_handle_diagnostics",
            category="diagnostics",
            description="故障診斷",
            aliases=("diagnose", "df"),
            parameters=(
                ParameterSpec(
                    name="symptom",
                    type="string",
                    required=False,
                    description="症狀描述",
                ),
                ParameterSpec(
                    name="question",
                    type="string",
                    required=False,
                    description="問題描述",
                ),
                ParameterSpec(
                    name="prompt",
                    type="string",
                    required=False,
                    description="提示詞",
                ),
                ParameterSpec(
                    name="instruction",
                    type="string",
                    required=False,
                    description="指令",
                ),
                ParameterSpec(
                    name="external_research",
                    type="boolean",
                    required=False,
                    default=False,
                    description="是否進行外部研究",
                ),
            ),
            examples=(
                'xingcheng_diagnose_fault --symptom "模型推理速度過慢"',
                'xingcheng_diagnose_fault --question "為什麼模型回覆錯誤" --external_research true',
            ),
        ),
    ]


def _create_codex_commands() -> list:
    """Create codex diagnostics command specifications."""
    return [
        CommandSpec(
            name="xingcheng_codex_alignment",
            handler="_handle_codex_diagnostics",
            category="codex",
            description="法典對齊檢查",
            aliases=("codex_alignment", "ca"),
            parameters=(),
            examples=("xingcheng_codex_alignment",),
        ),
        CommandSpec(
            name="xingcheng_codex_mirror_check",
            handler="_handle_codex_diagnostics",
            category="codex",
            description="法典鏡像檢查",
            aliases=("codex_mirror_check", "cmc"),
            parameters=(),
            examples=("xingcheng_codex_mirror_check",),
        ),
    ]


def _create_teaching_commands() -> list:
    """Create teaching command specifications."""
    return [
        CommandSpec(
            name="xingcheng_submit_teaching",
            handler="_handle_teaching",
            category="teaching",
            description="提交教學範例",
            aliases=("submit_teaching", "st"),
            parameters=(
                ParameterSpec(
                    name="example",
                    type="object",
                    required=True,
                    description="教學範例內容",
                ),
            ),
            examples=(
                'xingcheng_submit_teaching --example \'{"input": "...", "output": "..."}\'',
            ),
        ),
    ]


def _create_self_learning_commands() -> list:
    """Create self-learning command specifications."""
    return [
        CommandSpec(
            name="xingcheng_self_learning_cycle",
            handler="_handle_self_learning",
            category="self_learning",
            description=(
                "執行一輪自我學習循環（main-system 排程經 system channel 觸發）"
            ),
            aliases=(),
            parameters=(),
            examples=("xingcheng_self_learning_cycle",),
        ),
        CommandSpec(
            name="xingcheng_retention_sweep",
            handler="_handle_retention_sweep",
            category="self_learning",
            description=(
                "執行一輪資料保留清理（main-system retention flow 經 "
                "system channel 觸發；§10.67）"
            ),
            aliases=(),
            parameters=(),
            examples=("xingcheng_retention_sweep",),
        ),
        CommandSpec(
            name="xingcheng_web_search",
            handler="_handle_web_search",
            category="search",
            description=(
                "經受管 SearXNG loopback 路徑執行一次 Web 搜尋"
                "（A177 資訊層治理通道；五核心稽核的外部證據檢查使用）"
            ),
            aliases=(),
            parameters=(
                ParameterSpec(
                    name="query",
                    type="string",
                    required=True,
                    description="搜尋查詢",
                ),
                ParameterSpec(
                    name="max_results",
                    type="integer",
                    required=False,
                    default=5,
                    min_value=1,
                    max_value=10,
                    description="最大結果數",
                ),
            ),
            examples=('xingcheng_web_search --query "codex amendment"',),
        ),
    ]


def _create_infer_command() -> list:
    """Create infer command specification."""
    return [
        CommandSpec(
            name="xingcheng_infer",
            handler="_handle_infer",
            category="infer",
            description="本地模型推理",
            aliases=("infer", "inf"),
            parameters=(
                ParameterSpec(
                    name="prompt",
                    type="string",
                    required=False,
                    description="提示詞",
                ),
                ParameterSpec(
                    name="instruction",
                    type="string",
                    required=False,
                    description="提示詞（與 prompt 等價）",
                ),
                ParameterSpec(
                    name="model",
                    type="string",
                    required=False,
                    description="指定模型",
                ),
                ParameterSpec(
                    name="temperature",
                    type="number",
                    required=False,
                    default=0.7,
                    min_value=0.0,
                    max_value=2.0,
                    description="溫度參數",
                ),
                ParameterSpec(
                    name="max_tokens",
                    type="integer",
                    required=False,
                    default=2048,
                    description="最大生成 token 數",
                ),
                ParameterSpec(
                    name="stream",
                    type="boolean",
                    required=False,
                    default=False,
                    description="是否流式輸出",
                ),
            ),
            examples=(
                'xingcheng_infer --prompt "解釋量子計算" --temperature 0.7',
                'xingcheng_infer --prompt "寫一個 Python 函數" --model "qwen3.5:9b-q4_K_M" --temperature 0.3',
            ),
        ),
    ]


def _create_all_commands() -> list:
    """Create all command specifications."""
    all_commands = []
    all_commands.extend(_create_git_commands())
    all_commands.extend(_create_platform_commands())
    all_commands.extend(_create_rag_commands())
    all_commands.extend(_create_sql_commands())
    all_commands.extend(_create_diagnostics_commands())
    all_commands.extend(_create_status_commands())
    all_commands.extend(_create_memory_upgrade_commands())
    all_commands.extend(_create_tune_mobile_commands())
    all_commands.extend(_create_investment_commands())
    all_commands.extend(_create_diagnostics_commands())
    all_commands.extend(_create_codex_commands())
    all_commands.extend(_create_teaching_commands())
    all_commands.extend(_create_self_learning_commands())
    all_commands.extend(_create_infer_command())
    return all_commands


def register_all_commands(registry=None) -> CommandRegistry:
    """Register all Xingcheng commands with the registry."""
    from .command_parser import CommandRegistry, get_command_registry, register_command

    registry = registry or CommandRegistry()

    # Duplicate command names may exist while the command catalog is being
    # consolidated; the first definition wins instead of crashing the whole
    # registry (and with it every service call).
    for cmd_spec in _create_all_commands():
        try:
            register_command(cmd_spec)
        except ValueError:
            continue

    return get_command_registry()


def get_xingcheng_registry() -> CommandRegistry:
    """Get or create Xingcheng command registry."""
    from .command_parser import get_command_registry
    registry = get_command_registry()

    # Check if already initialized
    if not registry._commands:
        register_all_commands()

    return registry


__all__ = [
    "register_all_commands",
    "get_xingcheng_registry",
    "CommandSpec",
    "ParameterSpec",
    "ParameterType",
    "CommandCategory",
    "resolve_command",
]
