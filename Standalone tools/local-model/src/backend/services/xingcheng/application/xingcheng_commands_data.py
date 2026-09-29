"""Xingcheng command specs — data/knowledge domain (rag/sql/memory-upgrade)."""

from __future__ import annotations

from .command_parser import CommandSpec, ParameterSpec


def create_rag_commands() -> list[CommandSpec]:
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


def create_sql_commands() -> list[CommandSpec]:
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


def create_memory_upgrade_commands() -> list[CommandSpec]:
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
