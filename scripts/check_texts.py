#!/usr/bin/env python
"""Линтер текстов интерфейса — критерий приёмки №11.

Проверяет правила §2.9 задания механически, а не на глаз. Правила простые, но
именно поэтому их легко нарушить в спешке: слово «генерация» само просится в
текст про картинки, а «Вы» — в вежливую формулировку.

Проверяются два источника: реестр экранов из app/core/texts.py и реестр
пресетов из config/presets.py. Пресеты — точка расширения, куда тексты
попадают в обход texts.py, поэтому оставить их без проверки нельзя.

Запуск: python scripts/check_texts.py
Возвращает 1, если хоть одно правило нарушено. Стоит в CI.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass

sys.path.insert(0, ".")

from app.core.texts import SCREENS, Screen
from config.presets import PRESETS

#: Слова, которых в интерфейсе быть не должно (§2.9).
#:
#: Сравниваем по основам, а не по точным словам: запрещена «генерация», но
#: «генерируем» и «сгенерировано» ничем не лучше. Пользователь не обязан
#: знать нашу внутреннюю кухню — он пришёл за картинкой, а не за нейросетью.
FORBIDDEN_STEMS = (
    "кредит",
    "токен",
    "нейросет",
    "промпт",
    "генерац",
    "генерир",
)

#: Обращение только на «ты» (§2.9). Ищем формы «вы» по границам слова, иначе
#: под запрет попали бы «выбери» и «выход».
FORMAL_ADDRESS = re.compile(
    r"\b(вы|вас|вам|вами|ваш|ваша|ваше|ваши|вашего|вашей|вашему|вашим|ваших|вашу)\b",
    re.IGNORECASE,
)

#: Не длиннее пяти строк (§2.9). Простыни в мессенджере не читают.
#:
#: Экран может поднять этот потолок сам (Screen.max_lines) — но только явно и
#: с объяснением: витрина тарифов существует ради сравнения, а сравнивать
#: по пять строк нечего. Тест следит, чтобы исключений не стало больше.
MAX_LINES = 5


@dataclass(frozen=True, slots=True)
class Violation:
    """Одно нарушение с указанием, где именно."""

    where: str
    rule: str
    detail: str

    def __str__(self) -> str:
        return f"  [{self.rule}] {self.where}: {self.detail}"


def _label(text: str) -> str:
    """Короткая узнаваемая метка текста для отчёта."""
    first_line = text.split("\n")[0].strip()
    if not first_line:
        return "(без текста)"
    return first_line[:60] + ("…" if len(first_line) > 60 else "")


def check_wording(where: str, text: str) -> list[Violation]:
    """Запрещённые слова и обращение на «вы»."""
    violations: list[Violation] = []
    lowered = text.lower()

    violations.extend(
        Violation(where, "запрещённое слово", f"встречается «{stem}»")
        for stem in FORBIDDEN_STEMS
        if stem in lowered
    )

    formal = FORMAL_ADDRESS.search(text)
    if formal is not None:
        violations.append(
            Violation(
                where,
                "обращение",
                f"«{formal.group(0)}» — обращаемся только на «ты»",
            )
        )
    return violations


def check_screen(screen: Screen) -> list[Violation]:
    """Все правила для одного экрана."""
    where = _label(screen.text) if screen.text else _label(", ".join(screen.buttons))
    violations = check_wording(where, screen.text)

    for button in screen.buttons:
        violations.extend(check_wording(f"{where} → кнопка «{button}»", button))

    if len(screen.lines) > screen.max_lines:
        violations.append(
            Violation(
                where,
                "длина",
                f"{len(screen.lines)} строк, допускается не больше {screen.max_lines}",
            )
        )

    if not screen.buttons and not screen.next_step:
        violations.append(
            Violation(
                where,
                "тупик",
                "нет ни кнопок, ни описанного следующего шага",
            )
        )

    return violations


def check_presets() -> list[Violation]:
    """Тексты пресетов — та же планка, что и у остальных экранов."""
    violations: list[Violation] = []
    for preset in PRESETS.values():
        where = f"пресет {preset.id}"
        violations.extend(check_wording(f"{where} → кнопка", preset.button))
        violations.extend(check_wording(f"{where} → приглашение", preset.invitation))
        if not preset.invitation.strip():
            violations.append(
                Violation(where, "тупик", "нет приглашения прислать фото")
            )
    return violations


def collect() -> list[Violation]:
    """Собирает нарушения по всем источникам текстов."""
    violations: list[Violation] = []
    for screen in SCREENS:
        violations.extend(check_screen(screen))
    violations.extend(check_presets())
    return violations


def report(violations: Iterable[Violation], checked: int) -> bool:
    """Печатает отчёт. Возвращает True, если всё чисто."""
    found = list(violations)
    if not found:
        print(f"Тексты в порядке: проверено экранов и пресетов — {checked}.")
        return True

    print(f"Нарушений: {len(found)}\n")
    for violation in found:
        print(violation)
    print("\nПравила — в docs/spec.md §5 (§2.9 задания).")
    return False


def main() -> int:
    checked = len(SCREENS) + len(PRESETS)
    return 0 if report(collect(), checked) else 1


if __name__ == "__main__":
    sys.exit(main())
