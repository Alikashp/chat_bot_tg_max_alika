"""Действия, которые пользователь может совершить.

Единый реестр на оба мессенджера. Ядро оперирует этими значениями, а как
именно они доезжают до бота — забота адаптера: в Telegram постоянная
клавиатура возвращает подпись кнопки текстом, а inline-кнопка — callback_data;
в MAX постоянных клавиатур нет вовсе, и всё приходит payload'ом
(docs/research.md §1.6).

Значения короткие намеренно: в Telegram callback_data ограничен 64 байтами,
и в этот предел должен помещаться идентификатор пресета вместе с префиксом.
"""

from __future__ import annotations

from enum import StrEnum


class Action(StrEnum):
    """Действие без параметров."""

    # Постоянное меню (§2.1)
    MENU_IMAGES = "m:img"
    MENU_PRESETS = "m:fun"
    MENU_PROFILE = "m:me"
    MENU_TARIFFS = "m:pay"

    # Чат (§2.2)
    CHAT_RETRY = "c:retry"
    CHAT_NEW_DIALOG = "c:new"

    # Картинки (§2.3)
    IMAGE_AGAIN = "i:again"
    IMAGE_SHARE = "i:share"
    IMAGE_RETRY = "i:retry"

    # Пресеты (§2.4)
    PRESET_AGAIN = "p:again"
    PRESET_SHARE = "p:share"
    PRESET_ANOTHER = "p:other"
    PRESET_RETRY = "p:retry"

    # Пейволл и тарифы (§2.5, §2.8)
    OPEN_TARIFFS = "t:open"
    INVITE_FRIEND = "r:invite"
    MY_LINK = "r:link"
    REFERRAL_SEND = "r:send"


#: Префикс выбора пресета. За ним идёт идентификатор из реестра.
PRESET_PREFIX = "p:pick:"

#: Префикс покупки тарифа. За ним идёт идентификатор тарифа.
BUY_PREFIX = "t:buy:"

#: Префикс выбора способа оплаты: за ним «способ:тариф».
METHOD_PREFIX = "t:pay:"


def preset_action(preset_id: str) -> str:
    """Действие «выбран такой-то пресет»."""
    return f"{PRESET_PREFIX}{preset_id}"


def parse_preset_action(action: str) -> str | None:
    """Достаёт идентификатор пресета из действия; None — если это не оно."""
    if not action.startswith(PRESET_PREFIX):
        return None
    return action.removeprefix(PRESET_PREFIX) or None


def method_action(method: str, tariff_id: str) -> str:
    """Действие «оплатить такой-то тариф таким-то способом»."""
    return f"{METHOD_PREFIX}{method}:{tariff_id}"


def parse_method_action(action: str) -> tuple[str, str] | None:
    """Достаёт пару «способ, тариф»; None — если это не оно."""
    if not action.startswith(METHOD_PREFIX):
        return None
    method, _, tariff_id = action.removeprefix(METHOD_PREFIX).partition(":")
    if not method or not tariff_id:
        return None
    return method, tariff_id


def buy_action(tariff_id: str) -> str:
    """Действие «выбран такой-то тариф»."""
    return f"{BUY_PREFIX}{tariff_id}"


def parse_buy_action(action: str) -> str | None:
    """Достаёт идентификатор тарифа из действия; None — если это не оно."""
    if not action.startswith(BUY_PREFIX):
        return None
    return action.removeprefix(BUY_PREFIX) or None
