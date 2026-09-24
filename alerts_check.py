#!/usr/bin/env python3
"""Проверка alerts.yaml по схеме: структура плюс имена подстановок.

Работает и в CI (действие GitHub), и на сервере перед применением конфига —
вторая линия обороны нужна потому, что файл может приехать мимо CI.

Использование:
    alerts_check.py [--схема ПУТЬ-ИЛИ-АДРЕС] ФАЙЛ [ФАЙЛ ...]

Выход: 0 — всё хорошо, 1 — найдены ошибки, 2 — не смогли даже прочитать.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import jsonschema
import yaml

SCHEMA_DEFAULT = Path(__file__).resolve().parent / "alerts.schema.json"
if not SCHEMA_DEFAULT.exists():
    SCHEMA_DEFAULT = Path(__file__).resolve().parent.parent / "alerts.schema.json"

SCHEMA_VERSION_KEY = "x-версия"
SCHEMA_TIMEOUT = 20

PLACEHOLDER = re.compile(r"\$\{([^}]*)\}")


@dataclass(frozen=True)
class Problem:
    """Одна претензия к файлу: строка и причина по-русски."""

    line: int
    reason: str


class SchemaError(Exception):
    """Схему не достали или она без версии — проверять нечем."""


def read_schema_text(source: str | Path) -> str:
    """Текст схемы: из файла или по адресу http(s).

    Адрес нужен, когда схему берут не рядом с проверкой, а по ссылке: так конфиг
    можно проверить любой опубликованной версией, не трогая саму проверку.
    """
    text = str(source)
    if text.startswith(("http://", "https://")):
        with urllib.request.urlopen(text, timeout=SCHEMA_TIMEOUT) as response:
            return response.read().decode("utf-8")
    return Path(text).read_text(encoding="utf-8")


def schema_version(schema: dict) -> int:
    """Номер версии схемы.

    Без версии схему нельзя опубликовать: на неё нечем сослаться и не видно, по
    какой именно проверяли конфиг, когда схема приехала по сети.
    """
    version = schema.get(SCHEMA_VERSION_KEY)
    if not isinstance(version, int) or isinstance(version, bool):
        raise SchemaError(f"нет целого «{SCHEMA_VERSION_KEY}» — публиковать нечего")
    return version


def load_schema(source: str | Path) -> dict:
    """Схема по пути или адресу; версия обязательна и читается сразу."""
    try:
        text = read_schema_text(source)
    except (OSError, UnicodeError) as exc:
        # UnicodeError — это адрес с кириллицей: urllib откажется его отправлять,
        # и без перехвата сборка падала бы трейсбеком вместо причины по-русски.
        raise SchemaError(f"не читается: {exc}") from exc
    try:
        schema = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SchemaError(f"это не JSON: {exc}") from exc
    schema_version(schema)
    return schema


def placeholder_catalog(schema: dict) -> dict[str, str]:
    """Список разрешённых имён подстановок — он живёт в самой схеме."""
    return dict(schema.get("x-подстановки", {}))


def walk_scalars(node: yaml.Node) -> Iterator[yaml.ScalarNode]:
    """Все скалярные узлы дерева YAML.

    Комментарии сюда не попадают — иначе закомментированный пример ловился бы
    как ошибка.
    """
    if isinstance(node, yaml.ScalarNode):
        yield node
    elif isinstance(node, yaml.SequenceNode):
        for item in node.value:
            yield from walk_scalars(item)
    elif isinstance(node, yaml.MappingNode):
        for _, value in node.value:
            yield from walk_scalars(value)


def unknown_placeholders(root: yaml.Node, catalog: dict[str, str]) -> list[Problem]:
    """Подстановки с именем не из каталога.

    Опечатка в имени иначе всплыла бы через месяц пустым адресом, а не сейчас.
    """
    known = ", ".join(sorted(catalog)) or "(список пуст)"
    problems = []
    for node in walk_scalars(root):
        for match in PLACEHOLDER.finditer(node.value):
            name = match.group(1)
            if name not in catalog:
                problems.append(
                    Problem(
                        node.start_mark.line + 1,
                        f"неизвестная подстановка ${{{name}}}; в схеме описаны: {known}",
                    )
                )
    return problems


def unquoted_placeholders(root: yaml.Node) -> list[Problem]:
    """Подстановка вне кавычек.

    Значение подставляется в текст до разбора YAML, и оно может сломать разбор
    («@имя_бота» плайн-скаляром не бывает) или сменить тип (номер чата станет
    числом там, где схема ждёт строку). Кавычки снимают оба случая.
    """
    return [
        Problem(node.start_mark.line + 1, "подстановку пиши в кавычках: \"${ИМЯ}\"")
        for node in walk_scalars(root)
        if node.style is None and PLACEHOLDER.search(node.value)
    ]


def node_at(root: yaml.Node, path) -> yaml.Node:
    """Узел по пути из ошибки jsonschema; если не нашли — ближайший предок."""
    node = root
    for step in path:
        if isinstance(node, yaml.MappingNode):
            for key, value in node.value:
                if key.value == step:
                    node = value
                    break
            else:
                return node
        elif isinstance(node, yaml.SequenceNode) and isinstance(step, int):
            if step >= len(node.value):
                return node
            node = node.value[step]
        else:
            return node
    return node


def describe(error: jsonschema.ValidationError) -> str:
    """Причина по-русски. Английский текст jsonschema читать человеку незачем."""
    schema = error.schema if isinstance(error.schema, dict) else {}
    hint = schema.get("description", "")
    if error.validator == "required":
        field = re.search(r"'([^']+)'", error.message)
        return f"нет обязательного поля «{field.group(1) if field else '?'}»"
    if error.validator == "additionalProperties":
        field = re.search(r"'([^']+)'", error.message)
        return f"поле «{field.group(1) if field else '?'}» схеме неизвестно — опечатка?"
    if error.validator == "anyOf":
        needed = [sub["required"][0] for sub in error.validator_value if "required" in sub]
        reason = f"нужно хотя бы одно из полей: {', '.join(needed)}"
        return f"{reason} — {hint}" if hint else reason
    if error.validator == "dependentRequired":
        return f"{error.message} — вместе с этим полем нужна «единица»"
    if error.validator == "pattern":
        return f"значение {error.instance!r} записано не в том формате" + (f": {hint}" if hint else "")
    if error.validator == "const":
        return f"здесь должно быть {error.validator_value!r}, а стоит {error.instance!r}"
    if error.validator == "enum":
        return f"{error.instance!r} — не из списка: {', '.join(map(str, error.validator_value))}"
    if error.validator == "type":
        return f"нужен тип {error.validator_value}, а лежит {error.instance!r}" + (f": {hint}" if hint else "")
    return error.message


def key_line(node: yaml.Node, name: str) -> int | None:
    """Строка, где записан ключ, — чтобы указать на саму опечатку, а не на блок."""
    if isinstance(node, yaml.MappingNode):
        for key, _ in node.value:
            if key.value == name:
                return key.start_mark.line + 1
    return None


def schema_problems(data, schema: dict, root: yaml.Node) -> list[Problem]:
    validator = jsonschema.Draft202012Validator(schema)
    problems = []
    for error in validator.iter_errors(data):
        node = node_at(root, error.absolute_path)
        line = node.start_mark.line + 1
        if error.validator == "additionalProperties":
            extra = re.search(r"'([^']+)'", error.message)
            line = (extra and key_line(node, extra.group(1))) or line
        problems.append(Problem(line, describe(error)))
    return problems


def check_text(text: str, schema: dict) -> list[Problem]:
    """Все претензии к тексту конфига, отсортированные по строкам."""
    root = yaml.compose(text)
    if root is None:
        return [Problem(1, "файл пустой")]
    data = yaml.safe_load(text)
    problems = unknown_placeholders(root, placeholder_catalog(schema))
    problems += unquoted_placeholders(root)
    problems += schema_problems(data, schema, root)
    return sorted(problems, key=lambda p: (p.line, p.reason))


def check_file(path: Path, schema: dict) -> list[Problem]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [Problem(1, f"файл не читается: {exc}")]
    try:
        return check_text(text, schema)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = mark.line + 1 if mark else 1
        return [Problem(line, f"это не YAML: {getattr(exc, 'problem', exc)}")]


def main() -> int:
    parser = argparse.ArgumentParser(description="Проверить alerts.yaml по схеме")
    parser.add_argument("файлы", nargs="+", type=Path)
    parser.add_argument(
        "--схема",
        default=str(SCHEMA_DEFAULT),
        help="путь к схеме или её адрес http(s)://",
    )
    args = parser.parse_args()

    try:
        schema = load_schema(args.схема)
    except SchemaError as exc:
        print(f"схема не годится ({args.схема}): {exc}", file=sys.stderr)
        return 2

    # Видно, чем именно проверяли: со схемой по сети это единственный след в логе.
    print(f"схема версии {schema_version(schema)}: {args.схема}")

    bad = 0
    for path in args.файлы:
        problems = check_file(path, schema)
        for problem in problems:
            print(f"{path}:{problem.line}: {problem.reason}")
        if problems:
            bad += 1
        else:
            print(f"{path}: в порядке")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
