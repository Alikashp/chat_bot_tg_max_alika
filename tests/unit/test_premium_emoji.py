"""Премиальные эмодзи Telegram: разметка обычного символа анимированным.

Главное, что здесь доказывается, — смещения. Telegram считает их в кодовых
единицах UTF-16, а Python — в кодовых точках, и ошибка на единицу подсвечивает
соседний символ вместо эмодзи. Проверяется это не сверкой чисел с ожидаемыми,
а обратным раскодированием: по смещению и длине достаём кусок текста и
смотрим, что накрыт ровно тот эмодзи.
"""

from __future__ import annotations

from aiogram.types import MessageEntity

from app.adapters.telegram.emoji import entities

LOADING = "5345906554510012647"
HEART = "6037249452824072506"


def _covered(text: str, entity: MessageEntity) -> str:
    """Что именно накрыла сущность — по правилам Telegram, в UTF-16."""
    units = text.encode("utf-16-le")
    start = entity.offset * 2
    return units[start : start + entity.length * 2].decode("utf-16-le")


def test_nothing_configured_means_nothing_changes() -> None:
    """Пустая настройка — сообщения уходят ровно как раньше."""
    assert entities("🔄 Делаю… ~15 сек", {}) is None


def test_a_text_without_known_emoji_is_left_alone() -> None:
    assert entities("Делаю… ~15 сек", {"🔄": LOADING}) is None


def test_the_entity_covers_exactly_the_emoji() -> None:
    """Сущность обязана накрывать ровно один эмодзи, иначе Telegram её выбросит."""
    text = "🔄 Делаю… ~15 сек"

    found = entities(text, {"🔄": LOADING})

    assert found is not None and len(found) == 1
    assert _covered(text, found[0]) == "🔄"
    assert found[0].custom_emoji_id == LOADING


def test_an_emoji_after_text_is_found_at_the_right_offset() -> None:
    """Ровно то место, где ошибка в кодовых точках сдвинула бы разметку."""
    text = "Готово 🔄 совсем"

    found = entities(text, {"🔄": LOADING})

    assert found is not None
    assert _covered(text, found[0]) == "🔄"


def test_an_unknown_emoji_before_a_known_one_does_not_shift_it() -> None:
    """Единственное место, где кодовые точки и UTF-16 расходятся на деле.

    Обычные буквы занимают в UTF-16 столько же, сколько в кодовых точках, и на
    них ошибка не видна. А вот чужой эмодзи впереди занимает две единицы вместо
    одной — и разметка съезжает на него самого.
    """
    text = "🎭 Готово 🔄"

    found = entities(text, {"🔄": LOADING})

    assert found is not None and len(found) == 1
    assert _covered(text, found[0]) == "🔄"


def test_an_emoji_with_a_variation_selector_is_covered_whole() -> None:
    """«❤️» — это символ плюс модификатор начертания, а не один знак.

    Накрыв только первую половину, мы оставили бы модификатор висеть отдельно:
    человек увидел бы анимацию и рядом с ней осколок исходного эмодзи.
    """
    text = "❤️ Половинки"

    found = entities(text, {"❤️": HEART})

    assert found is not None and len(found) == 1
    assert _covered(text, found[0]) == "❤️"


def test_a_longer_key_wins_over_its_own_prefix() -> None:
    """Если известны и «❤», и «❤️», брать надо длинный — иначе тот же осколок."""
    text = "❤️ Половинки"

    found = entities(text, {"❤": "111", "❤️": HEART})

    assert found is not None and len(found) == 1
    assert _covered(text, found[0]) == "❤️"
    assert found[0].custom_emoji_id == HEART


def test_every_known_emoji_in_the_text_is_marked() -> None:
    text = "🔄 Делаю ❤️ ещё"

    found = entities(text, {"🔄": LOADING, "❤️": HEART})

    assert found is not None and len(found) == 2
    assert [_covered(text, one) for one in found] == ["🔄", "❤️"]


def test_the_same_emoji_twice_is_marked_twice() -> None:
    """Оба вхождения — каждое со своим смещением."""
    text = "🔄 и ещё 🔄"

    found = entities(text, {"🔄": LOADING})

    assert found is not None and len(found) == 2
    assert [_covered(text, one) for one in found] == ["🔄", "🔄"]
