"""Реестр пресетов обработки фото — главный виральный крючок (§2.4).

Пресет здесь — запись в реестре, а не отдельный обработчик. Добавление
пресета должно сводиться к добавлению записи в PRESETS: никакого нового кода
в сценариях, адаптерах или клавиатурах. Это проверяется тестом (критерий
приёмки A1).

Поле instruction — единственное место в проекте, где текст пишется для
провайдера, а не для человека. Пользователь его никогда не видит: он видит
только button и invitations, и на них распространяются все правила §2.9,
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

    #: Приглашения прислать фото — по одному на каждое нужное фото, в том
    #: порядке, в каком они уедут провайдеру.
    #:
    #: Кортеж, а не строка рядом с числом «сколько фото нужно»: два поля
    #: разошлись бы на первой же правке, и бот попросил бы второе фото, не
    #: зная, какими словами. Порядок здесь существенный — см. instruction у
    #: polaroid_child.
    invitations: tuple[str, ...]

    #: Инструкция провайдеру. Не показывается пользователю.
    instruction: str

    #: Доступен только на платном тарифе. В меню такой пресет показывается
    #: всем и с замком: скрывать его значило бы не продавать подписку, а
    #: прятать причину её купить.
    paid_only: bool = False

    def __post_init__(self) -> None:
        if not self.invitations:
            raise ValueError(f"пресет {self.id}: нужно хотя бы одно приглашение")

    @property
    def photos_required(self) -> int:
        """Сколько фото нужно собрать перед обработкой."""
        return len(self.invitations)


PRESETS: Mapping[str, Preset] = MappingProxyType(
    {
        "lego": Preset(
            id="lego",
            button="🧱 Лего",
            invitations=("Кинь фото — сделаю из тебя лего",),
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
            invitations=("Кинь фото — подселим соседа",),
            instruction=(
                "Add a scruffy, comically dishevelled homeless-looking man "
                "standing next to the person in this photo, as if they were "
                "posing together. Keep the original person unchanged and match "
                "the lighting and perspective of the scene."
            ),
        ),
        "id_photo": Preset(
            id="id_photo",
            button="🪪 Фото на документы",
            invitations=("Кинь селфи — сделаю фото на пропуск или резюме",),
            instruction=(
                "Turn this photo into a professional ID portrait. Plain light grey "
                "seamless studio background, even soft frontal lighting, neutral calm "
                "expression, head and shoulders framing, straight posture facing the "
                "camera, sharp focus, subject wearing a plain dark shirt or blouse. "
                "Preserve the person's exact face, facial features, skin tone and "
                "hairstyle without any beautification, retouching or age change. The "
                "result must be a realistic photograph, not an illustration or render."
            ),
        ),
        "figurine": Preset(
            id="figurine",
            button="🧸 Фигурка в коробке",
            invitations=("Кинь фото — сделаю коллекционную фигурку с тобой",),
            paid_only=True,
            instruction=(
                "Turn the person in this photo into a 1/7 scale commercialized "
                "collectible figurine placed on a computer desk. The figurine stands "
                "on a round transparent acrylic base with no text. Next to it, a "
                "premium toy packaging box printed with the character artwork. Behind "
                "it, a computer monitor showing the 3D model of the figurine in "
                "modeling software. Merchandise product photography, soft studio "
                "lighting, shallow depth of field. "
                "Preserve the person's exact facial features, face shape, hairstyle, "
                "skin tone and clothing so they remain clearly recognisable. Do not "
                "beautify, do not change the face structure, do not alter their age."
            ),
        ),
        "polaroid_child": Preset(
            id="polaroid_child",
            button="📷 Я и я в детстве",
            # Взрослое фото первым, и это не вкусовщина: провайдер применяет
            # высокую точность ко всем исходникам, но дополнительную
            # детализацию текстуры — только к первому. Детские снимки обычно
            # хуже качеством, и вытянуть их всё равно не выйдет.
            invitations=(
                "Кинь два фото: своё сейчас и детское",
                "Отлично. Теперь кинь детское фото 👶",
            ),
            paid_only=True,
            instruction=(
                "Create a single candid Polaroid instant photo showing the adult from "
                "image 1 and the child from image 2 standing together, hugging and "
                "smiling at the camera, as if photographed in the same room at the "
                "same moment. Iconic white Polaroid border, slight overexposure, muted "
                "retro colours, soft focus, warm indoor light, subtle film grain. "
                "Preserve both faces exactly as they are in the source images so both "
                "people remain clearly recognisable. Do not beautify, do not change "
                "face structure or age."
            ),
        ),
    }
)
