"""Xingcheng command specs — operations domain (git/platform/status/diagnostics/codex/maintenance)."""

from __future__ import annotations

from .command_parser import CommandSpec, ParameterSpec


def create_git_commands() -> list[CommandSpec]:
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


def create_platform_commands() -> list[CommandSpec]:
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


def create_status_commands() -> list[CommandSpec]:
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


def create_diagnostics_commands() -> list[CommandSpec]:
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


def create_codex_commands() -> list[CommandSpec]:
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


# ``create_teaching_commands`` retired (B167/B38): xingcheng_submit_teaching
# had no live training consumer; the channel is closed.


def create_maintenance_commands() -> list[CommandSpec]:
    """Create retention/search command specifications.

    ``xingcheng_self_learning_cycle`` retired (B167/B38): JAX/XLA and
    Python training are retired with no transitional period.
    """
    return [
        CommandSpec(
            name="xingcheng_retention_sweep",
            handler="_handle_retention_sweep",
            category="maintenance",
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
                "經受管 searchd (Go metasearch) loopback 路徑執行一次 Web 搜尋"
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
