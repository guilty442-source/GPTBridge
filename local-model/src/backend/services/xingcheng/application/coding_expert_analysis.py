from __future__ import annotations

import ast
import hashlib
import json
import re
from typing import Any


class CodingExpertAnalysisMixin:
    """Static analysis, validation, and refactoring methods for StarCodingExpert."""

    @staticmethod
    def _call_name(node: ast.Call) -> str:
        parts: list[str] = []
        current: ast.AST = node.func
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            parts.append(current.id)
        return ".".join(reversed(parts))

    @classmethod
    def _python_analysis(cls, source: str) -> dict[str, Any]:
        try:
            tree = ast.parse(source)
        except SyntaxError as error:
            return {
                "node_count": 0,
                "functions": [],
                "classes": [],
                "imports": [],
                "complexity": 0,
                "security_findings": [],
                "syntax_errors": [f"line {error.lineno}: {error.msg}"],
            }
        imports: list[str] = []
        aliases: dict[str, str] = {}
        findings: list[dict[str, Any]] = []
        complexity_nodes = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.BoolOp, ast.Match)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(alias.name)
                    aliases[alias.asname or alias.name.split(".")[0]] = alias.name
            elif isinstance(node, ast.ImportFrom):
                module = str(node.module or "")
                imports.append(module)
                for alias in node.names:
                    aliases[alias.asname or alias.name] = (
                        f"{module}.{alias.name}" if module else alias.name
                    )
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                call_name = cls._call_name(node)
                root, separator, remainder = call_name.partition(".")
                resolved_call = (
                    f"{aliases[root]}.{remainder}"
                    if separator and root in aliases
                    else aliases.get(call_name, call_name)
                )
                prohibited = resolved_call in cls.PROHIBITED_CALLS
                if call_name in {"getattr", "builtins.getattr"} and len(node.args) >= 2:
                    attribute = node.args[1]
                    prohibited = prohibited or bool(
                        isinstance(attribute, ast.Constant)
                        and isinstance(attribute.value, str)
                        and attribute.value in cls.PROHIBITED_DYNAMIC_CALL_NAMES
                    )
                if resolved_call in {"open", "builtins.open"}:
                    mode_node = node.args[1] if len(node.args) >= 2 else None
                    for keyword in node.keywords:
                        if keyword.arg == "mode":
                            mode_node = keyword.value
                    mode = (
                        str(mode_node.value)
                        if isinstance(mode_node, ast.Constant)
                        and isinstance(mode_node.value, str)
                        else "r"
                    )
                    prohibited = prohibited or any(flag in mode for flag in "wax+")
                if prohibited:
                    findings.append(
                        {
                            "severity": "high",
                            "code": "DANGEROUS_CALL",
                            "symbol": resolved_call or call_name or "dynamic-call",
                            "line": node.lineno,
                        }
                    )
        for imported in imports:
            if imported.split(".")[0] in cls.PROHIBITED_IMPORTS:
                findings.append(
                    {"severity": "high", "code": "PROHIBITED_IMPORT", "symbol": imported, "line": 0}
                )
        return {
            "node_count": sum(1 for _ in ast.walk(tree)),
            "functions": [
                node.name
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            ],
            "classes": [node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)],
            "imports": list(dict.fromkeys(imports)),
            "complexity": 1 + sum(isinstance(node, complexity_nodes) for node in ast.walk(tree)),
            "security_findings": findings,
            "syntax_errors": [],
        }

    @staticmethod
    def _balanced_delimiters(source: str) -> list[str]:
        pairs = {")": "(", "]": "[", "}": "{"}
        stack: list[tuple[str, int]] = []
        quote = ""
        escaped = False
        index = 0
        while index < len(source):
            character = source[index]
            following = source[index + 1] if index + 1 < len(source) else ""
            if quote:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == quote:
                    quote = ""
                index += 1
                continue
            if character in {"'", '"', "`"}:
                quote = character
            elif character == "/" and following == "/":
                newline = source.find("\n", index + 2)
                index = len(source) if newline < 0 else newline
                continue
            elif character == "/" and following == "*":
                ending = source.find("*/", index + 2)
                if ending < 0:
                    return ["unterminated block comment"]
                index = ending + 2
                continue
            elif character in "([{":
                stack.append((character, index))
            elif character in pairs:
                if not stack or stack[-1][0] != pairs[character]:
                    return [f"unmatched {character} at character {index}"]
                stack.pop()
            index += 1
        if quote:
            return ["unterminated string literal"]
        return [f"unclosed {character} at character {position}" for character, position in stack]

    @classmethod
    def _script_analysis(cls, source: str) -> dict[str, Any]:
        findings = [
            {
                "severity": "high",
                "code": "DANGEROUS_SCRIPT_PATTERN",
                "symbol": pattern,
                "line": source[: match.start()].count("\n") + 1,
            }
            for pattern in cls.PROHIBITED_SCRIPT_PATTERNS
            for match in [re.search(pattern, source, flags=re.IGNORECASE)]
            if match is not None
        ]
        return {
            "node_count": 0,
            "functions": re.findall(r"\bfunction\s+([A-Za-z_$][\w$]*)", source),
            "classes": re.findall(r"\bclass\s+([A-Za-z_$][\w$]*)", source),
            "imports": re.findall(r"\bfrom\s+['\"]([^'\"]+)['\"]", source),
            "complexity": 1 + len(re.findall(r"\b(?:if|for|while|case|catch)\b|&&|\|\|", source)),
            "security_findings": findings,
            "syntax_errors": cls._balanced_delimiters(source),
        }

    @classmethod
    def _sql_analysis(cls, source: str) -> dict[str, Any]:
        without_comments = re.sub(r"--[^\n]*|/\*[\s\S]*?\*/", " ", source)
        lexical_source = re.sub(
            r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"",
            "''",
            without_comments,
        )
        statements = [item.strip() for item in lexical_source.split(";") if item.strip()]
        words = re.findall(r"\b[A-Za-z]+\b", lexical_source.upper())
        prohibited = sorted(cls.PROHIBITED_SQL_KEYWORDS & set(words))
        findings = [
            {
                "severity": "high",
                "code": "SQL_WRITE_OR_SCHEMA_CHANGE",
                "symbol": keyword,
                "line": 0,
            }
            for keyword in prohibited
        ]
        errors = cls._balanced_delimiters(without_comments)
        if len(statements) != 1:
            errors.append("exactly one SQL statement is required")
        if statements and not re.match(r"^(SELECT|WITH)\b", statements[0], flags=re.IGNORECASE):
            errors.append("only SELECT or WITH queries are allowed")
        return {
            "node_count": 0,
            "functions": [],
            "classes": [],
            "imports": [],
            "complexity": 1 + words.count("JOIN") + words.count("UNION"),
            "security_findings": findings,
            "syntax_errors": errors,
            "statement_count": len(statements),
            "read_only": not prohibited and not errors and bool(statements),
        }

    @classmethod
    def _validate_source(cls, language: str, source: str) -> dict[str, Any]:
        errors: list[str] = []
        if language == "python":
            analysis = cls._python_analysis(source)
            errors.extend(analysis["syntax_errors"])
        elif language in {"typescript", "javascript"}:
            analysis = cls._script_analysis(source)
            errors.extend(analysis["syntax_errors"])
        elif language == "sql":
            analysis = cls._sql_analysis(source)
            errors.extend(analysis["syntax_errors"])
        else:
            analysis = {"node_count": 0, "security_findings": [], "syntax_errors": []}
            try:
                json.loads(source)
            except json.JSONDecodeError as error:
                errors.append(f"line {error.lineno}: {error.msg}")
        security_ok = not any(
            item.get("severity") == "high" for item in analysis["security_findings"]
        )
        return {
            "ok": not errors and security_ok,
            "syntax_ok": not errors,
            "security_ok": security_ok,
            "errors": errors,
            "sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
            "character_count": len(source),
            "ast_node_count": int(analysis["node_count"]),
            "analysis": analysis,
            "executed": False,
        }

    @staticmethod
    def _refactor_python(source: str) -> str:
        try:
            return ast.unparse(ast.parse(source)).strip() + "\n"
        except (SyntaxError, ValueError):
            return source
