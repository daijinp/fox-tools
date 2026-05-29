#!/usr/bin/env python3
"""
Convert DataGrip-exported ClickHouse INSERT SQL containing Java-style map
literals like:

    {key=value, other=123}

into ClickHouse-compatible SQL:

    map('key', 'value', 'other', 123)

The converter focuses on INSERT ... VALUES statements and keeps all other SQL
unchanged.


datagrip导出ck数据的inster语句转为正确可插入的语句
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


INSERT_RE = re.compile(
    r"^\s*INSERT\s+INTO\s+(?P<table>.+?)\s*\((?P<columns>.*?)\)\s*VALUES\s*(?P<values>.+?)\s*;?\s*$",
    re.IGNORECASE | re.DOTALL,
)

NUMERIC_FLOAT_RE = re.compile(r"^[+-]?(?:\d+\.\d+|\d+\.\d*|\.\d+)(?:[eE][+-]?\d+)?$")
NUMERIC_INT_RE = re.compile(r"^[+-]?\d+$")
VALID_MAP_TYPES = {"String", "Float64", "Int64", "UInt8"}


@dataclass
class MapLiteral:
    pairs: list[tuple[str, str]]
    raw: str

    @property
    def is_empty(self) -> bool:
        return not self.pairs


@dataclass
class StatementModel:
    raw: str
    columns: list[str]
    rows: list[list[str | MapLiteral]]
    matched: bool


def split_sql_statements(sql: str) -> list[str]:
    statements: list[str] = []
    start = 0
    quote: str | None = None
    depth_paren = 0
    depth_brace = 0
    i = 0
    while i < len(sql):
        ch = sql[i]
        if quote:
            if ch == quote:
                if quote == "'" and i + 1 < len(sql) and sql[i + 1] == "'":
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
        elif ch == "(":
            depth_paren += 1
        elif ch == ")":
            depth_paren = max(0, depth_paren - 1)
        elif ch == "{":
            depth_brace += 1
        elif ch == "}":
            depth_brace = max(0, depth_brace - 1)
        elif ch == ";" and depth_paren == 0 and depth_brace == 0:
            statements.append(sql[start : i + 1])
            start = i + 1
        i += 1
    tail = sql[start:]
    if tail.strip():
        statements.append(tail)
    return statements


def split_top_level(text: str, delimiter: str = ",") -> list[str]:
    parts: list[str] = []
    start = 0
    quote: str | None = None
    depth_paren = 0
    depth_brace = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == quote:
                if quote == "'" and i + 1 < len(text) and text[i + 1] == "'":
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
        elif ch == "(":
            depth_paren += 1
        elif ch == ")":
            depth_paren -= 1
        elif ch == "{":
            depth_brace += 1
        elif ch == "}":
            depth_brace -= 1
        elif ch == delimiter and depth_paren == 0 and depth_brace == 0:
            parts.append(text[start:i].strip())
            start = i + 1
        i += 1
    parts.append(text[start:].strip())
    return parts


def extract_parenthesized_groups(text: str) -> list[str]:
    groups: list[str] = []
    quote: str | None = None
    depth = 0
    start = -1
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == quote:
                if quote == "'" and i + 1 < len(text) and text[i + 1] == "'":
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
        elif ch == "(":
            if depth == 0:
                start = i
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0 and start >= 0:
                groups.append(text[start + 1 : i])
        i += 1
    return groups


def parse_map_literal(token: str) -> MapLiteral:
    stripped = token.strip()
    inner = stripped[1:-1].strip()
    if not inner:
        return MapLiteral([], raw=token)
    pairs: list[tuple[str, str]] = []
    for item in split_top_level(inner):
        if "=" not in item:
            raise ValueError(f"Invalid map entry: {item}")
        key, value = item.split("=", 1)
        pairs.append((key.strip(), value.strip()))
    return MapLiteral(pairs, raw=token)


def parse_statement(statement: str) -> StatementModel:
    match = INSERT_RE.match(statement.strip())
    if not match:
        return StatementModel(raw=statement, columns=[], rows=[], matched=False)

    columns = [clean_column_name(part) for part in split_top_level(match.group("columns"))]
    row_texts = extract_parenthesized_groups(match.group("values"))
    rows: list[list[str | MapLiteral]] = []
    for row_text in row_texts:
        tokens = split_top_level(row_text)
        parsed_row: list[str | MapLiteral] = []
        for token in tokens:
            stripped = token.strip()
            if stripped.startswith("{") and stripped.endswith("}"):
                parsed_row.append(parse_map_literal(stripped))
            else:
                parsed_row.append(stripped)
        rows.append(parsed_row)

    return StatementModel(raw=statement, columns=columns, rows=rows, matched=True)


def clean_column_name(name: str) -> str:
    stripped = name.strip()
    if stripped.startswith("`") and stripped.endswith("`"):
        return stripped[1:-1]
    return stripped


def parse_map_type_overrides(items: Iterable[str]) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise ValueError(f"Invalid --map-type value: {item}")
        column, value_type = item.split("=", 1)
        overrides[column.strip()] = value_type.strip()
    return overrides


def infer_column_map_types(
    models: list[StatementModel],
    overrides: dict[str, str],
    empty_default_type: str | None,
) -> dict[str, str]:
    inferred = dict(overrides)
    for model in models:
        if not model.matched:
            continue
        for row in model.rows:
            for idx, cell in enumerate(row):
                if not isinstance(cell, MapLiteral) or cell.is_empty:
                    continue
                column = model.columns[idx]
                existing = inferred.get(column)
                current = infer_value_type_for_map(cell.pairs)
                if existing is None:
                    inferred[column] = current
                elif existing != current:
                    inferred[column] = reconcile_types(existing, current)

    if empty_default_type:
        for model in models:
            if not model.matched:
                continue
            for row in model.rows:
                for idx, cell in enumerate(row):
                    if isinstance(cell, MapLiteral) and cell.is_empty:
                        inferred.setdefault(model.columns[idx], empty_default_type)

    return inferred


def infer_value_type_for_map(pairs: list[tuple[str, str]]) -> str:
    seen_float = False
    seen_int = False
    for _, raw_value in pairs:
        if not raw_value:
            return "String"
        if is_quoted_string(raw_value):
            return "String"
        if is_nullish(raw_value):
            return "String"
        if looks_like_float(raw_value):
            seen_float = True
            continue
        if looks_like_int(raw_value):
            seen_int = True
            continue
        return "String"
    if seen_float:
        return "Float64"
    if seen_int:
        return "Int64"
    return "String"


def reconcile_types(left: str, right: str) -> str:
    if left == right:
        return left
    if "String" in (left, right):
        return "String"
    if "Float64" in (left, right):
        return "Float64"
    return "Int64"


def is_quoted_string(value: str) -> bool:
    return len(value) >= 2 and value[0] == "'" and value[-1] == "'"


def is_nullish(value: str) -> bool:
    return value.upper() in {"NULL", "NAN", "INF", "+INF", "-INF"}


def looks_like_float(value: str) -> bool:
    return bool(NUMERIC_FLOAT_RE.match(value))


def looks_like_int(value: str) -> bool:
    return bool(NUMERIC_INT_RE.match(value))


def render_sql_string(value: str) -> str:
    unquoted = strip_single_quotes(value)
    return "'" + unquoted.replace("'", "''") + "'"


def strip_single_quotes(value: str) -> str:
    if is_quoted_string(value):
        return value[1:-1].replace("''", "'")
    return value


def render_map_literal(map_lit: MapLiteral, value_type: str | None) -> str:
    if map_lit.is_empty:
        if value_type:
            return f"CAST(map(), 'Map(String, {value_type})')"
        return "map()"

    resolved_type = value_type or infer_value_type_for_map(map_lit.pairs)
    rendered_parts: list[str] = []
    for key, raw_value in map_lit.pairs:
        rendered_parts.append(render_sql_string(key))
        if resolved_type == "String":
            rendered_parts.append(render_sql_string(raw_value))
        else:
            rendered_parts.append(raw_value if raw_value else "NULL")
    return "map(" + ", ".join(rendered_parts) + ")"


def render_statement(model: StatementModel, column_types: dict[str, str]) -> str:
    if not model.matched:
        return model.raw.strip() + ("\n" if not model.raw.endswith("\n") else "")

    header_match = INSERT_RE.match(model.raw.strip())
    assert header_match is not None
    table = header_match.group("table").strip()
    columns_sql = ", ".join(model.columns)

    rendered_rows: list[str] = []
    for row in model.rows:
        rendered_cells: list[str] = []
        for idx, cell in enumerate(row):
            if isinstance(cell, MapLiteral):
                rendered_cells.append(render_map_literal(cell, column_types.get(model.columns[idx])))
            else:
                rendered_cells.append(cell)
        rendered_rows.append("(" + ", ".join(rendered_cells) + ")")

    return f"INSERT INTO {table} ({columns_sql}) VALUES\n" + ",\n".join(rendered_rows) + ";\n"


def convert_sql(sql: str, overrides: dict[str, str], empty_default_type: str | None) -> tuple[str, dict[str, str]]:
    statements = split_sql_statements(sql)
    models = [parse_statement(statement) for statement in statements]
    column_types = infer_column_map_types(models, overrides, empty_default_type)
    rendered = "".join(render_statement(model, column_types) for model in models)
    return rendered, column_types


def load_config(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    return json.loads(config_path.read_text(encoding="utf-8"))


def normalize_map_type_overrides(raw_overrides: Any) -> dict[str, str]:
    if raw_overrides is None:
        return {}
    if not isinstance(raw_overrides, dict):
        raise ValueError("'map_type_overrides' must be a JSON object.")

    normalized: dict[str, str] = {}
    for column, value_type in raw_overrides.items():
        if not isinstance(column, str) or not isinstance(value_type, str):
            raise ValueError("'map_type_overrides' keys and values must be strings.")
        if value_type not in VALID_MAP_TYPES:
            raise ValueError(f"Unsupported map type for column '{column}': {value_type}")
        normalized[column] = value_type
    return normalized


def resolve_path(base_dir: Path, raw_path: str) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        path = base_dir / path
    return path


def read_input(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write_output(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def print_inferred_types(inferred_types: dict[str, str]) -> None:
    for column, value_type in sorted(inferred_types.items()):
        print(f"{column}=Map(String, {value_type})")


def main() -> int:
    script_dir = Path(__file__).resolve().parent
    config_path = script_dir / "config.json"

    try:
        config = load_config(config_path)
        input_file = resolve_path(script_dir, str(config.get("input_file", "input.sql")))
        output_file = resolve_path(script_dir, str(config.get("output_file", "output.sql")))
        empty_default_type = config.get("empty_map_default_type")
        if empty_default_type is not None and empty_default_type not in VALID_MAP_TYPES:
            raise ValueError(
                f"Unsupported 'empty_map_default_type': {empty_default_type}. "
                f"Expected one of {sorted(VALID_MAP_TYPES)}"
            )
        print_types = bool(config.get("print_inferred_types", False))
        overrides = normalize_map_type_overrides(config.get("map_type_overrides"))
        source = read_input(input_file)
        converted, inferred_types = convert_sql(source, overrides, empty_default_type)
        write_output(output_file, converted)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Converted SQL written to: {output_file}")
    if print_types and inferred_types:
        print_inferred_types(inferred_types)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
