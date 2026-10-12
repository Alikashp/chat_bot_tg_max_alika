"""Суточный потолок наград за друзей при одновременных регистрациях (С5).

Потолок проверялся «посчитать, потом записать»: десяток друзей, нажавших
/start по одной ссылке разом, проходили подсчёт все, пока ни одна запись ещё
не легла, — и пригласивший получал наград больше потолка. Гарантия теперь в
самой записи: подсчёт и вставка — одна транзакция под блокировкой строки
пригласившего. Проверяется на настоящей базе.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import text

from app.adapters.storage.postgres import PostgresStorage, create_engine
from app.adapters.storage.schema import metadata
from app.core import support
from app.core.models import Chat, MessengerKind
from app.core.referral import referral_payload
from app.core.scenarios import onboarding
from app.core.scenarios.deps import Deps
from tests.fakes import FrozenClock

TEST_DSN = os.environ.get("TEST_DATABASE_URL")
CAP = 5
FRIENDS = 15


@pytest.fixture
async def storage(clock: FrozenClock) -> AsyncIterator[PostgresStorage]:
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
        yield PostgresStorage(engine, clock=clock)
    finally:
        await engine.dispose()


async def test_friends_starting_at_once_do_not_pass_the_daily_cap(
    deps: Deps, storage: PostgresStorage
) -> None:
    deps = replace(
        deps, settings=replace(deps.settings, referral_daily_reward_limit=CAP)
    )
    referrer = await storage.create_user(
        messenger=MessengerKind.TELEGRAM,
        external_id="1",
        referral_code="inviter",
        support_number=support.generate_number(),
        bonus_images=0,
        bonus_documents=0,
    )

    await asyncio.gather(
        *(
            onboarding.start(
                deps,
                Chat(messenger=MessengerKind.TELEGRAM, chat_id=str(100 + n)),
                MessengerKind.TELEGRAM,
                str(100 + n),
                referral_payload("inviter"),
            )
            for n in range(FRIENDS)
        )
    )

    since = deps.now() - timedelta(days=1)
    assert await storage.count_referrals_since(referrer.id, since) == CAP
    rewarded = await storage.get_user_by_id(referrer.id)
    assert rewarded is not None
    assert rewarded.bonus_images == CAP * deps.settings.referral_bonus_images


async def test_the_cap_holds_in_the_record_itself(storage: PostgresStorage) -> None:
    """Сама запись не уходит за потолок, сколько бы попыток ни пришло разом."""
    referrer = await storage.create_user(
        messenger=MessengerKind.TELEGRAM,
        external_id="1",
        referral_code="inviter",
        support_number=support.generate_number(),
        bonus_images=0,
        bonus_documents=0,
    )
    friends = [
        await storage.create_user(
            messenger=MessengerKind.TELEGRAM,
            external_id=str(100 + n),
            referral_code=f"friend{n}",
            support_number=support.generate_number(),
            bonus_images=0,
            bonus_documents=0,
        )
        for n in range(FRIENDS)
    ]
    since = storage._now() - timedelta(days=1)

    recorded = await asyncio.gather(
        *(
            storage.record_referral(referrer.id, friend.id, since=since, limit=CAP)
            for friend in friends
        )
    )

    assert sum(recorded) == CAP
    assert await storage.count_referrals_since(referrer.id, since) == CAP
