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
from app.core.tariffs import ModelTier, Tariff, tariff_of
from app.ports.ai import ImageProvider, LLMProvider
from app.ports.messenger import Messenger
from app.ports.observability import Logger
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

    @property
    def tariff(self) -> Tariff:
        return tariff_of(self.user.tariff)

    def model(self, settings: CoreSettings) -> str:
        """Модель под тариф пользователя.

        Пользователю не показывается и им не выбирается (§2.2). Названия
        приходят из конфига, чтобы сменить модель можно было переменной
        окружения, без выкладки кода.
        """
        if self.tariff.model_tier is ModelTier.ECONOMY:
            return settings.model_economy
        return settings.model_standard
