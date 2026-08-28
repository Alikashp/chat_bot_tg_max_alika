"""Критерий приёмки A2: в ядре нет I/O-библиотек.

Проверка механическая, а не «на глаз»: любой импорт aiogram, maxapi, httpx,
aiohttp или драйвера БД внутри app/core или app/ports валит тест. Это та самая
защита от дублирования продуктовой логики по адаптерам, о которой говорит §8
задания.
"""

from __future__ import annotations

import ast
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
