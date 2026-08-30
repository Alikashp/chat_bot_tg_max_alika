"""Критерий приёмки A2: в ядре нет I/O-библиотек.

Проверка механическая, а не «на глаз»: любой импорт aiogram, maxapi, httpx,
aiohttp или драйвера БД внутри app/core или app/ports валит тест. Это та самая
защита от дублирования продуктовой логики по адаптерам, о которой говорит §8
задания.
"""

from __future__ import annotations

import ast
import tokenize
from pathlib import Path

import pytest

FORBIDDEN_ROOTS = frozenset(
    {
        "aiogram",
        "maxapi",
        "aiohttp",
        "httpx",
        "requests",
        "sqlalchemy",
        "asyncpg",
        "psycopg",
        "redis",
        "structlog",
    }
)

PURE_PACKAGES = ("app/core", "app/ports")


def _python_files() -> list[Path]:
    root = Path(__file__).resolve().parents[2]
    return [
        path for package in PURE_PACKAGES for path in (root / package).rglob("*.py")
    ]


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


def test_pure_packages_are_not_empty() -> None:
    """Страховка от теста, который проходит потому, что ничего не проверил."""
    assert len(_python_files()) >= 5


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: p.name)
def test_no_io_libraries_in_core(path: Path) -> None:
    forbidden = _imported_roots(path) & FORBIDDEN_ROOTS

    assert not forbidden, f"{path} импортирует {sorted(forbidden)}"


# --- Критерий A4: ядро не знает про конкретные мессенджеры ----------------

#: Слова, по которым видно, что в ядро протёк механизм конкретного мессенджера.
#:
#: «inline_keyboard» и «reply_keyboard» — самые показательные: у Telegram это
#: две разные вещи, в MAX второй не существует вовсе. Если ядро о них знает,
#: значит оно решает за адаптер, и второй мессенджер потребует правок в core.
FORBIDDEN_WORDS = (
    "inline_keyboard",
    "reply_keyboard",
    "callback_data",
    "update_id",
    "chat_type",
    "file_id",
)


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: p.name)
def test_no_messenger_mechanics_leak_into_core(path: Path) -> None:
    """Проверяем код, а не комментарии.

    Комментарий, объясняющий, во что адаптер превратит наше значение, —
    полезное знание для читателя. Плохо, когда об этом знает сам код: тогда
    имя поля диктует второму мессенджеру чужую механику.
    """
    found = [word for word in FORBIDDEN_WORDS if word in _code_of(path)]

    assert not found, f"{path} знает про механику мессенджера: {found}"


def _code_of(path: Path) -> str:
    """Исходник без комментариев и строковых литералов, в нижнем регистре."""
    kept: list[str] = []
    with path.open("rb") as source:
        for token in tokenize.tokenize(source.readline):
            if token.type in {tokenize.COMMENT, tokenize.STRING}:
                continue
            kept.append(token.string)
    return " ".join(kept).lower()


def test_core_does_not_import_adapters() -> None:
    """Зависимость направлена в одну сторону: адаптеры знают ядро, не наоборот."""
    offenders = [
        path
        for path in _python_files()
        if any(root == "app" for root in _imported_roots(path))
        and "app.adapters" in path.read_text(encoding="utf-8")
    ]

    assert offenders == []
