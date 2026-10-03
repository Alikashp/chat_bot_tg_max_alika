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
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    false,
    func,
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
    # Имя в мессенджере — для поддержки. NOT NULL со значением по умолчанию:
    # «имени нет» — это тоже ответ, и пустая ячейка его не даёт.
    Column("username", String(64), nullable=False, server_default="NONE"),
    # Откуда человек пришёл: payload из ?start=... при регистрации.
    # NOT NULL со значением по умолчанию: «пришёл сам» — это тоже ответ,
    # и пустая ячейка его не даёт (см. core/sources.py).
    Column("source", String(64), nullable=False, server_default="direct"),
    # Почта для фискального чека. NULL — человек картой не платил: у звёзд
    # чек выставляет мессенджер, и адрес там ни к чему.
    Column("email", String(254), nullable=True),
    Column("bonus_messages", Integer, nullable=False, server_default="0"),
    Column("bonus_images", Integer, nullable=False, server_default="0"),
    Column("bonus_documents", Integer, nullable=False, server_default="0"),
    # Презентации (фаза 10). По умолчанию одна: строка, вставленная кем угодно
    # без явного значения, — это новый человек, и разовая презентация ему
    # положена. Вместе с ней по умолчанию ставится и отметка о выдаче.
    Column("bonus_presentations", Integer, nullable=False, server_default="1"),
    # Когда выдали разовую презентацию. NULL — ещё не выдавали. По отметке
    # миграция отличает тех, кто её уже получил: без неё повторный прогон
    # раздал бы вторую (у разборов документов отметки не было, и это стоило
    # лишней раздачи — см. миграцию f8c3d0a91b62).
    Column(
        "presentations_granted_at",
        DateTime(timezone=True),
        nullable=True,
        server_default=func.now(),
    ),
    # Когда началась текущая сборка презентации. NULL — сборки нет. Захват
    # слота — условный UPDATE по этой колонке (см. claim_presentation).
    Column("presentation_started_at", DateTime(timezone=True), nullable=True),
    # Какую версию постоянного меню человек видел последней (§4.2). NULL —
    # никакую: заведён до версий, и меню ему обновится с первым ответом.
    Column("menu_version", String(16), nullable=True),
    # С какого момента считается месячная норма платного тарифа (фаза 11).
    # Пишется каждой выдачей оплаченного. NULL — после появления месячных
    # норм человек ещё не платил; период тогда считается от конца срока.
    Column("norm_since", DateTime(timezone=True), nullable=True),
    # Заказ, по которому выдан пробный период (фаза 11, часть 3). NULL —
    # пробного периода не было. Отметка — та же, что «один раз на человека».
    Column("trial_order_id", String(36), nullable=True),
    # Когда выдали разовый бонус за подписку на канал. NULL — не выдавали.
    # Отметка и есть защита от повторной выдачи: начисление ставит её тем же
    # UPDATE, который добавляет картинки, и условие NULL стоит в его WHERE.
    Column("channel_bonus_at", DateTime(timezone=True), nullable=True),
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
    CheckConstraint("bonus_documents >= 0", name="ck_users_bonus_documents"),
    CheckConstraint("bonus_presentations >= 0", name="ck_users_bonus_presentations"),
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
    # Картинки и документы по суткам больше не считаются (фаза 11: у них
    # месячная норма, monthly_usage). Колонки остаются, пока жива версия
    # бота, которая в них пишет: выкладка идёт без остановки, и пару минут
    # старая и новая работают рядом. Удалять — отдельной миграцией потом.
    Column("images_used", Integer, nullable=False, server_default="0"),
    Column("documents_used", Integer, nullable=False, server_default="0"),
)

star_cancels = Table(
    "star_cancels",
    metadata,
    # Идентификатор первого списания звёздной подписки: по нему Telegram её
    # отменяет. Ключ — он, а не человек: у одного человека могут застрять
    # две прежние подписки, а одна и та же подписка встаёт в очередь один раз.
    Column("charge_id", String(128), primary_key=True),
    Column(
        "user_id",
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("queued_at", DateTime(timezone=True), nullable=False),
)

monthly_usage = Table(
    "monthly_usage",
    metadata,
    Column(
        "user_id",
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    # Начало периода месячной нормы — ключ, а не «месяц»: период у каждого
    # свой (от оплаты или от регистрации) и считается в ядре. Новый период —
    # новая строка; оттого остаток и не переносится.
    Column("period_start", DateTime(timezone=True), primary_key=True),
    Column("images_used", Integer, nullable=False, server_default="0"),
    Column("documents_used", Integer, nullable=False, server_default="0"),
    Column("presentations_used", Integer, nullable=False, server_default="0"),
    CheckConstraint("images_used >= 0", name="ck_monthly_usage_images"),
    CheckConstraint("documents_used >= 0", name="ck_monthly_usage_documents"),
    CheckConstraint("presentations_used >= 0", name="ck_monthly_usage_presentations"),
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
    # Заказ списания с неизвестным исходом: один период — один заказ.
    Column("charge_order_id", String(36), nullable=True),
    # Предупредить перед очередным списанием и без этого не списывать. Только
    # первое списание после пробного периода (фаза 11, часть 3).
    Column("remind_before_charge", Boolean, nullable=False, server_default=false()),
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
    # Заказ на пробный период: 1 ₽ за три дня «Лайта» с сохранением карты.
    Column("trial", Boolean, nullable=False, server_default=false()),
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

generations = Table(
    "generations",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column(
        "user_id",
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    ),
    # chat | image | preset. Строкой, а не перечислением базы: вид работы
    # добавляется продуктовым решением, а менять тип в PostgreSQL дороже,
    # чем дописать строку в реестр.
    Column("kind", String(16), nullable=False),
    # Идентификатор прикола из config/presets.py. NULL у чата и у картинки
    # по описанию — у них прикола нет вовсе.
    Column("preset_id", String(32), nullable=True),
    Column("model", String(64), nullable=False, server_default=""),
    # success | failed. Упавшие попытки нужны не для полноты: провайдер берёт
    # деньги за попытку, и без них доля брака видна только по счёту.
    Column("status", String(16), nullable=False),
    # Имя класса исключения. Текста ошибки здесь нет намеренно: он несёт
    # присланный запрос, а содержимого сообщений мы не храним (§3.5).
    Column("error_code", String(64), nullable=True),
    # NULL, а не ноль: у картинок провайдер токены не называет, и ноль в
    # такой строке испортил бы любую сумму.
    Column("tokens_in", Integer, nullable=True),
    Column("tokens_out", Integer, nullable=True),
    Column("duration_ms", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    # Два разреза, которые спрашивают на самом деле: «что делал этот человек»
    # и «что происходило за такой-то период». Без второго отчёт за месяц
    # читает таблицу целиком.
    Index("ix_generations_user_created", "user_id", "created_at"),
    Index("ix_generations_created", "created_at"),
    CheckConstraint("duration_ms >= 0", name="ck_generations_duration"),
)
