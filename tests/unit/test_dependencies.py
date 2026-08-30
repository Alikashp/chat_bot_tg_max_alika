"""Всё, что приложение импортирует, должно стоять в его зависимостях.

Проверка появилась после настоящего сбоя: httpx лежал только в dev-секции,
поэтому все тесты проходили, образ собирался — и падал в проде на первом же
импорте. Разработчику этот класс ошибок не виден вообще: у него в окружении
стоят и рабочие зависимости, и тестовые.
"""

from __future__ import annotations

import ast
import sys
import tomllib
from importlib.metadata import packages_distributions
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

#: Пакеты приложения. Их импорты друг на друга зависимостями не являются.
OWN_PACKAGES = frozenset({"app", "config", "tests", "scripts", "migrations"})

#: Где живёт код, который поедет в прод. Миграции здесь не для полноты:
#: они накатываются при старте приложения, в том же процессе.
RUNTIME_PACKAGES = ("app", "config", "migrations")


def _declared_runtime_dependencies() -> set[str]:
    """Имена из [project] dependencies, без версий и extras."""
    manifest = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    names: set[str] = set()
    for requirement in manifest["project"]["dependencies"]:
        name = requirement.split(";")[0]
        for separator in ("[", "=", ">", "<", "!", "~", " "):
            name = name.split(separator)[0]
        names.add(_normalise(name))
    return names


def _normalise(name: str) -> str:
    """Имена пакетов сравниваются без учёта регистра, дефисов и подчёркиваний."""
    return name.lower().replace("_", "-")


def _roots_of(node: ast.AST) -> set[str]:
    """Верхний уровень импорта. Относительные импорты — свои, они не в счёт."""
    if isinstance(node, ast.Import):
        return {alias.name.split(".")[0] for alias in node.names}
    if isinstance(node, ast.ImportFrom) and node.module and not node.level:
        return {node.module.split(".")[0]}
    return set()


def _imported_roots() -> set[str]:
    roots: set[str] = set()
    for package in RUNTIME_PACKAGES:
        for path in (ROOT / package).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                roots |= _roots_of(node)
    return {
        root
        for root in roots
        if root not in OWN_PACKAGES and root not in sys.stdlib_module_names
    }


def _distributions_for(module: str) -> set[str]:
    """Какие пакеты дают этот модуль. Через метаданные, а не по имени модуля.

    Имя модуля и имя пакета совпадают не всегда: pydantic_settings приезжает
    из pydantic-settings, а sqlalchemy — из SQLAlchemy.
    """
    return {_normalise(name) for name in packages_distributions().get(module, [])}


def test_something_is_actually_imported() -> None:
    """Страховка от проверки, которая проходит, потому что ничего не нашла."""
    assert len(_imported_roots()) >= 5


@pytest.mark.parametrize("module", sorted(_imported_roots()))
def test_imported_module_is_a_runtime_dependency(module: str) -> None:
    declared = _declared_runtime_dependencies()
    distributions = _distributions_for(module) or {_normalise(module)}

    assert distributions & declared, (
        f"{module} импортируется в приложении, но его пакета "
        f"({', '.join(sorted(distributions))}) нет в [project] dependencies"
    )
