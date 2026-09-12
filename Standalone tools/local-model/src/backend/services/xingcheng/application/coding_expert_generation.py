from __future__ import annotations

import ast
import json
import re
from typing import Any


class CodingExpertGenerationMixin:
    """Source-code generation methods for StarCodingExpert.

    Covers Python, TypeScript/JavaScript, SQL, and JSON artifact generation.
    """

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
