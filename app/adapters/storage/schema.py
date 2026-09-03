"""Схема базы данных.

Определения таблиц отдельно от реализации хранилища: их использует и сам
адаптер, и Alembic для миграций.

Главное здесь — ограничения. Идемпотентность рефералки и запрет
self-referral заданы схемой, а не проверками в коде: повторный /start по той
же ссылке физически не может начислить награду дважды, сколько бы
одновременных запросов ни пришло. Проверка в коде — это обещание
разработчика, ограничение в базе — гарантия.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData()

users = Table(
    "users",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("messenger", String(16), nullable=False),
    Column("external_id", String(64), nullable=False),
    Column("tariff", String(16), nullable=False, server_default="free"),
    Column("referral_code", String(32), nullable=False),
    Column("support_number", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("daily_image_quota", Integer, nullable=False),
    # Имя в мессенджере — для поддержки. NOT NULL со значением по умолчанию:
    # «имени нет» — это тоже ответ, и пустая ячейка его не даёт.
    Column("username", String(64), nullable=False, server_default="NONE"),
    Column("bonus_messages", Integer, nullable=False, server_default="0"),
    Column("bonus_images", Integer, nullable=False, server_default="0"),
    Column("tariff_expires_at", DateTime(timezone=True), nullable=True),
    # Чего бот ждёт от пользователя следующим сообщением (см. core/pending.py).
    # Text, а не String: у приколов с двумя фото ожидание несёт ещё и ссылки
    # на уже присланные снимки, а ссылка в MAX — это http-адрес, и его длину
    # задаём не мы.
    Column("pending", Text, nullable=True),
    # Что повторить по кнопке «Ещё раз» (см. core/retry_context.py).
    # Text, а не String: внутри лежит описание картинки, а оно бывает
    # длиной в абзац.
    Column("retry_context", Text, nullable=True),
    # Один и тот же числовой id в Telegram и в MAX — разные люди.
    UniqueConstraint("messenger", "external_id", name="uq_users_messenger_external"),
    UniqueConstraint("referral_code", name="uq_users_referral_code"),
    UniqueConstraint("support_number", name="uq_users_support_number"),
    # Бонус не может уйти в минус ни при какой гонке.
    CheckConstraint("bonus_messages >= 0", name="ck_users_bonus_messages"),
    CheckConstraint("bonus_images >= 0", name="ck_users_bonus_images"),
)

usage = Table(
    "usage",
    metadata,
    Column(
        "user_id",
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    # Сутки как дата, а не как отметка времени: «сегодня» считается по
    # часовому поясу пользователя ещё в ядре, сюда приходит уже готовый день.
    Column("day", Date, primary_key=True),
    Column("messages_used", Integer, nullable=False, server_default="0"),
    Column("images_used", Integer, nullable=False, server_default="0"),
)

dialogs = Table(
    "dialogs",
    metadata,
    Column(
        "user_id",
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("turns", JSONB, nullable=False),
    Column("user_turns", Integer, nullable=False, server_default="0"),
)

subscriptions = Table(
    "subscriptions",
    metadata,
    # Первичный ключ — сам пользователь: подписка у человека одна. Смена
    # тарифа меняет строку, а не добавляет вторую, иначе списывали бы дважды.
    Column(
        "user_id",
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("tariff", String(16), nullable=False),
    Column("method", String(16), nullable=False),
    Column("status", String(16), nullable=False),
    # Сумма списания и валюта. Хранятся, а не берутся из тарифа: цена тарифа
    # меняется, а списываем мы то, на что человек согласился, пока не
    # предупредим об изменении (§4.17 оферты).
    Column("amount", Integer, nullable=False),
    Column("currency", String(8), nullable=False),
    Column("next_charge_at", DateTime(timezone=True), nullable=False, index=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("payment_method_id", String(128), nullable=True),
    Column("charge_id", String(128), nullable=True),
    Column("reminded_for", DateTime(timezone=True), nullable=True),
    # За какое списание уже сверили цену с тарифом (§4.17 оферты).
    Column("price_checked_for", DateTime(timezone=True), nullable=True),
    Column("failed_since", DateTime(timezone=True), nullable=True),
    Column("cancelled_at", DateTime(timezone=True), nullable=True),
    CheckConstraint("amount > 0", name="ck_subscriptions_amount"),
)

payments = Table(
    "payments",
    metadata,
    # Идентификатор наш, а не провайдера: он нужен до того, как провайдер о
    # платеже узнает, и он же служит ключом идемпотентности.
    Column("id", String(36), primary_key=True),
    Column(
        "user_id",
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    ),
    Column("tariff", String(16), nullable=False),
    Column("method", String(16), nullable=False),
    Column("amount", Integer, nullable=False),
    Column("currency", String(8), nullable=False),
    Column("status", String(16), nullable=False, server_default="pending"),
    Column("created_at", DateTime(timezone=True), nullable=False),
    # Идентификатор у провайдера. Уникален: одно уведомление об оплате не
    # должно уметь закрыть два наших заказа.
    Column("external_id", String(128), nullable=True, unique=True),
    Column("paid_at", DateTime(timezone=True), nullable=True),
    # Редакция документов, с которой человек согласился, оформляя заказ.
    # Хранится у платежа, а не у пользователя: документы меняются, и важно,
    # какая редакция действовала в момент конкретной оплаты.
    Column("docs_version", String(32), nullable=True),
    CheckConstraint("amount > 0", name="ck_payments_amount"),
)

referrals = Table(
    "referrals",
    metadata,
    # Ключ по приглашённому, а не по паре: награда полагается за нового
    # пользователя и только одному пригласившему. Пара в первичном ключе
    # позволила бы одного и того же человека «привести» дважды разными людьми.
    Column(
        "referee_id",
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "referrer_id",
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("created_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("referrer_id <> referee_id", name="ck_referrals_no_self"),
)
