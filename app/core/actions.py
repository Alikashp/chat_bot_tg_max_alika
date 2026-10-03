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

from dataclasses import dataclass
from enum import StrEnum


class Action(StrEnum):
    """Действие без параметров."""

    # Постоянное меню (§2.1)
    MENU_IMAGES = "m:img"
    MENU_PRESETS = "m:fun"
    MENU_PROFILE = "m:me"
    MENU_TARIFFS = "m:pay"
    MENU_DOCUMENTS = "m:doc"
    MENU_PRESENTATIONS = "m:pres"
    #: Показать само меню. Нужен там, где постоянной клавиатуры нет (MAX).
    MENU_SHOW = "m:show"

    # Чат (§2.2)
    CHAT_RETRY = "c:retry"
    CHAT_NEW_DIALOG = "c:new"
    CHAT_CONTINUE = "c:more"

    # Картинки (§2.3)
    IMAGE_AGAIN = "i:again"
    IMAGE_SHARE = "i:share"
    IMAGE_RETRY = "i:retry"

    # Пресеты (§2.4)
    PRESET_AGAIN = "p:again"
    PRESET_SHARE = "p:share"
    PRESET_ANOTHER = "p:other"
    PRESET_RETRY = "p:retry"

    # Документы
    DOCUMENT_ANOTHER = "d:other"

    # Презентации (фаза 10)
    PRESENTATION_AGAIN = "v:again"
    PRESENTATION_RETRY = "v:retry"
    PRESENTATION_SUGGEST = "v:idea"

    # Подписка (§4.14 оферты: отмена — в профиле)
    SUBSCRIPTION = "s:show"
    SUBSCRIPTION_OFF = "s:off"

    # Пейволл и тарифы (§2.5, §2.8)
    OPEN_TARIFFS = "t:open"
    #: Взять пробный период «Лайта» (фаза 11, часть 3).
    TRIAL = "t:trial"
    INVITE_FRIEND = "r:invite"
    MY_LINK = "r:link"
    REFERRAL_SEND = "r:send"

    # Разовый бонус за подписку на канал
    CHANNEL_OFFER = "n:show"
    CHANNEL_CHECK = "n:check"


#: Префикс выбора пресета. За ним идёт идентификатор из реестра.
PRESET_PREFIX = "p:pick:"

#: Префикс выбора действия над файлом. За ним идентификатор из реестра.
DOCUMENT_PREFIX = "d:pick:"

#: Префикс выбора оформления презентации. За ним идентификатор темы из API.
THEME_PREFIX = "v:theme:"

#: Кнопки-связки. За префиксом — жетон из порта Handoff: сами данные (тема,
#: текст доклада) в кнопку не помещаются, а текст доклада и в базу не кладётся.
REPORT_FROM_PREFIX = "x:rep:"
PRESENTATION_FROM_PREFIX = "x:pres:"

#: Префикс покупки тарифа. За ним идёт идентификатор тарифа.
BUY_PREFIX = "t:buy:"

#: Префикс выбора способа оплаты: за ним «способ:тариф».
METHOD_PREFIX = "t:pay:"

#: Префикс «спросить почту заново». За ним тариф: спросив адрес, надо вернуть
#: человека к оплате того же тарифа, а не в начало витрины.
EMAIL_PREFIX = "t:mail:"


def preset_action(preset_id: str) -> str:
    """Действие «выбран такой-то пресет»."""
    return f"{PRESET_PREFIX}{preset_id}"


def parse_preset_action(action: str) -> str | None:
    """Достаёт идентификатор пресета из действия; None — если это не оно."""
    if not action.startswith(PRESET_PREFIX):
        return None
    return action.removeprefix(PRESET_PREFIX) or None


def document_action(action_id: str) -> str:
    """Действие «выбрано такое-то действие над файлом»."""
    return f"{DOCUMENT_PREFIX}{action_id}"


def parse_document_action(action: str) -> str | None:
    """Достаёт идентификатор действия над файлом; None — если это не оно."""
    if not action.startswith(DOCUMENT_PREFIX):
        return None
    return action.removeprefix(DOCUMENT_PREFIX) or None


def parse_theme_action(action: str) -> str | None:
    """Кнопка оформления из версии до экрана параметров; None — не она.

    Таких кнопок бот больше не рисует, но в переписках они живут вечно, и
    ответить на них надо честно (сессия 7, В5).
    """
    if not action.startswith(THEME_PREFIX):
        return None
    return action.removeprefix(THEME_PREFIX) or None


def report_from_action(token: str) -> str:
    """«Сделать доклад по презентации» с жетоном её темы."""
    return f"{REPORT_FROM_PREFIX}{token}"


def parse_report_from_action(action: str) -> str | None:
    if not action.startswith(REPORT_FROM_PREFIX):
        return None
    return action.removeprefix(REPORT_FROM_PREFIX) or None


def presentation_from_action(token: str) -> str:
    """«Сделать презентацию по докладу» с жетоном доклада."""
    return f"{PRESENTATION_FROM_PREFIX}{token}"


def parse_presentation_from_action(action: str) -> str | None:
    if not action.startswith(PRESENTATION_FROM_PREFIX):
        return None
    return action.removeprefix(PRESENTATION_FROM_PREFIX) or None


# --- Экран параметров презентации (сессия 7) -----------------------------

DECK_GO_PREFIX = "v:go:"
DECK_PICK_PREFIX = "v:opt:"
DECK_SET_PREFIX = "v:set:"
DECK_BACK_PREFIX = "v:back:"


class DeckField(StrEnum):
    """Параметр экрана — коротко: данные кнопки Telegram ограничены 64 байтами."""

    TOPIC = "t"
    MATERIAL = "m"
    LANGUAGE = "l"
    SLIDES = "s"
    AUDIENCE = "a"
    DESIGN = "d"


@dataclass(frozen=True, slots=True)
class DeckAction:
    """Разобранная кнопка экрана параметров."""

    #: Что делать: собрать, выбрать параметр, задать значение, вернуться.
    kind: str
    token: str
    field: DeckField | None = None
    value: str = ""


def deck_go_action(token: str) -> str:
    """«Собрать презентацию» по черновику под жетоном."""
    return f"{DECK_GO_PREFIX}{token}"


def deck_pick_action(token: str, field: DeckField) -> str:
    """Показать выбор значения параметра."""
    return f"{DECK_PICK_PREFIX}{token}:{field.value}"


def deck_set_action(token: str, field: DeckField, value: str) -> str:
    """Задать параметру значение и вернуться на экран."""
    return f"{DECK_SET_PREFIX}{token}:{field.value}:{value}"


def deck_back_action(token: str) -> str:
    """Вернуться на экран без изменений."""
    return f"{DECK_BACK_PREFIX}{token}"


def parse_deck_action(action: str) -> DeckAction | None:
    """Разбирает кнопку экрана параметров; None — это не она или она испорчена."""
    for prefix, kind in (
        (DECK_GO_PREFIX, "go"),
        (DECK_BACK_PREFIX, "back"),
        (DECK_PICK_PREFIX, "pick"),
        (DECK_SET_PREFIX, "set"),
    ):
        if not action.startswith(prefix):
            continue
        token, _, rest = action.removeprefix(prefix).partition(":")
        if not token:
            return None
        if kind in ("go", "back"):
            return DeckAction(kind=kind, token=token)
        code, _, value = rest.partition(":")
        try:
            field = DeckField(code)
        except ValueError:
            return None
        return DeckAction(kind=kind, token=token, field=field, value=value)
    return None


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


#: Чем помечается «вернуться к пробному периоду» там, где обычно стоит тариф:
#: в ожидании почты и в кнопке «Другая почта».
TRIAL_TARGET = "trial"


def email_action(tariff_id: str) -> str:
    """Действие «хочу указать другую почту для чека»."""
    return f"{EMAIL_PREFIX}{tariff_id}"


def parse_email_action(action: str) -> str | None:
    """Достаёт тариф, к оплате которого вернуться после почты."""
    if not action.startswith(EMAIL_PREFIX):
        return None
    return action.removeprefix(EMAIL_PREFIX) or None


def buy_action(tariff_id: str) -> str:
    """Действие «выбран такой-то тариф»."""
    return f"{BUY_PREFIX}{tariff_id}"


def parse_buy_action(action: str) -> str | None:
    """Достаёт идентификатор тарифа из действия; None — если это не оно."""
    if not action.startswith(BUY_PREFIX):
        return None
    return action.removeprefix(BUY_PREFIX) or None
