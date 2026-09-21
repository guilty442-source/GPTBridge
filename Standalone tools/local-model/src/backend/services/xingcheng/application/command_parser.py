"""Enhanced Command Parser for Xingcheng.

Provides a robust command parsing system with:
- Command registry with metadata
- Alias support
- Parameter validation
- Fuzzy matching
- Help/documentation generation
- Command aliases and groups
"""

from __future__ import annotations

import difflib
import inspect
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

from typing_extensions import Self


class CommandCategory(Enum):
    """Command categories for organization."""
    GIT = "git"
    PLATFORM = "platform"
    RAG = "rag"
    SQL = "sql"
    DIAGNOSTICS = "diagnostics"
    STATUS = "status"
    UPGRADE_MEMORY = "upgrade_memory"
    INFER = "infer"
    CODEX = "codex"
    SYSTEM = "system"


class ParameterType(Enum):
    """Parameter types for validation."""
    STRING = "string"
    INTEGER = "integer"
    BOOLEAN = "boolean"
    NUMBER = "number"
    ARRAY = "array"
    OBJECT = "object"
    ENUM = "enum"


@dataclass(frozen=True)
class ParameterSpec:
    """Parameter specification for validation."""
    name: str
    type: ParameterType = ParameterType.STRING
    required: bool = False
    default: Any = None
    description: str = ""
    enum_values: tuple[str, ...] = ()
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    pattern: Optional[str] = None
    min_length: Optional[int] = None
    max_length: Optional[int] = None


@dataclass
class CommandSpec:
    """Command specification with metadata."""
    name: str
    handler: Callable
    category: CommandCategory = CommandCategory.SYSTEM
    description: str = ""
    aliases: tuple[str, ...] = ()
    parameters: tuple[ParameterSpec, ...] = ()
    examples: tuple[str, ...] = ()
    deprecated: bool = False
    hidden: bool = False
    permission_required: str = ""

    def __post_init__(self) -> None:
        # Callers may declare the category by its wire value ("git",
        # "platform", ...); normalize to the enum so the registry index and
        # listing stay consistent.
        if not isinstance(self.category, CommandCategory):
            try:
                self.category = CommandCategory(str(self.category))
            except ValueError:
                self.category = CommandCategory.SYSTEM

    def matches(self, command: str) -> bool:
        """Check if command matches this spec (name or alias)."""
        return command == self.name or command in self.aliases

    def get_all_names(self) -> tuple[str, ...]:
        """Get all names (name + aliases)."""
        return (self.name,) + self.aliases


class CommandParserError(Exception):
    """Command parser error with details."""

    def __init__(
        self,
        message: str,
        command: str = "",
        suggestions: tuple[str, ...] = (),
        code: str = "COMMAND_ERROR"
    ):
        super().__init__(message)
        self.message = message
        self.command = command
        self.suggestions = suggestions
        self.code = code


class ParameterValidationError(CommandParserError):
    """Parameter validation error."""

    def __init__(
        self,
        param_name: str,
        expected: str,
        received: Any,
        command: str = ""
    ):
        message = f"參數 '{param_name}' 驗證失敗: 預期 {expected}, 收到 {type(received).__name__}"
        super().__init__(message, command=command, code="PARAM_VALIDATION_ERROR")
        self.param_name = param_name
        self.expected = expected
        self.received = received


class CommandRegistry:
    """Command registry with enhanced parsing capabilities."""

    def __init__(self) -> None:
        self._commands: dict[str, CommandSpec] = {}
        self._alias_map: dict[str, str] = {}  # alias -> command_name
        self._category_index: dict[CommandCategory, set[str]] = {
            cat: set() for cat in CommandCategory
        }

    def register(self, spec: CommandSpec) -> Self:
        """Register a command specification."""
        if spec.name in self._commands:
            raise ValueError(f"命令已存在: {spec.name}")

        self._commands[spec.name] = spec
        self._category_index[spec.category].add(spec.name)

        for alias in spec.aliases:
            if alias in self._alias_map:
                raise ValueError(f"別名已存在: {alias}")
            self._alias_map[alias] = spec.name

        return self

    def unregister(self, name: str) -> bool:
        """Unregister a command."""
        spec = self._commands.pop(name, None)
        if spec is None:
            return False

        self._category_index[spec.category].discard(name)
        for alias in spec.aliases:
            self._alias_map.pop(alias, None)

        return True

    def get(self, name: str) -> Optional[CommandSpec]:
        """Get command spec by name or alias."""
        # Try exact name
        if name in self._commands:
            return self._commands[name]

        # Try alias
        if name in self._alias_map:
            return self._commands[self._alias_map[name]]

        return None

    def resolve(self, command: str) -> Optional[CommandSpec]:
        """Resolve command with fuzzy matching."""
        # Exact match
        spec = self.get(command)
        if spec:
            return spec

        # Fuzzy match
        suggestions = self._fuzzy_match(command)
        if len(suggestions) == 1:
            return self._commands[suggestions[0]]

        return None

    def _fuzzy_match(self, command: str, threshold: float = 0.6) -> list[str]:
        """Find fuzzy matches for command."""
        all_names = list(self._commands.keys()) + list(self._alias_map.keys())
        matches = difflib.get_close_matches(command, all_names, n=5, cutoff=threshold)
        return matches

    def get_suggestions(self, command: str) -> tuple[str, ...]:
        """Get command suggestions for a failed command."""
        matches = self._fuzzy_match(command)
        return tuple(matches)

    def list_commands(
        self,
        category: Optional[CommandCategory] = None,
        include_hidden: bool = False,
        include_deprecated: bool = False
    ) -> list[CommandSpec]:
        """List all registered commands."""
        commands = []
        for spec in self._commands.values():
            if spec.hidden and not include_hidden:
                continue
            if spec.deprecated and not include_deprecated:
                continue
            if category and spec.category != category:
                continue
            commands.append(spec)

        return sorted(commands, key=lambda s: (s.category.value, s.name))

    def get_categories(self) -> list[CommandCategory]:
        """Get categories with registered commands."""
        return [
            cat for cat, cmds in self._category_index.items()
            if cmds
        ]


# Global registry instance
_command_registry: Optional[CommandRegistry] = None


def get_command_registry() -> CommandRegistry:
    """Get global command registry."""
    global _command_registry
    if _command_registry is None:
        _command_registry = CommandRegistry()
    return _command_registry


def register_command(spec: CommandSpec) -> None:
    """Register a command in the global registry."""
    get_command_registry().register(spec)


def get_command(name: str) -> Optional[CommandSpec]:
    """Get command spec from global registry."""
    return get_command_registry().get(name)


def resolve_command(command: str) -> Optional[CommandSpec]:
    """Resolve command with fuzzy matching."""
    return get_command_registry().resolve(command)


def list_commands(
    category: Optional[CommandCategory] = None,
    include_hidden: bool = False,
    include_deprecated: bool = False
) -> list[CommandSpec]:
    """List all registered commands."""
    return get_command_registry().list_commands(
        category=category,
        include_hidden=include_hidden,
        include_deprecated=include_deprecated
    )


def get_command_suggestions(command: str) -> tuple[str, ...]:
    """Get command suggestions."""
    return get_command_registry().get_suggestions(command)


# Parameter validation
def validate_parameters(
    params: dict[str, Any],
    spec: CommandSpec
) -> dict[str, Any]:
    """Validate and coerce parameters according to spec."""
    validated = {}
    errors = []

    # Check required parameters
    for param_spec in spec.parameters:
        param_name = param_spec.name

        if param_name not in params:
            if param_spec.required:
                errors.append(f"缺少必要參數: {param_name}")
            elif param_spec.default is not None:
                validated[param_name] = param_spec.default
            continue

        value = params[param_name]

        # Type validation and coercion
        try:
            validated[param_name] = _coerce_and_validate(
                value, param_spec
            )
        except ParameterValidationError as e:
            errors.append(str(e))

    # Undeclared keys pass through untouched: governed IPC payloads carry
    # routing/envelope fields (runtime_model, interaction_mode, _*_ flags)
    # that are not part of a command's declared parameter surface.
    known_params = {p.name for p in spec.parameters}
    for key in params:
        if key not in known_params:
            validated[key] = params[key]

    if errors:
        raise CommandParserError(
            f"參數驗證失敗: {'; '.join(errors)}",
            code="PARAM_VALIDATION_ERROR"
        )

    return validated


def _coerce_and_validate(value: Any, spec: ParameterSpec) -> Any:
    """Coerce and validate a single parameter value."""
    # None handling
    if value is None:
        if spec.required:
            raise ParameterValidationError(
                spec.name, "required", None
            )
        return spec.default

    # Type coercion
    if spec.type == ParameterType.STRING:
        value = str(value)
        if spec.min_length is not None and len(value) < spec.min_length:
            raise ParameterValidationError(
                spec.name, f"最小長度 {spec.min_length}", value
            )
        if spec.max_length is not None and len(value) > spec.max_length:
            raise ParameterValidationError(
                spec.name, f"最大長度 {spec.max_length}", value
            )
        if spec.pattern and not re.match(spec.pattern, value):
            raise ParameterValidationError(
                spec.name, f"符合模式 {spec.pattern}", value
            )
        if spec.enum_values and value not in spec.enum_values:
            raise ParameterValidationError(
                spec.name, f"必須是以下之一: {spec.enum_values}", value
            )

    elif spec.type == ParameterType.INTEGER:
        try:
            value = int(value)
        except (ValueError, TypeError):
            raise ParameterValidationError(
                spec.name, "integer", value
            )
        if spec.min_value is not None and value < spec.min_value:
            raise ParameterValidationError(
                spec.name, f"最小值 {spec.min_value}", value
            )
        if spec.max_value is not None and value > spec.max_value:
            raise ParameterValidationError(
                spec.name, f"最大值 {spec.max_value}", value
            )

    elif spec.type == ParameterType.NUMBER:
        try:
            value = float(value)
        except (ValueError, TypeError):
            raise ParameterValidationError(
                spec.name, "number", value
            )
        if spec.min_value is not None and value < spec.min_value:
            raise ParameterValidationError(
                spec.name, f"最小值 {spec.min_value}", value
            )
        if spec.max_value is not None and value > spec.max_value:
            raise ParameterValidationError(
                spec.name, f"最大值 {spec.max_value}", value
            )

    elif spec.type == ParameterType.BOOLEAN:
        if isinstance(value, str):
            value = value.lower() in ("true", "1", "yes", "on")
        else:
            value = bool(value)

    elif spec.type == ParameterType.ARRAY:
        if not isinstance(value, (list, tuple)):
            raise ParameterValidationError(
                spec.name, "array", value
            )
        value = list(value)

    elif spec.type == ParameterType.OBJECT:
        if not isinstance(value, dict):
            raise ParameterValidationError(
                spec.name, "object", value
            )

    elif spec.type == ParameterType.ENUM:
        if value not in spec.enum_values:
            raise ParameterValidationError(
                spec.name, f"必須是以下之一: {spec.enum_values}", value
            )

    return value


# Help generation
def generate_help_text(
    registry: Optional[CommandRegistry] = None,
    category: Optional[CommandCategory] = None
) -> str:
    """Generate help text for commands."""
    registry = registry or get_command_registry()
    commands = registry.list_commands(category=category)

    if not commands:
        return "無可用命令"

    lines = ["可用命令:", ""]

    current_category = None
    for cmd in commands:
        if cmd.category != current_category:
            current_category = cmd.category
            lines.append(f"\n【{cmd.category.value.upper()}】")

        names = cmd.get_all_names()
        name_str = ", ".join(names)

        line = f"  {name_str}"
        if cmd.description:
            line += f" - {cmd.description}"
        if cmd.aliases:
            line += f" (別名: {', '.join(cmd.aliases)})"
        if cmd.deprecated:
            line += " [已棄用]"

        lines.append(line)

        # Show parameters
        if cmd.parameters:
            for param in cmd.parameters:
                req = " (必要)" if param.required else " (可選)"
                default = f" = {param.default}" if param.default is not None else ""
                lines.append(f"    {param.name}: {param.type.value}{req}{default}")
                if param.description:
                    lines.append(f"      {param.description}")

    return "\n".join(lines)


def generate_command_help(command: str) -> str:
    """Generate detailed help for a specific command."""
    registry = get_command_registry()
    spec = registry.get(command)

    if not spec:
        suggestions = registry.get_suggestions(command)
        if suggestions:
            return f"未知命令: {command}\n\n您可能想找: {', '.join(suggestions)}"
        return f"未知命令: {command}"

    lines = [
        f"命令: {spec.name}",
        f"描述: {spec.description}" if spec.description else "",
        f"分類: {spec.category.value}",
        "",
    ]

    if spec.aliases:
        lines.append(f"別名: {', '.join(spec.aliases)}")

    if spec.examples:
        lines.append("\n範例:")
        for ex in spec.examples:
            lines.append(f"  {ex}")

    if spec.parameters:
        lines.append("\n參數:")
        for param in spec.parameters:
            req = " (必要)" if param.required else " (可選)"
            default = f" = {param.default}" if param.default is not None else ""
            lines.append(
                f"  {param.name}: {param.type.value}{req}{default}"
            )
            if param.description:
                lines.append(f"    {param.description}")
            if param.enum_values:
                lines.append(f"    可選值: {', '.join(param.enum_values)}")
            if param.pattern:
                lines.append(f"    模式: {param.pattern}")

    if spec.deprecated:
        lines.append("\n⚠️ 此命令已棄用")

    return "\n".join(filter(None, lines))


__all__ = [
    "CommandCategory",
    "ParameterType",
    "ParameterSpec",
    "CommandSpec",
    "CommandParserError",
    "ParameterValidationError",
    "CommandRegistry",
    "get_command_registry",
    "register_command",
    "get_command",
    "resolve_command",
    "list_commands",
    "get_command_suggestions",
    "ParameterSpec",
    "CommandSpec",
    "validate_parameters",
    "generate_help_text",
    "generate_command_help",
    "ParameterValidationError",
    "CommandParserError",
]
