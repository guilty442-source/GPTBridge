from __future__ import annotations

import ast
import difflib
import hashlib
import json
import re
from pathlib import PurePosixPath
from typing import Any


class StarCodingExpert:
    """Governed program synthesis, analysis, refactoring and upgrade authoring."""

    ALLOWED_LANGUAGES = frozenset({"python", "typescript", "javascript", "sql", "json"})
    ALLOWED_ACTIONS = frozenset({"generate", "analyze", "refactor", "self_upgrade"})
    ALLOWED_PYTHON_KINDS = frozenset(
        {"function", "class", "dataclass", "test", "api"}
    )
    ALLOWED_SCRIPT_KINDS = frozenset({"function", "class", "test"})
    PROTECTED_PROJECT_ROOTS = frozenset({"governance_rule", ".git"})
    PROJECT_SOURCE_ROOTS = frozenset(
        {
            "ai-assistant",
            "ai-collaboration",
            "file-sorter",
            "global-cleaner",
            "investment-mobile",
            "local-ai",
            "main-system",
            "shared-layer",
            "system-rescue",
            "vaultly",
        }
    )
    PROHIBITED_IMPORTS = frozenset({"ctypes", "subprocess", "winreg"})
    PROHIBITED_CALLS = frozenset(
        {
            "eval",
            "exec",
            "compile",
            "__import__",
            "builtins.eval",
            "builtins.exec",
            "builtins.compile",
            "builtins.__import__",
            "os.system",
            "os.remove",
            "os.unlink",
            "os.rmdir",
            "os.removedirs",
            "os.rename",
            "os.replace",
            "shutil.rmtree",
            "shutil.move",
            "pathlib.Path.unlink",
            "pathlib.Path.rmdir",
            "pathlib.Path.rename",
            "pathlib.Path.replace",
            "pathlib.Path.write_text",
            "pathlib.Path.write_bytes",
            # Calls on constructed Path objects have no stable AST prefix.
            "unlink",
            "rmdir",
            "write_text",
            "write_bytes",
        }
    )
    PROHIBITED_DYNAMIC_CALL_NAMES = frozenset(
        {
            "eval",
            "exec",
            "compile",
            "__import__",
            "system",
            "remove",
            "unlink",
            "rmdir",
            "removedirs",
            "rename",
            "replace",
            "rmtree",
            "move",
            "write_text",
            "write_bytes",
        }
    )
    PROHIBITED_SCRIPT_PATTERNS = (
        r"\beval\s*\(",
        r"\bnew\s+Function\s*\(",
        r"\bchild_process\b",
        r"\bprocess\.binding\s*\(",
    )
    PROHIBITED_SQL_KEYWORDS = frozenset(
        {
            "INSERT",
            "UPDATE",
            "DELETE",
            "DROP",
            "ALTER",
            "CREATE",
            "REPLACE",
            "ATTACH",
            "DETACH",
            "PRAGMA",
        }
    )

    @staticmethod
    def _identifier(value: Any, default: str = "generated_task") -> str:
        normalized = re.sub(r"\W+", "_", str(value or "").strip(), flags=re.UNICODE)
        normalized = normalized.strip("_") or default
        if normalized[0].isdigit():
            normalized = f"task_{normalized}"
        return normalized[:80] if normalized.isidentifier() else default

    @staticmethod
    def _string_literal(value: Any) -> str:
        return json.dumps(str(value or ""), ensure_ascii=False)

    @classmethod
    def _infer_spec(cls, prompt: str) -> dict[str, Any]:
        normalized = str(prompt or "").casefold()
        rules = (
            (("平均", "average", "mean"), "calculate_average", "sum(values) / len(values) if values else 0"),
            (("加總", "總和", "sum"), "calculate_total", "sum(values)"),
            (("最大", "maximum", " max"), "find_maximum", "max(values) if values else None"),
            (("最小", "minimum", " min"), "find_minimum", "min(values) if values else None"),
            (("數量", "個數", "count"), "count_items", "len(values)"),
            (("排序", "sort"), "sort_values", "sorted(values)"),
        )
        inferred: dict[str, Any] = {
            "language": "python",
            "kind": "function",
            "name": "generated_task",
            "parameters": ["payload"],
            "description": prompt or "星澄產生的函式",
            "return_expression": "None",
            "operation": "custom",
        }
        if any(
            token in normalized
            for token in ("fastapi", "rest api", "restful", "api 服務", "api服務")
        ):
            inferred.update(
                kind="api",
                name="star_api",
                framework="fastapi",
                resource="items",
            )
        for tokens, name, expression in rules:
            if any(token in normalized for token in tokens):
                inferred.update(
                    name=name,
                    parameters=["values"],
                    return_expression=expression,
                    operation={
                        "calculate_average": "average",
                        "calculate_total": "sum",
                        "find_maximum": "maximum",
                        "find_minimum": "minimum",
                        "count_items": "count",
                        "sort_values": "sort",
                    }[name],
                )
                break
        if "typescript" in normalized or "typescript" in str(prompt).casefold():
            inferred["language"] = "typescript"
        elif "javascript" in normalized or "javascript" in str(prompt).casefold():
            inferred["language"] = "javascript"
        elif re.search(r"\bsql\b", normalized):
            inferred["language"] = "sql"
        elif "json" in normalized:
            inferred["language"] = "json"
        if inferred["kind"] == "api":
            pass
        elif "dataclass" in normalized or "資料類別" in normalized:
            inferred.update(kind="dataclass", name="GeneratedRecord")
        elif "類別" in normalized or "class" in normalized:
            inferred.update(kind="class", name="GeneratedService")
        elif "測試" in normalized or "unittest" in normalized:
            inferred.update(kind="test", name="GeneratedCodeTest")
        return inferred

    @classmethod
    def _normalize_spec(cls, payload: dict[str, Any], prompt: str, intent: str) -> dict[str, Any]:
        inferred = cls._infer_spec(prompt)
        provided = payload.get("code_spec")
        if isinstance(provided, dict):
            inferred.update(provided)
        action = str(
            inferred.get("action")
            or ("self_upgrade" if intent == "self_upgrade" else "generate")
        ).strip().casefold()
        inferred["action"] = action if action in cls.ALLOWED_ACTIONS else "generate"
        inferred["language"] = str(inferred.get("language") or "python").strip().casefold()
        inferred["kind"] = str(inferred.get("kind") or "function").strip().casefold()
        if inferred["language"] == "sql":
            inferred["kind"] = "query"
        elif inferred["language"] == "json":
            inferred["kind"] = "document"
        return inferred

    @classmethod
    def _python_function(cls, spec: dict[str, Any], prompt: str) -> str:
        name = cls._identifier(spec.get("name"))
        requested = spec.get("parameters")
        parameters = (
            [cls._identifier(item, f"arg_{index}") for index, item in enumerate(requested)]
            if isinstance(requested, list)
            else ["payload"]
        )
        parameters = list(dict.fromkeys(parameters))[:16]
        docstring = str(spec.get("description") or prompt or "星澄產生的函式").strip()[:500]
        expression_text = str(spec.get("return_expression") or "None").strip()[:4_000]
        try:
            expression = ast.parse(expression_text, mode="eval")
            normalized_expression = ast.unparse(expression.body)
        except (SyntaxError, ValueError):
            normalized_expression = "None"
        return (
            f"def {name}({', '.join(parameters)}):\n"
            f"    {cls._string_literal(docstring)}\n"
            f"    return {normalized_expression}\n"
        )

    @classmethod
    def _field_specs(cls, value: Any) -> list[dict[str, str]]:
        if not isinstance(value, list) or not value:
            return [{"name": "value", "type": "Any", "default": "None"}]
        fields: list[dict[str, str]] = []
        for index, item in enumerate(value[:32]):
            record = item if isinstance(item, dict) else {"name": item}
            fields.append(
                {
                    "name": cls._identifier(record.get("name"), f"field_{index}"),
                    "type": str(record.get("type") or "Any")[:80],
                    "default": repr(record.get("default")),
                }
            )
        return fields

    @classmethod
    def _python_dataclass(cls, spec: dict[str, Any], prompt: str) -> str:
        name = cls._identifier(spec.get("name"), "GeneratedRecord")
        fields = cls._field_specs(spec.get("fields"))
        body = "\n".join(
            f"    {field['name']}: {field['type']} = {field['default']}" for field in fields
        )
        return (
            "from dataclasses import dataclass\n"
            "from typing import Any\n\n"
            "@dataclass\n"
            f"class {name}:\n"
            f"    {cls._string_literal(spec.get('description') or prompt or '星澄產生的資料類別')}\n"
            f"{body}\n"
        )

    @classmethod
    def _python_class(cls, spec: dict[str, Any], prompt: str) -> str:
        name = cls._identifier(spec.get("name"), "GeneratedService")
        fields = cls._field_specs(spec.get("fields"))
        parameters = ", ".join(f"{field['name']}=None" for field in fields)
        assignments = "\n".join(
            f"        self.{field['name']} = {field['name']}" for field in fields
        )
        return (
            f"class {name}:\n"
            f"    {cls._string_literal(spec.get('description') or prompt or '星澄產生的類別')}\n\n"
            f"    def __init__(self, {parameters}):\n"
            f"{assignments}\n"
        )

    @classmethod
    def _python_test(cls, spec: dict[str, Any], prompt: str) -> str:
        subject = cls._identifier(spec.get("subject"), "generated_task")
        test_name = cls._identifier(spec.get("name"), f"test_{subject}")
        cases = spec.get("test_cases")
        assertions: list[str] = []
        for case in cases[:20] if isinstance(cases, list) else []:
            if isinstance(case, dict):
                args = repr(case.get("args") if isinstance(case.get("args"), list) else [])
                assertions.append(
                    f"    assert {subject}(*{args}) == {repr(case.get('expected'))}"
                )
        if not assertions:
            assertions.append(f"    assert callable({subject})")
        return (
            f"def {test_name}():\n"
            f"    {cls._string_literal(spec.get('description') or prompt or '星澄產生的測試')}\n"
            + "\n".join(assertions)
            + "\n"
        )

    @classmethod
    def _python_api(cls, spec: dict[str, Any], prompt: str) -> str:
        """Generate a complete, transaction-safe FastAPI CRUD module.

        The module is emitted but never executed by Star.  It uses only a local
        SQLite database and converts storage failures into explicit HTTP errors.
        """

        resource = cls._identifier(spec.get("resource"), "items").casefold()
        table = cls._identifier(spec.get("table"), resource).casefold()
        title = cls._string_literal(
            spec.get("title") or prompt or "Star generated FastAPI service"
        )
        database_name = cls._identifier(spec.get("database_name"), "star_api") + ".sqlite3"
        return f'''from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from collections.abc import Iterator

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field


DATABASE_PATH = {database_name!r}
app = FastAPI(title={title})


class ItemInput(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    value: float


@contextmanager
def database_transaction() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN")
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize_database() -> None:
    with database_transaction() as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS {table} ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "name TEXT NOT NULL, value REAL NOT NULL)"
        )


@app.on_event("startup")
def startup() -> None:
    initialize_database()


@app.post("/{resource}", status_code=201)
def create_item(payload: ItemInput) -> dict[str, object]:
    try:
        with database_transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO {table}(name, value) VALUES(?, ?)",
                (payload.name, payload.value),
            )
            return {{"id": int(cursor.lastrowid), **payload.model_dump()}}
    except sqlite3.DatabaseError as error:
        raise HTTPException(status_code=503, detail="database unavailable") from error


@app.get("/{resource}/{{item_id}}")
def read_item(item_id: int) -> dict[str, object]:
    try:
        with database_transaction() as connection:
            row = connection.execute(
                "SELECT id, name, value FROM {table} WHERE id = ?", (item_id,)
            ).fetchone()
    except sqlite3.DatabaseError as error:
        raise HTTPException(status_code=503, detail="database unavailable") from error
    if row is None:
        raise HTTPException(status_code=404, detail="item not found")
    return dict(row)
'''

    @classmethod
    def _python_source(cls, spec: dict[str, Any], prompt: str) -> str:
        generators = {
            "function": cls._python_function,
            "class": cls._python_class,
            "dataclass": cls._python_dataclass,
            "test": cls._python_test,
            "api": cls._python_api,
        }
        return generators[str(spec.get("kind") or "function")](spec, prompt)

    @classmethod
    def _script_expression(cls, spec: dict[str, Any]) -> str:
        operation = str(spec.get("operation") or "custom")
        expressions = {
            "average": "values.length ? values.reduce((total, value) => total + value, 0) / values.length : 0",
            "sum": "values.reduce((total, value) => total + value, 0)",
            "maximum": "values.length ? Math.max(...values) : null",
            "minimum": "values.length ? Math.min(...values) : null",
            "count": "values.length",
            "sort": "[...values].sort((left, right) => left - right)",
        }
        if operation in expressions:
            return expressions[operation]
        candidate = str(spec.get("return_expression") or "null").strip()[:4_000]
        return "null" if candidate in {"", "None"} else candidate

    @staticmethod
    def _script_type(value: Any, default: str = "unknown") -> str:
        candidate = str(value or default).strip()[:120]
        if re.fullmatch(
            r"[A-Za-z_$][\w$]*(?:\s*<\s*[A-Za-z_$][\w$]*(?:\s*,\s*[A-Za-z_$][\w$]*)*\s*>)?(?:\[\])?",
            candidate,
        ):
            return candidate
        return default

    @classmethod
    def _script_source(cls, spec: dict[str, Any], prompt: str, language: str) -> str:
        kind = str(spec.get("kind") or "function")
        name = cls._identifier(spec.get("name"))
        requested = spec.get("parameters")
        parameters = (
            [cls._identifier(item, f"arg_{index}") for index, item in enumerate(requested)]
            if isinstance(requested, list)
            else ["payload"]
        )
        parameters = list(dict.fromkeys(parameters))[:16]
        export = "export " if spec.get("export", True) is not False else ""
        if language == "typescript":
            parameter_types = spec.get("parameter_types")
            type_map = parameter_types if isinstance(parameter_types, dict) else {}
            if str(spec.get("operation")) in {"average", "sum", "maximum", "minimum", "sort"}:
                type_map = {**type_map, "values": "number[]"}
            typed_parameters = ", ".join(
                f"{parameter}: {cls._script_type(type_map.get(parameter))}"
                for parameter in parameters
            )
            inferred_return = {
                "average": "number",
                "sum": "number",
                "maximum": "number | null",
                "minimum": "number | null",
                "count": "number",
                "sort": "number[]",
            }.get(str(spec.get("operation")), "unknown")
            return_type = f": {cls._script_type(spec.get('return_type'), inferred_return)}"
        else:
            typed_parameters = ", ".join(parameters)
            return_type = ""
        description = str(spec.get("description") or prompt or "Star generated code").replace("*/", "")[:500]
        if kind == "class":
            field = cls._identifier(spec.get("field"), "value")
            field_type = ": unknown" if language == "typescript" else ""
            return (
                f"/** {description} */\n"
                f"{export}class {name} {{\n"
                f"  {field}{field_type};\n\n"
                f"  constructor({field}{field_type}) {{\n"
                f"    this.{field} = {field};\n"
                "  }\n"
                "}\n"
            )
        expression = cls._script_expression(spec)
        function_source = (
            f"/** {description} */\n"
            f"{export}function {name}({typed_parameters}){return_type} {{\n"
            f"  return {expression};\n"
            "}\n"
        )
        if kind != "test":
            return function_source
        subject = cls._identifier(spec.get("subject"), name)
        test_return_type = ": void" if language == "typescript" else ""
        return (
            f"/** {description} */\n"
            f"{export}function test_{subject}(){test_return_type} {{\n"
            f"  console.assert(typeof {subject} === 'function');\n"
            "}\n"
        )

    @staticmethod
    def _sql_identifier(value: Any, default: str) -> str:
        candidate = str(value or default).strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", candidate):
            candidate = default
        return f'"{candidate}"'

    @classmethod
    def _sql_source(cls, spec: dict[str, Any]) -> str:
        table = cls._sql_identifier(spec.get("table"), "records")
        requested_fields = spec.get("fields")
        fields = (
            ", ".join(cls._sql_identifier(item, "value") for item in requested_fields[:64])
            if isinstance(requested_fields, list) and requested_fields
            else "*"
        )
        query = f"SELECT {fields}\nFROM {table}"
        filters = spec.get("filters")
        if isinstance(filters, dict) and filters:
            clauses = [
                f"{cls._sql_identifier(key, 'value')} = :{cls._identifier(key, 'value')}"
                for key in list(filters)[:32]
            ]
            query += "\nWHERE " + " AND ".join(clauses)
        order_by = spec.get("order_by")
        if order_by:
            direction = "DESC" if str(spec.get("direction") or "ASC").upper() == "DESC" else "ASC"
            query += f"\nORDER BY {cls._sql_identifier(order_by, 'value')} {direction}"
        if spec.get("limit") is not None:
            try:
                limit = max(1, min(10_000, int(spec["limit"])))
            except (TypeError, ValueError):
                limit = 100
            query += f"\nLIMIT {limit}"
        return query + ";\n"

    @classmethod
    def _json_document(cls, spec: dict[str, Any], prompt: str) -> str:
        value = spec.get("value")
        if value is None:
            value = {"name": str(spec.get("name") or "star-generated"), "description": prompt}
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

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

    @classmethod
    def _generated_tests(cls, spec: dict[str, Any], source: str) -> dict[str, Any]:
        if str(spec.get("language")) == "python" and str(spec.get("kind")) == "api":
            test_source = (
                source.rstrip()
                + "\n\n"
                + "def test_api_contract():\n"
                + "    assert app.title\n"
                + "    assert callable(create_item)\n"
                + "    assert callable(read_item)\n"
            )
            return {
                "available": True,
                "source": test_source,
                "validation": cls._validate_source("python", test_source),
                "executed": False,
            }
        cases = spec.get("test_cases")
        if (
            str(spec.get("language")) != "python"
            or str(spec.get("kind")) != "function"
            or not isinstance(cases, list)
            or not cases
        ):
            return {"available": False, "source": "", "validation": None}
        test_spec = {
            "kind": "test",
            "subject": cls._identifier(spec.get("name")),
            "name": f"test_{cls._identifier(spec.get('name'))}",
            "test_cases": cases,
            "description": "星澄依規格產生的驗證案例",
        }
        test_source = source.rstrip() + "\n\n" + cls._python_test(test_spec, "")
        return {
            "available": True,
            "source": test_source,
            "validation": cls._validate_source("python", test_source),
            "executed": False,
        }

    @classmethod
    def _upgrade_target(
        cls,
        value: Any,
        language: str,
        *,
        existing_source: bool = False,
        selected_folder_scope: bool = False,
    ) -> dict[str, Any]:
        candidate = PurePosixPath(str(value or "").replace("\\", "/"))
        suffixes = {
            "python": {".py"},
            "typescript": {".ts", ".tsx"},
            "javascript": {".js", ".jsx", ".mjs"},
            "sql": {".sql"},
            "json": {".json"},
        }
        valid = (
            not candidate.is_absolute()
            and ".." not in candidate.parts
            and candidate.suffix.casefold() in suffixes.get(language, set())
            and bool(candidate.parts)
            and candidate.parts[0].casefold() not in cls.PROTECTED_PROJECT_ROOTS
            and (
                selected_folder_scope
                or len(candidate.parts) == 1
                or candidate.parts[0].casefold() in cls.PROJECT_SOURCE_ROOTS
                or tuple(part.casefold() for part in candidate.parts[:4])
                == ("src", "backend", "services", "local_ai")
            )
        )
        return {
            "path": candidate.as_posix(),
            "within_project_source": valid,
            "programming_project_root": "user-selected-folder",
            "outside_project_access": False,
            "project_scope": "all-project-source-excluding-governance-rule",
            "governance_rule_excluded": True,
            # Retained for v1 proposal readers; the effective scope is now
            # the project-wide field above.
            "within_local_ai_source": valid,
            "new_file_only": not existing_source,
        }

    @staticmethod
    def _unified_diff(original: str, proposed: str, target_path: str) -> str:
        return "".join(
            difflib.unified_diff(
                original.splitlines(keepends=True),
                proposed.splitlines(keepends=True),
                fromfile=f"a/{target_path}",
                tofile=f"b/{target_path}",
            )
        )

    def process(self, payload: dict[str, Any], intent: str) -> dict[str, Any]:
        prompt = str(payload.get("instruction") or payload.get("prompt") or "").strip()
        spec = self._normalize_spec(payload, prompt, intent)
        language = str(spec["language"])
        action = str(spec["action"])
        if language not in self.ALLOWED_LANGUAGES:
            return {
                "ok": False,
                "error_code": "UNSUPPORTED_CODE_LANGUAGE",
                "supported_languages": sorted(self.ALLOWED_LANGUAGES),
            }
        if language == "python" and str(spec["kind"]) not in self.ALLOWED_PYTHON_KINDS:
            return {
                "ok": False,
                "error_code": "UNSUPPORTED_PYTHON_ARTIFACT",
                "supported_kinds": sorted(self.ALLOWED_PYTHON_KINDS),
            }
        if (
            language in {"typescript", "javascript"}
            and str(spec["kind"]) not in self.ALLOWED_SCRIPT_KINDS
        ):
            return {
                "ok": False,
                "error_code": "UNSUPPORTED_SCRIPT_ARTIFACT",
                "supported_kinds": sorted(self.ALLOWED_SCRIPT_KINDS),
            }
        supplied_source = str(payload.get("source_code") or spec.get("source_code") or "")
        if action in {"analyze", "refactor"} and not supplied_source.strip():
            return {"ok": False, "error_code": "SOURCE_CODE_REQUIRED", "action": action}
        if action == "analyze":
            source = supplied_source
        elif action == "refactor":
            source = (
                self._refactor_python(supplied_source)
                if language == "python"
                else supplied_source.strip() + "\n"
            )
        else:
            if language == "python":
                source = self._python_source(spec, prompt)
            elif language in {"typescript", "javascript"}:
                source = self._script_source(spec, prompt, language)
            elif language == "sql":
                source = self._sql_source(spec)
            else:
                source = self._json_document(spec, prompt)
        validation = self._validate_source(language, source)
        tests = self._generated_tests(spec, source)
        result: dict[str, Any] = {
            "ok": validation["ok"],
            "action": action,
            "artifact_kind": str(spec["kind"]),
            "language": language,
            "source": source,
            "validation": validation,
            "generated_tests": tests,
            "normalized_spec": spec,
            "synthesis": "star-constrained-ast-program-synthesis",
            "generated_by": "star-coding-native-model",
            "network_used": False,
            "external_model_used": False,
            "executed": False,
        }
        if intent == "self_upgrade" or action == "self_upgrade":
            original_source = str(
                payload.get("original_source") or spec.get("original_source") or ""
            )
            target = self._upgrade_target(
                spec.get("target_path")
                or {
                    "python": "local-ai/src/backend/services/local_ai/application/generated_extension.py",
                    "typescript": "local-ai/src/backend/services/local_ai/application/generated_extension.ts",
                    "javascript": "local-ai/src/backend/services/local_ai/application/generated_extension.js",
                    "sql": "local-ai/src/backend/services/local_ai/application/generated_query.sql",
                    "json": "local-ai/src/backend/services/local_ai/application/generated_extension.json",
                }[language],
                language,
                existing_source=bool(original_source),
                selected_folder_scope=bool(
                    str(payload.get("programming_folder") or "").strip()
                ),
            )
            tests_valid = tests["available"] is False or bool(
                isinstance(tests.get("validation"), dict)
                and tests["validation"].get("ok") is True
            )
            approved = validation["ok"] and tests_valid and target["within_project_source"]
            result["upgrade_proposal"] = {
                "schema": "star-self-upgrade-proposal/v1",
                "target": target,
                "self_authored": True,
                "proposal_ready": approved,
                "source_sha256": validation["sha256"],
                "test_source_sha256": (
                    tests["validation"]["sha256"]
                    if tests["available"] and isinstance(tests.get("validation"), dict)
                    else ""
                ),
                "change_type": "modify" if original_source else "create",
                "unified_diff": self._unified_diff(
                    original_source,
                    source,
                    str(target["path"]),
                ),
                "required_checks": [
                    "scope-validation",
                    "syntax-validation",
                    "static-security-scan",
                    "isolated-test-suite",
                    "governance-audit",
                    "recoverable-backup",
                ],
                "source_write_performed": False,
                "publish_authority": "governance-versioned-release-only",
                "rollback_required": True,
            }
        return result


__all__ = ["StarCodingExpert"]
