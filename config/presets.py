"""Реестр пресетов обработки фото — главный виральный крючок (§2.4).

Пресет здесь — запись в реестре, а не отдельный обработчик. Добавление
третьего пресета должно сводиться к добавлению записи в PRESETS: никакого
нового кода в сценариях, адаптерах или клавиатурах. Это проверяется тестом
(критерий приёмки A1).

Поле instruction — единственное место в проекте, где текст пишется для
провайдера, а не для человека. Пользователь его никогда не видит: он видит
только button и invitation, и на них распространяются все правила §2.9,
что проверяет scripts/check_texts.py.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True, slots=True)
class Preset:
    """Один пресет обработки фото."""

    #: Устойчивый идентификатор. Попадает в данные кнопок, поэтому менять его
    #: у выпущенного пресета нельзя: у людей в переписке останутся старые
    #: кнопки, и они перестанут работать.
    id: str

    #: Подпись кнопки в меню приколов.
    button: str

    #: Приглашение прислать фото.
    invitation: str

    #: Инструкция провайдеру. Не показывается пользователю.
    instruction: str


PRESETS: Mapping[str, Preset] = MappingProxyType(
    {
        "lego": Preset(
            id="lego",
            button="🧱 Лего",
            invitation="Кинь фото — сделаю из тебя лего",
            instruction=(
                "Turn the person in this photo into a LEGO minifigure. "
                "Keep the pose, clothing colours and background recognisable, "
                "render the character in glossy plastic LEGO style with "
                "cylindrical hands and a classic minifigure head."
            ),
        ),
        "bad_day": Preset(
            id="bad_day",
            button="🏚 Плохой день",
            invitation="Кинь фото — подселим соседа",
            instruction=(
                "Add a scruffy, comically dishevelled homeless-looking man "
                "standing next to the person in this photo, as if they were "
                "posing together. Keep the original person unchanged and match "
                "the lighting and perspective of the scene."
            ),
        ),
    }
)


def preset_buttons() -> tuple[str, ...]:
    """Подписи кнопок в порядке реестра — для меню приколов."""
    return tuple(preset.button for preset in PRESETS.values())


def preset_by_button(button: str) -> Preset | None:
    """Находит пресет по подписи кнопки."""
    for preset in PRESETS.values():
        if preset.button == button:
            return preset
    return None
