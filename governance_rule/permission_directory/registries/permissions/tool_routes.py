from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from governance_rule.permission_directory.execution.path_guard import (
    permission_denied,
)


AI_CHANNEL_ID: Final[str] = "shared-layer/ai-channel"
AUTHORIZED_TOOL_IDS: Final[frozenset[str]] = frozenset(
    {"ai-assistant", "xingcheng", "ai-collaboration", "star-chat"}
)
AI_CHANNEL_TOOL_IDS: Final[frozenset[str]] = (
    AUTHORIZED_TOOL_IDS | {"investment-mobile", "model-dialogue"}
)
# Physical nesting is not runtime hosting for these independent tools.
# model-dialogue lives below local-model on disk but owns its own lifecycle,
# actor and runtime; local-model therefore cannot be inferred as its host.
SELF_HOSTED_TOOL_EXEMPTIONS: Final[Mapping[str, str]] = {
    "model-dialogue": "model-dialogue",
}
XINGCHENG_AUTOMATIC_WORKFLOW_SEQUENCE: Final[tuple[str, ...]] = (
    "receive-original-traditional-chinese",
    "native-understand-command-and-normalize-taiwan-chinese",
    "native-analyze-code-stem-and-tool-calling-at-workflow-front",
    "extract-actions-objects-parameters-constraints",
    "classify-task-and-intensity",
    "apply-safety-and-permission-gates",
    "decompose-and-route-subtasks",
    "execute-and-collect-results",
    "cross-validate-repair-or-escalate",
    "integrate-localize-apply-verify-and-report",
)
_TOOL_ACTOR_PREFIX: Final[str] = "governance/tool/"
_GOVERNANCE_MAIN_ACTOR: Final[str] = "governance/main-system"

# Governance authorizes every route. Star holds the highest non-governance
# permission envelope but has no fixed responsibilities. Local model work is
# executed by the internal local-model-platform boundary through xingcheng's
# governed runtime endpoint; this does not assign the work to Star itself.
AI_ROUTE_COMMANDS: Final[Mapping[tuple[str, str], frozenset[str]]] = {
    ("ai-assistant", "xingcheng"): frozenset(
        {
            "xingcheng_status",
            "xingcheng_infer",
            "xingcheng_search_investments",
            "xingcheng_analyze_investments",
            "xingcheng_discuss_investment_analysis",
            "xingcheng_manage_investment_accounting",
            "xingcheng_evaluate_upgrade",
            "xingcheng_memory_list",
            "xingcheng_memory_review",
        }
    ),
    ("star-chat", "xingcheng"): frozenset(
        {
            "xingcheng_status",
            "xingcheng_infer",
            "xingcheng_codex_alignment",
            "xingcheng_codex_mirror_check",
        }
    ),
    ("model-dialogue", "xingcheng"): frozenset(
        {
            "xingcheng_status",
            "xingcheng_infer",
            "xingcheng_codex_alignment",
            "xingcheng_codex_mirror_check",
            "xingcheng_sql_get_personality",
            "xingcheng_sql_save_personality",
        }
    ),
    ("xingcheng", "ai-collaboration"): frozenset(
        {"ai_nexus_send_message"}
    ),
    ("xingcheng", "ai-assistant"): frozenset(
        {
            "investment_mobile_get_snapshot",
            "investment_mobile_submit_instruction",
        }
    ),
}


def tool_actor(tool_id: str) -> str:
    return f"{_TOOL_ACTOR_PREFIX}{tool_id}"


def declared_host_tool_id(tool_id: str, fallback_host: str = "") -> str:
    """Resolve the permission-directory host declaration.

    A registered self-host exemption wins over physical-folder inference.
    Unknown tools retain the caller-supplied registered host and never gain
    an exemption implicitly.
    """

    value = str(tool_id or "").strip()
    return SELF_HOSTED_TOOL_EXEMPTIONS.get(value, str(fallback_host or "").strip())


def _actor_tool_id(actor: str) -> str:
    value = str(actor or "").strip()
    if not value.startswith(_TOOL_ACTOR_PREFIX):
        raise permission_denied()
    tool_id = value[len(_TOOL_ACTOR_PREFIX) :]
    if tool_id not in AI_CHANNEL_TOOL_IDS:
        raise permission_denied()
    return tool_id


def authorize_ai_route(requester_actor: str, target_tool_id: str, command: str) -> str:
    caller_tool_id = _actor_tool_id(requester_actor)
    target = str(target_tool_id or "").strip()
    requested_command = str(command or "").strip()
    if target not in AUTHORIZED_TOOL_IDS:
        raise permission_denied()
    if requested_command not in AI_ROUTE_COMMANDS.get(
        (caller_tool_id, target), frozenset()
    ):
        raise permission_denied()
    return caller_tool_id


def authorize_ai_target(requester_actor: str, target_tool_id: str, command: str) -> None:
    """Authorize an own-tool command or a governed cross-tool AI route.

    AI-channel participants that are not route targets (e.g.
    investment-mobile, a submit-only 星澄 client) accept governance and
    self commands — their governed runtime executes own-tool requests —
    but never a cross-tool route into themselves.
    """

    target = str(target_tool_id or "").strip()
    requested_command = str(command or "").strip()
    actor = str(requester_actor or "").strip()
    if target not in AI_CHANNEL_TOOL_IDS or not requested_command:
        raise permission_denied()
    # Governance remains the highest authority and may manage an AI tool via
    # the shared request layer. It isn't accepted by authorize_ai_route(), so
    # it cannot impersonate an AI participant or create a peer-to-peer route.
    if actor == _GOVERNANCE_MAIN_ACTOR:
        return
    if actor == tool_actor(target):
        return
    # Submit-only participants are never a cross-tool route target.
    if target not in AUTHORIZED_TOOL_IDS:
        raise permission_denied()
    authorize_ai_route(actor, target, requested_command)


def authorize_xingcheng_automatic_workflow(
    requester_actor: str,
    target_tool_id: str,
    command: str,
    payload: Mapping[str, Any],
) -> None:
    """Authorize and validate the fixed local-model workflow envelope."""

    authorize_ai_target(requester_actor, target_tool_id, command)
    if (
        str(target_tool_id or "").strip() != "xingcheng"
        or str(command or "").strip() != "xingcheng_infer"
        or payload.get("automatic_workflow") is not True
        or payload.get("autonomous_agent") is not True
        or tuple(payload.get("workflow_sequence") or ())
        != XINGCHENG_AUTOMATIC_WORKFLOW_SEQUENCE
        or str(payload.get("primary_language") or "").strip() != "zh-TW"
    ):
        raise permission_denied()


def ai_channel_status() -> dict[str, Any]:
    return {
        "ok": True,
        "channel_id": AI_CHANNEL_ID,
        "transport": "governance-authenticated-shared-layer",
        "participants": sorted(AI_CHANNEL_TOOL_IDS),
        "self_hosted_tool_exemptions": dict(SELF_HOSTED_TOOL_EXEMPTIONS),
        "mobile_participant_scope": "submit-to-xingcheng-only",
        "authority_order": [
            "governance-rule",
            "xingcheng",
            "ai-assistant",
            "ai-collaboration",
            "star-chat",
        ],
        "channel_top_level_tool": "xingcheng",
        "highest_authority": "governance-rule",
        "authorization_owner": "governance-rule",
        "highest_non_governance_permission_holder": "xingcheng",
        "highest_authority_management_required": True,
        "star_has_fixed_responsibilities": True,
        "star_fixed_responsibilities": [
            "sql-central-management",
            "rag-central-management",
            "git-central-management",
        ],
        "star_permission_activation": "governance-rule-explicit-authorization-only",
        "star_governance_direct_connection": "read-only-authority-snapshot",
        "star_governance_source_of_truth": True,
        "star_governance_source_priority": "highest",
        "local_model_execution_owner": "local-model-platform",
        "local_model_runtime_endpoint": "xingcheng",
        "star_local_model_task_participation": False,
        "xingcheng_automatic_workflow": {
            "required": True,
            "sequence": list(XINGCHENG_AUTOMATIC_WORKFLOW_SEQUENCE),
            "primary_language": "zh-TW",
            "project_scope": "E:/GPTBridge",
            "excluded_path_roots": ["governance_rule"],
        },
        "routes": [
            {
                "source": source,
                "target": target,
                "commands": sorted(commands),
            }
            for (source, target), commands in sorted(AI_ROUTE_COMMANDS.items())
        ],
        "investment_manager_external_ai": False,
        "external_ai_response_recipient": "xingcheng",
        "queue_when_offline": False,
        "database_shared": False,
    }


MOBILE_TOOL_ID: Final[str] = "investment-mobile"
STAR_TOOL_ID: Final[str] = "xingcheng"
MOBILE_ACTOR: Final[str] = "governance/tool/investment-mobile"
MOBILE_ROUTE_COMMAND: Final[frozenset[str]] = frozenset(
    {
        "xingcheng_mobile_get_investment_snapshot",
        "xingcheng_mobile_submit_investment_instruction",
    }
)


def authorize_investment_mobile_route(
    requester_actor: str, target_tool_id: str, command: str
) -> None:
    if (
        str(requester_actor or "").strip() != MOBILE_ACTOR
        or str(target_tool_id or "").strip() != STAR_TOOL_ID
        or str(command or "").strip() not in MOBILE_ROUTE_COMMAND
    ):
        raise permission_denied()


def authorize_investment_mobile_target(
    requester_actor: str, target_tool_id: str, command: str
) -> None:
    authorize_investment_mobile_route(requester_actor, target_tool_id, command)

def authorize_tool_self_route(
    requester_actor: str, target_tool_id: str, command: str
) -> None:
    """Authorize a governed tool's self-submit on its own channel.

    Mode-B (native) tool hosts queue WS-accepted commands to themselves
    through the transport proxy; the only legitimate route for that
    binding is the tool's own actor targeting its own tool_id.  This is
    intentionally narrower than authorize_ai_target(): governance and
    peer routes never flow through a tool's self-submit binding.
    """
    target = str(target_tool_id or "").strip()
    actor = str(requester_actor or "").strip()
    if not target or not str(command or "").strip():
        raise permission_denied()
    if actor != tool_actor(target):
        raise permission_denied()
