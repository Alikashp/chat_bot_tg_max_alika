"""Премиальные эмодзи Telegram: обычный символ → анимированный аналог.

Знание чисто телеграмное и потому живёт здесь, а не в текстах. В ядре
``texts.py`` остаётся обычный эмодзи, и в MAX человек видит именно его: там
премиальных эмодзи нет вообще, и подставлять туда телеграмную разметку значило
бы показать людям сырые теги.

Подмена делается через ``entities``, а не через ``parse_mode=HTML``. Разметку
мы не включаем нигде: стоит включить её ради одной иконки — и каждый текст
на каждом экране придётся экранировать, иначе первая же угловая скобка в
сообщении сломает отправку. ``entities`` даёт то же самое и не трогает ничего
из работающего.
"""

from __future__ import annotations

from collections.abc import Mapping

from aiogram.types import MessageEntity

#: Тип сущности, которым Telegram размечает премиальный эмодзи.
_CUSTOM_EMOJI = "custom_emoji"


def entities(text: str, premium: Mapping[str, str]) -> list[MessageEntity] | None:
    """Размечает в тексте эмодзи, у которых есть премиальный аналог.

    None означает «размечать нечего» — и тогда сообщение уходит ровно так же,
    как уходило до всей этой затеи.

    Смещения и длины Telegram считает **в кодовых единицах UTF-16**, а Python
    считает строку в кодовых точках. Для большинства эмодзи это разные числа:
    они лежат за пределами основной плоскости и занимают в UTF-16 две единицы
    вместо одной. Сместиться здесь на единицу — значит подсветить соседний
    символ вместо эмодзи, поэтому позиция считается пересчётом в UTF-16, а не
    индексом строки.
    """
    if not premium:
        return None

    # Длинные ключи вперёд: «❤️» — это «❤» плюс модификатор начертания, и,
    # проверив короткий ключ первым, мы разметили бы только первую половину,
    # а модификатор остался бы висеть отдельным символом.
    keys = sorted(premium, key=len, reverse=True)

    found: list[MessageEntity] = []
    offset = 0
    position = 0
    while position < len(text):
        match = next((key for key in keys if text.startswith(key, position)), None)
        if match is None:
            offset += _utf16_length(text[position])
            position += 1
            continue

        length = _utf16_length(match)
        # Сущность обязана накрывать ровно один обычный эмодзи: иначе Telegram
        # молча выбрасывает её и человек видит исходный символ.
        found.append(
            MessageEntity(
                type=_CUSTOM_EMOJI,
                offset=offset,
                length=length,
                custom_emoji_id=premium[match],
            )
        )
        offset += length
        position += len(match)
    return found or None


def _utf16_length(text: str) -> int:
    """Длина в кодовых единицах UTF-16 — в них Telegram считает смещения."""
    return len(text.encode("utf-16-le")) // 2
