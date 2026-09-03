"""Ожидание фото под прикол — состояние, которое переживает выкатку.

Проверяется главным образом одно: разбор не должен зависеть от того, что
именно мессенджер положил в ссылку на фото. В Telegram это file_id, в MAX —
обычный http-адрес с двоеточиями и косыми чертами, и обе строки приходят к
нам как есть.
"""

from __future__ import annotations

import pytest

from app.core.pending import (
    AWAIT_IMAGE_PROMPT,
    AwaitedPreset,
    await_preset,
    is_awaiting_image_prompt,
    parse_await_preset,
)

#: Настоящая ссылка на фото в MAX: с двоеточием, косыми чертами и параметрами.
MAX_LINK = "https://cdn.max.ru/photo/12345?sig=ab%2Fcd:1&expires=1780000000"


def test_a_preset_without_photos_survives_the_round_trip() -> None:
    assert parse_await_preset(await_preset("lego")) == AwaitedPreset("lego")


def test_a_link_with_colons_and_slashes_survives_the_round_trip() -> None:
    """Разделителем не может быть символ, который бывает внутри ссылки."""
    stored = await_preset("polaroid_child", (MAX_LINK, "AgACAgIAAxkBAAI"))

    assert parse_await_preset(stored) == AwaitedPreset(
        "polaroid_child", (MAX_LINK, "AgACAgIAAxkBAAI")
    )


def test_the_order_of_photos_is_kept() -> None:
    """Первым уезжает взрослый снимок — переставить их значит получить другое."""
    parsed = parse_await_preset(await_preset("polaroid_child", ("adult", "child")))

    assert parsed is not None
    assert parsed.collected == ("adult", "child")


def test_waiting_for_a_description_is_not_waiting_for_a_photo() -> None:
    assert parse_await_preset(AWAIT_IMAGE_PROMPT) is None
    assert is_awaiting_image_prompt(AWAIT_IMAGE_PROMPT)
    assert not is_awaiting_image_prompt(await_preset("lego"))


@pytest.mark.parametrize(
    "stored",
    [None, "", "await:preset:", "await:preset:lego\n", "что-то чужое"],
)
def test_a_broken_state_is_read_as_no_waiting(stored: str | None) -> None:
    """Значение могло остаться от прошлой версии формата: гадать тут нечего."""
    assert parse_await_preset(stored) is None


def test_a_link_with_a_newline_is_refused_at_the_door() -> None:
    """Иначе одна ссылка разобралась бы обратно как две."""
    with pytest.raises(ValueError):
        await_preset("lego", ("сначала\nпотом",))


def test_a_preset_without_an_identifier_is_refused() -> None:
    with pytest.raises(ValueError):
        await_preset("")
