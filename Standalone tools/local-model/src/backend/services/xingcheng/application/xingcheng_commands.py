"""Xingcheng Command Registry.

Registers all Xingcheng commands with the enhanced command parser.
Command specifications live in the per-domain modules
``xingcheng_commands_ops`` / ``_data`` / ``_domain`` (A185 split);
this module only assembles and registers them.
"""

from __future__ import annotations

from .command_parser import (
    CommandCategory,
    CommandRegistry,
    CommandSpec,
    ParameterSpec,
    ParameterType,
    register_command,
    resolve_command,
)
from .xingcheng_commands_data import (
    create_memory_upgrade_commands,
    create_rag_commands,
    create_sql_commands,
)
from .xingcheng_commands_domain import (
    create_chat_commands,
    create_infer_command,
    create_investment_commands,
    create_tune_mobile_commands,
)
from .xingcheng_commands_ops import (
    create_codex_commands,
    create_diagnostics_commands,
    create_git_commands,
    create_maintenance_commands,
    create_platform_commands,
    create_status_commands,
)


def _create_all_commands() -> list[CommandSpec]:
    """Create all command specifications."""
    all_commands: list[CommandSpec] = []
    all_commands.extend(create_git_commands())
    all_commands.extend(create_platform_commands())
    all_commands.extend(create_rag_commands())
    all_commands.extend(create_sql_commands())
    all_commands.extend(create_diagnostics_commands())
    all_commands.extend(create_status_commands())
    all_commands.extend(create_memory_upgrade_commands())
    all_commands.extend(create_tune_mobile_commands())
    all_commands.extend(create_investment_commands())
    all_commands.extend(create_codex_commands())
    all_commands.extend(create_maintenance_commands())
    all_commands.extend(create_chat_commands())
    all_commands.extend(create_infer_command())
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
