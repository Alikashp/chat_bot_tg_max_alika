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
                "Turn the person in the uploaded photo into a highly detailed "
                "LEGO-style minifigure while keeping them clearly recognizable. "
                "Preserve the person's hairstyle, hair color, key facial traits, skin "
                "tone, clothing colors, outfit details, pose and overall vibe. The "
                "character must clearly look like a real collectible minifigure with "
                "classic blocky proportions, cylindrical hands, a glossy molded "
                "plastic body, detailed sculpted plastic hair matching the person's "
                "hairstyle, an expressive printed LEGO-style face based on their real "
                "features, and clothing recreated as detailed printed and molded toy "
                "pieces. Adapt shoes and small outfit details into LEGO form. Keep the "
                "original background recognizable, but make the LEGO figure the main "
                "focus. Use realistic glossy plastic materials, subtle reflections, "
                "detailed molded surfaces, premium collectible-toy rendering and "
                "cinematic lighting. The result should look like a high-end custom "
                "LEGO minifigure photographed in the real scene, not a simple generic "
                "LEGO character and not a human with LEGO-like features."
            ),
        ),
        "bad_day": Preset(
            id="bad_day",
            button="🏚 Плохой день",
            invitations=("Кинь фото — подселим соседа",),
            instruction=(
                "Add exactly one tired, unkempt, homeless-looking man naturally placed somewhere "
                "in the existing scene. He may be standing or sitting, whichever fits the "
                "composition better. Do not add any other people. Make him look like a real "
                "ordinary person who has had a hard time, not a horror character. He should have "
                "a natural human face, normal anatomy, and believable proportions. He may look "
                "poor, slightly dishevelled, and wear worn, modest clothes, but keep him visually "
                "realistic and not extreme. Do not make him grotesque, frightening, severely dirty, "
                "deformed, sick-looking, or low-quality. Do not add injuries, blood, missing teeth, "
                "exaggerated grime, extreme raggedness, or a disturbing appearance. He must not "
                "smoke or hold cigarettes, alcohol, drugs, weapons, or other inappropriate objects. "
                "Change only this. Preserve all existing people, faces, poses, clothing, furniture, "
                "objects, room layout, and background exactly as they are. If the source image "
                "contains no people, keep it that way except for this single added man. Match him "
                "carefully to the photo's lighting, perspective, shadows, camera quality, focus, "
                "and grain so he looks naturally present in the original image."
            ),
        ),
        # Замок тут не только про деньги. Портрет живёт узнаваемостью, а
        # отрисовать лицо модель может лишь теми пикселями, которые ей
        # оплачены: на бесплатном тарифе мы рисуем в low, и черты уплывают
        # ещё до всякой инструкции. Платный тариф даёт medium — и заодно
        # платит за него.
        "id_photo": Preset(
            id="id_photo",
            button="🪪 Фото на документы",
            paid_only=True,
            # Про «прямо в камеру» сказано человеку не для красоты. Развернуть
            # голову на снимке невозможно, не дорисовав ракурс, которого там
            # нет, — а дорисованный ракурс это уже другое лицо. Значит, кадр
            # анфас должен прийти от человека, и попросить его надо словами.
            invitations=(
                "Кинь селфи прямо в камеру — сделаю фото на пропуск или резюме",
            ),
            # Инструкция устроена как наряд ретушёру, а не как задание
            # художнику: сначала закрытый список того, что менять, потом «всё
            # остальное не трогать».
            #
            # Про позу здесь нет ни слова, и это не упущение. Просить
            # «развернись к камере и смотри в объектив» значит просить
            # дорисовать ракурс, которого на снимке нет: под новым углом
            # модель заново придумывает форму скул, носа и разрез глаз — и
            # человек получает обратно чужое лицо, как бы старательно мы ни
            # перечисляли рядом «сохрани черты». Кадр анфас должен прийти от
            # человека, поэтому про него сказано в приглашении, а не тут.
            instruction=(
                "Сделай профессиональное фото на документы по исходной фотографии. "
                "Главный приоритет — максимальное сходство с человеком на исходнике. "
                "Это должен быть тот же человек. Сохрани форму и пропорции лица, "
                "глаза, нос, губы, брови, челюсть, подбородок, скулы, возраст и "
                "индивидуальные черты. Не меняй заметно внешность и не создавай новое "
                "лицо. Не делай сильный beauty-фильтр, пластиковую кожу, глянцевую "
                "ретушь или модельную внешность. Допускается только лёгкое "
                "естественное улучшение: немного выровнять тон кожи, уменьшить "
                "временные покраснения, слегка смягчить круги под глазами и аккуратно "
                "убрать выбившиеся волосы. Сделай чистый светлый нейтральный фон, "
                "мягкий студийный свет и аккуратный вид фото на документы. При "
                "необходимости приведи одежду к нейтральному аккуратному виду для "
                "официального фото. Результат должен выглядеть как реальная "
                "фотография, снятая профессиональной камерой: натуральная кожа, "
                "хорошая резкость, естественный свет, без ощущения AI-генерации."
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
                "Using the uploaded photo as reference, create a premium stylized "
                "fashion doll based on the person. The result must clearly look like a "
                "manufactured collectible doll, not a real person standing inside "
                "packaging. Keep the person recognizable through their hairstyle, hair "
                "color, key facial traits, skin tone, outfit and overall vibe, but "
                "stylize them into a polished vinyl fashion doll with a slightly "
                "oversized head, larger expressive eyes, simplified facial features, "
                "smooth sculpted vinyl skin, a slim stylized body, and molded toy-like "
                "hair. Base the doll's outfit and colors only on what the person is "
                "actually wearing in the uploaded photo. Include only 0-3 separate "
                "accessories that are clearly visible in the source photo, such as "
                "glasses, jewelry, a bag, phone, headphones or similar personal items. "
                "Do not place clothing items, spare outfits, shirts, pants, shoes or "
                "duplicates of what the doll is already wearing beside the figure. "
                "Never invent accessories. If no clear accessories are visible, show "
                "only the doll with no accessory section and no empty slots. Create a "
                "beautiful premium fashion-doll collector box with an elegant luxury "
                "design, rigid matte materials, a deep molded interior, refined "
                "metallic accents and polished retail presentation. The doll should "
                "fill most of the package vertically. The box should fill almost the "
                "entire image with very little or no visible background around the "
                "edges. Use a front-facing, tightly framed professional product shot. "
                "Use only subtle transparent plastic where necessary, with no large "
                "obvious blister shell. No text, logos, numbers or fake branding. "
                "High-end collectible toy photography, polished commercial lighting, "
                "premium materials, visually striking, social-media-ready."
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
                "Create a realistic, cozy Polaroid-style photo showing the same person "
                "as an adult and as a child sitting together at one table. Image 1 is "
                "the main identity reference. Image 2 is the child reference for age, "
                "clothing, and expression. The adult must clearly look like the person "
                "from image 1. The child must look like a believable younger version "
                "of that same person, while keeping the childlike age and feel of "
                "image 2. Preserve matching identity traits across both ages, "
                "including face shape, eyes, nose, eyebrows, smile, skin tone, and "
                "overall facial character. Do not heavily beautify and do not turn "
                "them into generic faces. Create a warm, cozy home interior with a "
                "wooden table, soft blurred background, and a chocolate birthday cake "
                "with several thin lit candles. The adult rests their chin on one hand "
                "and looks at the child with a tender smile. The child looks back "
                "warmly. The mood should feel intimate, nostalgic, and emotional. The "
                "final image should look like a real printed Polaroid instant photo "
                "with a classic white frame, a slightly thicker bottom border, soft "
                "warm tones, a subtle vintage feel, slight film texture, and a "
                "realistic instant-photo look. Keep the composition clean and centered "
                "inside the Polaroid frame. No text, letters, numbers, logos, or "
                "handwriting anywhere, including on the Polaroid border."
            ),
        ),
    }
)
