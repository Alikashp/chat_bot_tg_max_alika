"""Порт хранилища.

Хранилище отвечает за долговечность и атомарность, но не за продуктовые
правила. Решение «списывать из дневной квоты или из бонуса» принимает
core/limits.py, а хранилище лишь выполняет атомарные примитивы. Иначе эта
логика продублировалась бы в каждой реализации порта.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Protocol

from app.core.models import (
    DialogState,
    MessengerKind,
    Payment,
    TariffId,
    Usage,
    User,
    UserId,
)


class Storage(Protocol):
    """Долговременное хранилище пользователей, расхода, диалогов и рефералов."""

    # --- Пользователи --------------------------------------------------

    async def get_user(self, messenger: MessengerKind, external_id: str) -> User | None:
        """Находит пользователя по идентификатору в мессенджере."""
        ...

    async def get_user_by_id(self, user_id: UserId) -> User | None:
        """Находит пользователя по внутреннему идентификатору."""
        ...

    async def get_user_by_referral_code(self, code: str) -> User | None:
        """Находит пользователя по его реферальному коду."""
        ...

    async def create_user(
        self,
        *,
        messenger: MessengerKind,
        external_id: str,
        referral_code: str,
        daily_image_quota: int,
        referred_by: UserId | None = None,
    ) -> User:
        """Заводит нового пользователя.

        Реализация обязана обеспечить уникальность пары
        (messenger, external_id) и уникальность referral_code.
        """
        ...

    async def set_tariff(
        self,
        user_id: UserId,
        tariff: TariffId,
        expires_at: datetime | None,
    ) -> None:
        """Меняет тариф пользователя."""
        ...

    async def set_pending(self, user_id: UserId, pending: str | None) -> None:
        """Запоминает, чего бот ждёт от пользователя следующим сообщением.

        None сбрасывает ожидание. Хранится в базе, а не в памяти процесса:
        выкатка посреди разговора не должна превращать «Опиши, что нарисовать»
        в потерянный вопрос.
        """
        ...

    async def set_retry_context(self, user_id: UserId, context: str | None) -> None:
        """Запоминает, что повторить по кнопке «Ещё раз».

        Контекст один на пользователя и перезаписывается: «ещё раз»
        осмысленно относится к последнему результату.
        """
        ...

    # --- Расход --------------------------------------------------------

    async def get_usage(self, user_id: UserId, day: date) -> Usage:
        """Возвращает расход за указанные сутки.

        Для суток, за которые ещё ничего не потрачено, возвращает нулевой
        Usage, а не None: «не тратил» и «нет записи» — это одно и то же.
        """
        ...

    async def add_usage(
        self,
        user_id: UserId,
        day: date,
        *,
        messages: int = 0,
        images: int = 0,
    ) -> Usage:
        """Атомарно увеличивает дневной расход и возвращает новое значение."""
        ...

    async def spend_bonus(
        self,
        user_id: UserId,
        *,
        messages: int = 0,
        images: int = 0,
    ) -> bool:
        """Атомарно списывает бонусный баланс.

        Возвращает False и ничего не меняет, если баланса не хватает. Это
        всё-или-ничего: частичного списания быть не должно.
        """
        ...

    async def add_bonus(
        self,
        user_id: UserId,
        *,
        messages: int = 0,
        images: int = 0,
    ) -> None:
        """Атомарно начисляет бонусный баланс (награда за реферала)."""
        ...

    # --- Оплата --------------------------------------------------------

    async def create_payment(
        self,
        *,
        user_id: UserId,
        tariff: TariffId,
        method: str,
        amount: int,
        currency: str,
    ) -> Payment:
        """Заводит заказ на подписку до обращения к провайдеру.

        Своя запись нужна затем, чтобы уведомление об оплате было с чем
        сверить: без неё мы верили бы на слово тому, кто постучался на вебхук.
        """
        ...

    async def get_payment(self, payment_id: str) -> Payment | None:
        """Находит заказ по нашему идентификатору."""
        ...

    async def attach_external_id(self, payment_id: str, external_id: str) -> bool:
        """Запоминает идентификатор платежа у провайдера.

        Возвращает False, если этот платёж уже привязан к другому заказу.
        Такого быть не должно — ключом идемпотентности служит наш заказ, — но
        если случилось, отдавать ссылку нельзя: подтвердить оплату по ней мы
        всё равно не сможем, а деньги человек отдаст.
        """
        ...

    async def mark_paid(self, payment_id: str) -> bool:
        """Атомарно переводит заказ в «оплачен».

        Возвращает True только при первом переходе. Это и есть защита от
        двойной выдачи: уведомление об оплате приходит по несколько раз, и
        продлевать подписку на каждое нельзя.
        """
        ...

    # --- Диалог --------------------------------------------------------

    async def get_dialog(self, user_id: UserId) -> DialogState:
        """Возвращает состояние диалога; для нового пользователя — пустое."""
        ...

    async def save_dialog(self, user_id: UserId, dialog: DialogState) -> None:
        """Сохраняет состояние диалога."""
        ...

    async def reset_dialog(self, user_id: UserId) -> None:
        """Начинает диалог заново («🔄 Новый диалог»)."""
        ...

    # --- Рефералы ------------------------------------------------------

    async def record_referral(self, referrer_id: UserId, referee_id: UserId) -> bool:
        """Фиксирует реферальную связь.

        Возвращает True, если связь записана впервые, и False, если такая
        пара уже была. Идемпотентность обязана обеспечиваться самим
        хранилищем (в PostgreSQL — ограничением уникальности), а не
        проверкой в вызывающем коде: только так повторный /start физически
        не сможет начислить награду дважды.

        Реализация обязана отвергать self-referral (referrer_id == referee_id),
        возвращая False.
        """
        ...

    async def count_referrals(self, referrer_id: UserId) -> int:
        """Сколько всего друзей привёл пользователь (для профиля)."""
        ...

    async def count_referrals_since(self, referrer_id: UserId, since: datetime) -> int:
        """Сколько наград начислено с указанного момента (суточный антифрод)."""
        ...
