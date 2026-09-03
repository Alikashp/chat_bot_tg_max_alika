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

#: Что стоит в поле username, когда его у человека нет.
#:
#: Не пустота и не NULL: колонку читает человек из поддержки, а пустая ячейка
#: одинаково выглядит и как «имени нет», и как «мы его не записали». Слово
#: отвечает на этот вопрос сразу. Настоящим именем оно не притворяется:
#: username в Telegram короче пяти символов не бывает.
NO_USERNAME = "NONE"


def username_or_none(reported: str | None) -> str:
    """Приводит имя от мессенджера к тому, что кладём в базу."""
    cleaned = (reported or "").strip().lstrip("@")
    return cleaned or NO_USERNAME


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

    Кто кого пригласил, здесь не хранится. Эта связь живёт в таблице
    ``referrals``, где у неё есть и уникальность (один приглашённый — один
    раз), и время (для суточного потолка наград). Дублировать её полем у
    пользователя значило бы завести второй источник правды, который однажды
    разойдётся с первым.
    """

    id: UserId
    messenger: MessengerKind
    external_id: str
    tariff: TariffId
    referral_code: str
    #: Номер для поддержки. Случайный: по внутреннему идентификатору строки
    #: было бы видно, сколько всего людей в сервисе. См. core/support.py.
    support_number: int
    created_at: datetime
    daily_image_quota: int
    #: Имя пользователя в мессенджере — для поддержки, и только для неё:
    #: человеку оно нигде не показывается. Обновляется при каждом обращении,
    #: потому что его меняют когда захотят, а протухшее имя хуже, чем
    #: никакого: по нему пойдут искать не того. NO_USERNAME — имени нет.
    username: str = NO_USERNAME
    bonus_messages: int = 0
    bonus_images: int = 0
    tariff_expires_at: datetime | None = None
    #: Чего бот ждёт следующим сообщением — описание картинки или фото под
    #: прикол. См. core/pending.py. None — обычный чат.
    pending: str | None = None
    #: Что повторить по кнопке «Ещё раз». См. core/retry_context.py.
    retry_context: str | None = None


@dataclass(frozen=True, slots=True)
class Payment:
    """Заказ на подписку.

    Заводится до обращения к провайдеру и живёт дальше него: по нему потом
    сверяется уведомление об оплате. Без своей записи мы верили бы на слово
    тому, кто постучался на вебхук.
    """

    id: str
    user_id: UserId
    tariff: TariffId
    method: str
    amount: int
    currency: str
    status: str
    created_at: datetime
    #: Идентификатор платежа у провайдера. Появляется после его создания.
    external_id: str | None = None
    paid_at: datetime | None = None
    #: Редакция документов, принятая при оформлении заказа.
    docs_version: str | None = None


@dataclass(frozen=True, slots=True)
class Subscription:
    """Регулярная подписка пользователя.

    Одна на человека: смена тарифа — это та же подписка с другим тарифом, а
    не вторая рядом. Иначе с одного человека списывали бы дважды.

    ``next_charge_at`` — момент следующего списания. У карты по нему работает
    планировщик; у звёзд списывает сам Telegram, и дата нужна, чтобы честно
    показать её человеку и предупредить заранее.
    """

    user_id: UserId
    tariff: TariffId
    method: str
    status: str
    #: Сколько списываем и в чём. Хранится, а не считается по тарифу: цена
    #: тарифа меняется, а списываем мы ровно то, на что человек согласился,
    #: пока не предупредим об изменении (§4.17 оферты). У звёзд это ещё и
    #: единственный способ узнать сумму: её зафиксировал сам Telegram.
    amount: int
    currency: str
    next_charge_at: datetime
    created_at: datetime
    #: Способ оплаты, сохранённый провайдером. Реквизитов карты у нас нет —
    #: только выданный им идентификатор.
    payment_method_id: str | None = None
    #: Идентификатор платежа у мессенджера. Нужен, чтобы отменить подписку
    #: на звёздах: Telegram отменяет её по первому платежу.
    charge_id: str | None = None
    #: За какое списание уже предупредили. Хранится дата самого списания, а
    #: не время отправки: так напоминание не уйдёт дважды и не потеряется.
    reminded_for: datetime | None = None
    #: За какое списание уже сверили цену с тарифом (§4.17). Отметка ставится
    #: и когда цена не менялась: иначе сверка повторялась бы каждый тик
    #: планировщика все семь дней до списания.
    price_checked_for: datetime | None = None
    #: С какого момента списания не проходят. Пусто — всё в порядке.
    failed_since: datetime | None = None
    cancelled_at: datetime | None = None


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
    #: Имя пользователя, каким его назвал мессенджер прямо сейчас. None —
    #: мессенджер о нём не сказал; это не то же самое, что «имени нет».
    username: str | None = None
    text: str | None = None
    #: Ссылка на присланное фото в терминах мессенджера. Ядро её не
    #: разбирает: отдаёт обратно адаптеру, чтобы тот скачал файл.
    photo_ref: str | None = None
    action: str | None = None
    start_payload: str | None = None
    callback_id: str | None = None
    #: Заказ, который мессенджер объявил оплаченным (звёзды Telegram).
    paid_order_id: str | None = None
    #: Очередное списание по подписке, а не первая оплата. Мессенджер
    #: ссылается тем же заказом, что и в первый раз, поэтому без этого
    #: признака продление выглядело бы как повтор уже оплаченного и
    #: пропадало бы вместе с оплаченным периодом.
    paid_renewal: bool = False
    #: Идентификатор конкретного списания. У каждого периода свой — по нему
    #: продление отличается от собственного повтора.
    paid_charge_id: str | None = None
    #: Запрос «готовы ли принять оплату» вместе с заказом, о котором спросили.
    pre_checkout_id: str | None = None
    pre_checkout_order_id: str | None = None
