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
            # Первое слово инструкции решает больше, чем всё остальное. «Turn
            # this photo into» модель читает как «нарисуй заново», и человек
            # получает чужое лицо на белом фоне. «Retouch, keeping the same
            # photograph» ставит задачу как обработку — а обрабатывать можно
            # только то, что уже есть.
            instruction=(
                "Retouch this photograph into an official document portrait, "
                "keeping it the same photograph of the same person. "
                "Replace the background with a perfectly even, plain white backdrop. "
                "Remove every shadow from the face and the neck. Even, soft, frontal "
                "studio lighting. Head and shoulders framing, straight posture, "
                "shoulders square to the camera, eyes looking directly into the lens, "
                "neutral calm expression with the mouth closed. Sharp focus, natural "
                "skin texture. "
                "Do not redraw the person. Keep exactly the same face: identical "
                "facial features, face shape, bone structure, eyes, nose, mouth, "
                "eyebrows, skin tone, skin texture, moles and hairstyle. No "
                "beautification, no smoothing, no slimming, no makeup added or "
                "removed, no change of age. "
                "The result must be an ordinary photograph, not an illustration, "
                "a painting or a 3D render."
            ),
        ),
        "figurine": Preset(
            id="figurine",
            button="🧸 Фигурка в коробке",
            invitations=("Кинь фото — сделаю коллекционную фигурку с тобой",),
            paid_only=True,
            # Надписей на упаковке нет намеренно. Имя человека мы не
            # спрашиваем, а буквы модели рисуют плохо: вместо подписи выходит
            # набор похожих на буквы закорючек, и премиальная коробка сразу
            # выглядит подделкой. Наряд фигурки берётся с самого фото —
            # так прикол работает и для футболиста, и для кого угодно.
            instruction=(
                "Using the uploaded photograph as the reference, turn the person "
                "into an original premium collectible toy sealed inside a blister "
                "package, photographed as shelf-ready merchandise. "
                "Base the figure's outfit, colours and accessories on what the "
                "person is actually wearing in the photograph, restyled as "
                "collectible merchandise. "
                "Modern collectible-toy aesthetic: glossy blister plastic, matte "
                "cardboard backing, smooth vinyl textures, moulded plastic surfaces. "
                "Hair is moulded plastic with large sculpted grooves rather than "
                "separate strands. Realistic reflections with soft highlights, "
                "premium shelf-ready look, soft studio lighting, shallow depth of "
                "field. The cardboard backing carries only abstract graphic "
                "decoration: no lettering, no words, no numbers and no logos "
                "anywhere on the package. "
                "Keep the person's real face: the same facial features, face shape, "
                "skin tone and hairstyle silhouette, so the figure stays clearly "
                "recognisable as them. Do not beautify, do not change the face "
                "structure, do not alter their age. "
                "Highly realistic digital render, portrait framing."
            ),
        ),
        # Идентификатор остался от полароида, которым прикол был поначалу.
        # Менять его нельзя: он лежит в данных кнопок, а те живут у людей в
        # переписке вечно и после переименования перестали бы работать.
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
            # Сцена вместо полароида. Полароидная рамка с зерном и мягким
            # фокусом делала ровно то, чего тут делать нельзя: размывала оба
            # лица, ради узнаваемости которых прикол и существует. Спокойная
            # студийная съёмка за столом лица сохраняет.
            instruction=(
                "Combine the two source photographs into a single realistic studio "
                "photograph: the adult from image 1 and the child from image 2 "
                "sitting side by side at the same table, as if they had been "
                "photographed together in the same room at the same moment. "
                "On the table in front of them, a chocolate birthday cake with "
                "several thin lit candles. Plain beige seamless backdrop, warm soft "
                "daylight from the side, muted natural colours, shallow depth of "
                "field, calm and tender mood. The adult rests their chin on one hand "
                "and looks at the child with a soft smile; the child looks back at "
                "the adult. "
                "Keep both faces exactly as they are in the source photographs: "
                "identical facial features, face shape, skin tone and hairstyle, so "
                "both people stay clearly recognisable. The child stays a child and "
                "the adult stays an adult. Do not beautify, do not change face "
                "structure or age. "
                "No text, no lettering and no numbers anywhere in the image, "
                "including on the cake and the candles."
            ),
        ),
    }
)
