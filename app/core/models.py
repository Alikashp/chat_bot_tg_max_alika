"""Доменные модели.

Чистый Python: ни одного импорта из aiogram, maxapi, httpx, aiohttp или
драйвера БД. Это проверяется тестом tests/unit/test_core_is_pure.py —
см. критерий приёмки A2 в docs/spec.md.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from enum import StrEnum
from typing import NewType

#: Внутренний идентификатор пользователя. Не совпадает с id в мессенджере:
#: один и тот же человек в Telegram и в MAX — два разных пользователя.
UserId = NewType("UserId", int)


class MessengerKind(StrEnum):
    """Мессенджер, из которого пришёл пользователь."""

    TELEGRAM = "telegram"
    MAX = "max"


class TariffId(StrEnum):
    """Тарифы из §2.8 задания."""

    FREE = "free"
    LITE = "lite"
    PRO = "pro"
    MAX = "max"


class Role(StrEnum):
    """Роль реплики в диалоге."""

    USER = "user"
    ASSISTANT = "assistant"


@dataclass(frozen=True, slots=True)
class ChatTurn:
    """Одна реплика диалога."""

    role: Role
    content: str


@dataclass(frozen=True, slots=True)
class User:
    """Пользователь бота.

    Бонусные балансы (``bonus_messages``, ``bonus_images``) живут здесь, а не
    в дневном расходе: они не сгорают по суткам. Дневная квота картинок тоже
    хранится у пользователя, потому что она персональная — пришедшие по
    deeplink ``pres_*`` получают 5 вместо 3 (§2.1 задания).
    """

    id: UserId
    messenger: MessengerKind
    external_id: str
    tariff: TariffId
    referral_code: str
    created_at: datetime
    daily_image_quota: int
    referred_by: UserId | None = None
    bonus_messages: int = 0
    bonus_images: int = 0
    tariff_expires_at: datetime | None = None
    #: Чего бот ждёт следующим сообщением — описание картинки или фото под
    #: прикол. См. core/pending.py. None — обычный чат.
    pending: str | None = None
    #: Что повторить по кнопке «Ещё раз». См. core/retry_context.py.
    retry_context: str | None = None


@dataclass(frozen=True, slots=True)
class Usage:
    """Израсходованное за конкретные сутки.

    Обнуляется сменой суток: за новый день просто заводится новая запись,
    ничего не «сбрасывается» фоновой задачей.
    """

    day: date
    messages_used: int = 0
    images_used: int = 0


@dataclass(frozen=True, slots=True)
class DialogState:
    """Состояние диалога.

    ``user_turns`` считает только сообщения пользователя: от него зависит
    появление кнопки «🔄 Новый диалог» начиная с 10-го сообщения (§2.2).
    Счётчик отдельный от ``len(turns)``, потому что история обрезается по
    длине контекста, а счётчик обрезаться не должен.
    """

    turns: tuple[ChatTurn, ...] = ()
    user_turns: int = 0

    def appended(self, *turns: ChatTurn, max_turns: int) -> DialogState:
        """Возвращает диалог с добавленными репликами, обрезанный до ``max_turns``."""
        merged = (*self.turns, *turns)
        added_user_turns = sum(1 for turn in turns if turn.role is Role.USER)
        return replace(
            self,
            turns=merged[-max_turns:] if max_turns > 0 else (),
            user_turns=self.user_turns + added_user_turns,
        )


@dataclass(frozen=True, slots=True)
class Button:
    """Кнопка под сообщением.

    ``action`` — либо строковое действие, которое вернётся боту при нажатии,
    либо ``None``, если кнопка ведёт по ссылке ``url``.
    """

    text: str
    action: str | None = None
    url: str | None = None

    def __post_init__(self) -> None:
        if (self.action is None) == (self.url is None):
            raise ValueError("У кнопки должно быть ровно одно из: action или url")


@dataclass(frozen=True, slots=True)
class Keyboard:
    """Клавиатура под конкретным сообщением."""

    rows: tuple[tuple[Button, ...], ...] = ()

    @classmethod
    def row(cls, *buttons: Button) -> Keyboard:
        """Клавиатура из одного ряда."""
        return cls(rows=(buttons,))


@dataclass(frozen=True, slots=True)
class Chat:
    """Куда отправлять сообщение."""

    messenger: MessengerKind
    chat_id: str


@dataclass(frozen=True, slots=True)
class MessageRef:
    """Ссылка на отправленное сообщение — чтобы потом его отредактировать."""

    chat: Chat
    message_id: str


@dataclass(frozen=True, slots=True)
class Photo:
    """Картинка в виде байтов."""

    data: bytes
    mime_type: str = "image/png"
    filename: str = "image.png"


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    """Входящее сообщение, приведённое к общему виду обоими адаптерами.

    Дедупликации здесь нет намеренно: повтор отсеивается ещё до очереди, в
    интейке адаптера (app/adapters/telegram/intake.py), — иначе копия заняла
    бы место в очереди и доехала бы до разбора.
    """

    chat: Chat
    external_user_id: str
    text: str | None = None
    photo_file_id: str | None = None
    action: str | None = None
    start_payload: str | None = None
    callback_id: str | None = None
