"""Зависимости сценариев и контекст одного обращения.

Сценарии не создают зависимости и не знают, откуда те взялись: всё приходит
снаружи. Благодаря этому один и тот же сценарий работает и в Telegram, и в
MAX, и в тесте с фейками — различаются только объекты, которые в него передали.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime

from app.core.limits import current_day
from app.core.models import Chat, User
from app.core.settings import CoreSettings
from app.core.tariffs import ModelTier, Tariff, active_tariff, tariff_of
from app.ports.ai import ImageProvider, LLMProvider
from app.ports.concurrency import Concurrency
from app.ports.messenger import Messenger
from app.ports.observability import Logger
from app.ports.payments import CardPayments, StarsPayments
from app.ports.storage import Storage


@dataclass(frozen=True, slots=True)
class Deps:
    """Всё, что сценариям нужно снаружи."""

    storage: Storage
    messenger: Messenger
    llm: LLMProvider
    images: ImageProvider
    settings: CoreSettings
    logger: Logger
    #: Ограничитель одновременных задач на пользователя (§3.4.8).
    guard: Concurrency
    #: Оплата картой. None — не настроена: ключей провайдера нет.
    cards: CardPayments | None
    #: Оплата звёздами. None — мессенджер такого не умеет (MAX).
    stars: StarsPayments | None
    #: Часы. Передаются отдельно, чтобы тесты про сброс суток не зависели от
    #: того, в какое время их запустили.
    now: Callable[[], datetime]

    def today(self) -> date:
        """Какие сейчас сутки для пользователя."""
        return current_day(self.now(), self.settings.timezone)


@dataclass(frozen=True, slots=True)
class Session:
    """Контекст одного обращения: кто написал и куда отвечать."""

    user: User
    chat: Chat
    day: date
    #: Момент обращения. Нужен отдельно от day: сутки считаются по Москве и
    #: определяют дневную квоту, а срок подписки — точное время, и «сегодня»
    #: для него слишком грубо.
    now: datetime

    @property
    def tariff(self) -> Tariff:
        """Тариф, который действует прямо сейчас.

        Именно действует, а не записан: у оплаченного тарифа есть срок, и
        после него человек возвращается на бесплатный. Проверка живёт здесь,
        а не в каждом сценарии, потому что забыть её — значит раздать
        подписки навсегда.
        """
        return tariff_of(
            active_tariff(self.user.tariff, self.user.tariff_expires_at, self.now)
        )

    def model(self, settings: CoreSettings) -> str:
        """Модель под тариф пользователя.

        Пользователю не показывается и им не выбирается (§2.2). Названия
        приходят из конфига, чтобы сменить модель можно было переменной
        окружения, без выкладки кода.
        """
        if self.tariff.model_tier is ModelTier.ECONOMY:
            return settings.model_economy
        return settings.model_standard


def session_for(deps: Deps, user: User) -> Session:
    """Контекст обращения к человеку, который сейчас ничего не писал.

    Нужен там, где разговор начинаем мы: подтверждение оплаты приходит
    вебхуком провайдера, напоминание о списании — по расписанию. Чат в
    личной переписке совпадает с самим человеком, поэтому собрать его можно
    из пользователя, не имея входящего сообщения.
    """
    return Session(
        user=user,
        chat=Chat(messenger=user.messenger, chat_id=user.external_id),
        day=deps.today(),
        now=deps.now(),
    )
