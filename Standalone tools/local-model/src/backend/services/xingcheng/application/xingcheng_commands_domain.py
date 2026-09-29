"""Xingcheng command specs — business/interaction domain (investment/tune-mobile/infer/chat)."""

from __future__ import annotations

from .command_parser import CommandSpec, ParameterSpec


def create_investment_commands() -> list[CommandSpec]:
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


def create_tune_mobile_commands() -> list[CommandSpec]:
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


def create_infer_command() -> list[CommandSpec]:
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
                'xingcheng_infer --prompt "寫一個 Python 函數" --model "xingcheng-native-transformer" --temperature 0.3',
            ),
        ),
    ]


def create_chat_commands() -> list[CommandSpec]:
    """Create chat (shell/MoE) command specification."""
    return [
        CommandSpec(
            name="xingcheng_chat",
            handler="_handle_chat",
            category="chat",
            description=(
                "統一對話入口（xingcheng-shell MoE：gate 路由→"
                "math/reading/coding 專家→共享 general 專家）"
            ),
            aliases=("chat", "c"),
            parameters=(
                ParameterSpec(
                    name="prompt",
                    type="string",
                    required=False,
                    description="使用者訊息",
                ),
                ParameterSpec(
                    name="message",
                    type="string",
                    required=False,
                    description="使用者訊息（與 prompt 等價）",
                ),
                ParameterSpec(
                    name="question",
                    type="string",
                    required=False,
                    description="問題（與 prompt 等價）",
                ),
                ParameterSpec(
                    name="history",
                    type="array",
                    required=False,
                    description="多輪歷史 [{role, content}]（有界 8 輪）",
                ),
            ),
            examples=(
                'xingcheng_chat --prompt "什麼是複利？"',
                'xingcheng_chat --message "3 + 5 等於多少"',
            ),
        ),
    ]
