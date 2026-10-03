"""Месячная норма на настоящей базе: одновременные запросы и миграция.

Т3 — два одновременных запроса одного человека не тратят больше нормы. В
бою их разводит ещё и ограничитель задач (§3.4.8), но он живёт в памяти
одного процесса; последняя линия — сама база, и проверяется здесь именно
она: оба запроса проходят проверку остатка, оба отдают картинку, а счётчик
нормы всё равно не уходит за предел.

Т4 — миграция не трогает накопленное и переживает повторный накат.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from dataclasses import replace

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.adapters.storage.migrations import _config
from app.adapters.storage.postgres import PostgresStorage, create_engine
from app.adapters.storage.schema import metadata
from app.core.limits import LimitKind, monthly_norm
from app.core.models import Photo, TariffId
from app.core.scenarios import images, payments, spending
from app.core.scenarios.deps import Deps, Session
from app.ports.ai import ImageQuality
from tests.fakes import PNG_BYTES, FakeImages, FakeLogger, FakeStars

TEST_DSN = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    if TEST_DSN is None:
        pytest.skip("TEST_DATABASE_URL не задан")
    engine = create_engine(TEST_DSN)
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
        await connection.execute(
            text(
                "TRUNCATE subscriptions, payments, referrals, dialogs, usage, "
                "monthly_usage, users RESTART IDENTITY CASCADE"
            )
        )
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def storage(engine: AsyncEngine) -> PostgresStorage:
    """Сценарии ниже идут по настоящей базе, а не по хранилищу в памяти."""
    return PostgresStorage(engine)


class TwoAtOnceImages(FakeImages):
    """Провайдер, который отвечает, только когда пришли оба запроса.

    Иначе первый запрос мог бы успеть списать до того, как второй проверит
    остаток, и тест был бы зелёным без всякой защиты в базе.
    """

    def __init__(self) -> None:
        super().__init__()
        self._arrived = 0
        self._both = asyncio.Event()

    async def generate(
        self, prompt: str, *, quality: ImageQuality, model: str = ""
    ) -> Photo:
        self._arrived += 1
        if self._arrived >= 2:
            self._both.set()
        await asyncio.wait_for(self._both.wait(), timeout=5)
        return Photo(data=PNG_BYTES)


async def test_two_requests_at_once_do_not_spend_past_the_norm(
    deps: Deps,
    session: Session,
    storage: PostgresStorage,
    stars: FakeStars,
    logger: FakeLogger,
) -> None:
    """Т3: в норме одна картинка, бонуса нет, запросов два — норма не уходит за 1."""
    await payments.start_stars(deps, session, TariffId.PRO)
    await payments.confirm(deps, stars.invoices[0].order_id, charge_id="charge-1")
    user = await storage.get_user_by_id(session.user.id)
    assert user is not None
    assert await storage.spend_bonus(user.id, images=user.bonus_images)
    paid = replace(session, user=await storage.get_user_by_id(user.id) or user)
    limit = monthly_norm(paid.tariff, LimitKind.IMAGES)
    period = spending.period_of(deps, paid)
    for _ in range(limit - 1):
        assert await storage.spend_norm(
            user.id, period.start, LimitKind.IMAGES, limit=limit
        )
    both = replace(deps, images=TwoAtOnceImages())

    await asyncio.gather(
        images.draw(both, paid, "кот"),
        images.draw(both, paid, "пёс"),
    )

    spent = await storage.get_period_usage(user.id, period.start)
    assert spent.images_used == limit
    fresh = await storage.get_user_by_id(user.id)
    assert fresh is not None and fresh.bonus_images == 0
    assert logger.names().count("charged_over_limit") == 1


# --- Т4: миграция --------------------------------------------------------

#: Ревизия перед месячными нормами.
BEFORE = "e1a7c3d9b204"
MIGRATION_DB = "botdb_monthly_norms"


@pytest.fixture
async def migration_dsn(engine: AsyncEngine) -> AsyncIterator[str]:
    """Отдельная пустая база: миграции идут с нуля, а не поверх create_all."""
    assert TEST_DSN is not None
    admin = engine.execution_options(isolation_level="AUTOCOMMIT")
    async with admin.connect() as connection:
        await connection.execute(text(f"DROP DATABASE IF EXISTS {MIGRATION_DB}"))
        await connection.execute(text(f"CREATE DATABASE {MIGRATION_DB}"))
    base, _, _ = TEST_DSN.rpartition("/")
    try:
        yield f"{base}/{MIGRATION_DB}"
    finally:
        async with admin.connect() as connection:
            await connection.execute(text(f"DROP DATABASE IF EXISTS {MIGRATION_DB}"))


async def _migrate(dsn: str, monkeypatch: pytest.MonkeyPatch, action: str) -> None:
    from alembic import command

    monkeypatch.setenv("DATABASE_URL", dsn)
    config = _config(dsn)
    step = command.upgrade if action.startswith("up") else command.downgrade
    target = action.split(":", 1)[1]
    await asyncio.to_thread(step, config, target)


async def _balances(dsn: str) -> list[tuple[int, int, int, int]]:
    engine = create_engine(dsn)
    try:
        async with engine.connect() as connection:
            rows = await connection.execute(
                text(
                    "SELECT bonus_messages, bonus_images, bonus_documents, "
                    "bonus_presentations FROM users ORDER BY id"
                )
            )
            return [tuple(row) for row in rows.all()]
    finally:
        await engine.dispose()


# Предупреждение Alembic о формате alembic.ini к миграции отношения не имеет.
@pytest.mark.filterwarnings("ignore:No path_separator found:DeprecationWarning")
async def test_the_migration_keeps_every_balance_and_can_run_again(
    migration_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Т4: накопленное до выкладки на месте — после наката, отката и наката снова."""
    await _migrate(migration_dsn, monkeypatch, f"up:{BEFORE}")
    engine = create_engine(migration_dsn)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO users (messenger, external_id, tariff, "
                    "referral_code, support_number, created_at, bonus_messages, "
                    "bonus_images, bonus_documents, bonus_presentations, "
                    "tariff_expires_at) VALUES "
                    "('telegram', '1', 'free', 'a', 1, now(), 20, 7, 3, 1, NULL), "
                    "('telegram', '2', 'pro', 'b', 2, now(), 0, 2, 0, 2, "
                    " now() + interval '10 days')"
                )
            )
    finally:
        await engine.dispose()
    before = await _balances(migration_dsn)

    await _migrate(migration_dsn, monkeypatch, "up:head")
    assert await _balances(migration_dsn) == before
    await _migrate(migration_dsn, monkeypatch, f"down:{BEFORE}")
    await _migrate(migration_dsn, monkeypatch, "up:head")

    assert await _balances(migration_dsn) == before == [(20, 7, 3, 1), (0, 2, 0, 2)]
