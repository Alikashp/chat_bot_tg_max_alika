"""Оплата: тариф выдаётся только по подтверждённой оплате и ровно один раз.

Симметрия с главным инвариантом проекта неслучайна. Там лимит не списывается,
пока результат не доставлен; здесь тариф не выдаётся, пока деньги не
подтверждены. Оба правила про одно и то же — не брать чужого и не отдавать
своего по чужому слову.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest

from app.adapters.storage.memory import InMemoryStorage
from app.core import support, texts
from app.core.billing import Billing
from app.core.models import MessengerKind, TariffId, User
from app.core.scenarios import payments
from app.core.scenarios.deps import Deps, Session
from app.core.tariffs import tariff_of
from app.ports.payments import PaymentMethod, PaymentStatus
from tests.fakes import FakeCards, FakeLogger, FakeMessenger, FakeStars

PRO = TariffId.PRO


# --- Выбор способа -------------------------------------------------------


async def test_both_methods_are_offered_when_both_are_configured(
    deps: Deps, session: Session, messenger: FakeMessenger
) -> None:
    await payments.choose_method(deps, session, PRO)

    keyboard = messenger.last_text.keyboard
    assert keyboard is not None
    assert [button.text for button in keyboard.rows[0]] == [
        texts.BUTTON_PAY_CARD,
        texts.BUTTON_PAY_STARS,
    ]


async def test_the_star_price_is_named_before_paying(
    deps: Deps, session: Session, messenger: FakeMessenger
) -> None:
    """Цену в звёздах человек всё равно увидит на счёте — лучше здесь."""
    await payments.choose_method(deps, session, PRO)

    assert "⭐" in messenger.last_text.text


async def test_a_single_method_goes_straight_to_the_terms(
    deps: Deps, session: Session, messenger: FakeMessenger, stars: FakeStars
) -> None:
    """Выбирать не из чего — значит и спрашивать не о чем.

    Условия при этом не теряются: они на экране заказа, ровно над кнопкой
    оплаты. Раньше их показывал экран выбора, и лишний шаг был обязателен;
    теперь согласие стоит там же, где действие, которым его дают.
    """
    await payments.choose_method(replace(deps, cards=None), session, PRO)

    assert len(stars.invoices) == 1
    assert texts.CONSENT in messenger.last_text.text


async def test_the_order_screen_links_to_both_documents(
    deps: Deps, session: Session, messenger: FakeMessenger
) -> None:
    """Согласие без возможности прочитать — не согласие."""
    await payments.start_card(deps, session, PRO)

    keyboard = messenger.last_text.keyboard
    assert keyboard is not None
    links = {button.text: button.url for button in keyboard.rows[1]}
    assert links == {
        texts.BUTTON_OFFER: deps.settings.offer_url,
        texts.BUTTON_PRIVACY: deps.settings.privacy_url,
    }


async def test_the_consent_stands_next_to_the_pay_button(
    deps: Deps, session: Session, messenger: FakeMessenger
) -> None:
    """§4.11 оферты: согласие даётся нажатием кнопки оплаты.

    Значит и условия должны быть в том же сообщении. Разнеси их по разным —
    и согласие получено вслепую.
    """
    await payments.start_card(deps, session, PRO)

    keyboard = messenger.last_text.keyboard
    assert keyboard is not None
    assert texts.CONSENT in messenger.last_text.text
    assert keyboard.rows[0][0].text == texts.BUTTON_PAY_OPEN


async def test_payment_is_hidden_until_the_documents_are_published(
    deps: Deps, session: Session, messenger: FakeMessenger, stars: FakeStars
) -> None:
    """Брать деньги, не показав условия, нельзя — значит и кнопки быть не должно."""
    without_docs = replace(
        deps, settings=replace(deps.settings, offer_url="", docs_version="")
    )

    await payments.choose_method(without_docs, session, PRO)

    assert messenger.texts_said() == [texts.PAYMENTS_SOON]
    assert stars.invoices == []


async def test_the_consent_is_recorded_with_the_order(
    deps: Deps, session: Session, storage: InMemoryStorage, stars: FakeStars
) -> None:
    """Версия документов живёт вместе с заказом столько же, сколько платёж."""
    await payments.start_stars(deps, session, PRO)

    order = await storage.get_payment(stars.invoices[0].order_id)
    assert order is not None
    assert order.docs_version == deps.settings.docs_version


async def test_the_consent_is_written_to_the_log_on_payment(
    deps: Deps, session: Session, logger: FakeLogger, stars: FakeStars
) -> None:
    """Кто, когда, по какой редакции и за какой заказ — одной записью.

    Запись появляется по факту оплаты, а не при открытии экрана: соглашается
    человек нажатием кнопки, а брошенный заказ никакого согласия не значит.
    """
    await payments.start_stars(deps, session, PRO)
    assert [e for e in logger.events if e.event == "consent_accepted"] == []

    await payments.confirm(deps, stars.invoices[0].order_id)

    consent = [entry for entry in logger.events if entry.event == "consent_accepted"]
    assert len(consent) == 1
    assert consent[0].fields["user_id"] == int(session.user.id)
    assert consent[0].fields["payment_id"] == stars.invoices[0].order_id
    assert consent[0].fields["docs_version"] == deps.settings.docs_version


async def test_without_any_provider_there_is_no_dead_end(
    deps: Deps, session: Session, messenger: FakeMessenger
) -> None:
    await payments.choose_method(replace(deps, cards=None, stars=None), session, PRO)

    assert messenger.last_text.text == texts.PAYMENTS_SOON
    assert messenger.last_text.keyboard is not None


# --- Оплата картой -------------------------------------------------------


async def test_a_card_payment_gives_a_link(
    deps: Deps, session: Session, messenger: FakeMessenger, cards: FakeCards
) -> None:
    await payments.start_card(deps, session, PRO)

    assert cards.created[0][1] == tariff_of(PRO).price_rub
    keyboard = messenger.last_text.keyboard
    assert keyboard is not None
    assert keyboard.rows[0][0].url == "https://pay.example/checkout"


async def test_the_order_is_recorded_before_the_provider_is_asked(
    deps: Deps, session: Session, storage: InMemoryStorage, cards: FakeCards
) -> None:
    """Заказ заводится до обращения к провайдеру.

    Порядок именно такой, потому что идентификатор заказа нужен провайдеру
    ключом идемпотентности. Заводить его после ответа значило бы не иметь
    ключа в момент, когда он нужен.
    """
    await payments.start_card(deps, session, PRO)

    order_id, amount = cards.created[0]
    order = await storage.get_payment(order_id)
    assert order is not None
    assert order.amount == amount
    assert order.status == PaymentStatus.PENDING.value


async def test_a_failed_provider_does_not_leave_the_user_in_silence(
    deps: Deps, session: Session, messenger: FakeMessenger, cards: FakeCards
) -> None:
    cards.error = RuntimeError("провайдер лёг")

    await payments.start_card(deps, session, PRO)

    assert messenger.texts_said() == [texts.PAYMENT_FAILED]


async def test_a_provider_without_a_link_is_a_failure(
    deps: Deps, session: Session, messenger: FakeMessenger, cards: FakeCards
) -> None:
    """Сообщение «оплата по кнопке» без кнопки — это тупик."""
    cards.confirmation_url = None

    await payments.start_card(deps, session, PRO)

    assert messenger.texts_said() == [texts.PAYMENT_FAILED]


# --- Оплата звёздами -----------------------------------------------------


async def test_a_star_payment_offers_a_subscription(
    deps: Deps, session: Session, stars: FakeStars, messenger: FakeMessenger
) -> None:
    """Счёт на звёздах теперь всегда подписка: разового варианта у неё нет."""
    await payments.start_stars(deps, session, PRO)

    assert len(stars.invoices) == 1
    assert stars.invoices[0].stars > 0
    assert stars.invoices[0].period_days == deps.settings.subscription_days
    keyboard = messenger.last_text.keyboard
    assert keyboard is not None
    assert keyboard.rows[0][0].url is not None


async def test_the_invoice_carries_the_order(
    deps: Deps, session: Session, storage: InMemoryStorage, stars: FakeStars
) -> None:
    """По этому идентификатору потом опознаётся оплата."""
    await payments.start_stars(deps, session, PRO)

    order = await storage.get_payment(stars.invoices[0].order_id)
    assert order is not None
    assert order.method == PaymentMethod.STARS.value
    assert order.currency == "XTR"


# --- Запрос перед списанием ----------------------------------------------


async def test_a_known_order_is_approved(
    deps: Deps, session: Session, stars: FakeStars
) -> None:
    await payments.start_stars(deps, session, PRO)
    order_id = stars.invoices[0].order_id

    await payments.approve(deps, "req-1", order_id, user=session.user)

    assert stars.approvals == [("req-1", True)]


async def test_an_unknown_order_is_refused(
    deps: Deps, session: Session, stars: FakeStars
) -> None:
    """Согласие вслепую — это списанные деньги, за которые нечего выдать."""
    await payments.approve(deps, "req-1", "нет такого заказа", user=session.user)

    assert stars.approvals == [("req-1", False)]


async def test_someone_elses_order_is_refused(
    deps: Deps, session: Session, storage: InMemoryStorage, stars: FakeStars
) -> None:
    """Идентификатор заказа приходит снаружи — проверяем, чей он."""
    stranger = await storage.create_user(
        messenger=session.user.messenger,
        external_id="999",
        referral_code="stranger",
        support_number=support.generate_number(),
        bonus_images=3,
        bonus_documents=0,
    )
    order = await storage.create_payment(
        user_id=stranger.id,
        tariff=PRO,
        method=PaymentMethod.STARS.value,
        amount=100,
        currency="XTR",
        docs_version="2026-08-31",
    )

    await payments.approve(deps, "req-1", order.id, user=session.user)

    assert stars.approvals == [("req-1", False)]


async def test_a_paid_order_without_a_subscription_is_refused(
    deps: Deps, session: Session, storage: InMemoryStorage, stars: FakeStars
) -> None:
    """Старая ссылка на счёт, открытая второй раз, — это вторые деньги за то же."""
    await payments.start_stars(deps, session, PRO)
    order_id = stars.invoices[0].order_id
    await storage.mark_paid(order_id)

    await payments.approve(deps, "req-2", order_id, user=session.user)

    assert stars.approvals == [("req-2", False)]


async def test_a_renewal_of_a_live_subscription_is_approved(
    deps: Deps, session: Session, stars: FakeStars
) -> None:
    """Продление приходит по тому же заказу, который давно оплачен.

    Отказать здесь значило бы остановить подписку, за которую человек
    платит: Telegram спрашивает про списание тем же запросом, что и в первый
    раз, и ссылается на тот же счёт.
    """
    await payments.start_stars(deps, session, PRO)
    order_id = stars.invoices[0].order_id
    await payments.confirm(deps, order_id, charge_id="charge-1")

    await payments.approve(deps, "req-2", order_id, user=session.user)

    assert stars.approvals == [("req-2", True)]


# --- Одна подписка — один способ оплаты ----------------------------------


async def test_a_star_subscriber_is_not_charged_by_card_too(
    deps: Deps,
    session: Session,
    stars: FakeStars,
    cards: FakeCards,
    messenger: FakeMessenger,
) -> None:
    """Звёздную подписку Telegram продлевает сам, и наша карта её не отменит.

    Купи человек поверх неё тариф картой — платил бы дважды за один период:
    звёздами в Telegram и картой у нас.
    """
    await payments.start_stars(deps, session, PRO)
    await payments.confirm(deps, stars.invoices[0].order_id, charge_id="charge-1")

    await payments.start_card(deps, session, PRO)

    assert cards.created == []
    screen = texts.subscription_other_method(by_stars=True)
    assert messenger.last_text.text == screen.text
    assert messenger.last_text.keyboard is not None


async def test_a_card_subscriber_is_not_charged_in_stars_too(
    deps: Deps, session: Session, stars: FakeStars, messenger: FakeMessenger
) -> None:
    cards = FakeCards(recurring=True)
    recurring = replace(deps, cards=cards)
    await payments.start_card(recurring, session, PRO)
    await payments.confirm(recurring, cards.created[0][0])

    await payments.start_stars(recurring, session, PRO)

    assert stars.invoices == []
    screen = texts.subscription_other_method(by_stars=False)
    assert messenger.last_text.text == screen.text


async def test_a_cancelled_subscription_does_not_block_the_other_method(
    deps: Deps, session: Session, stars: FakeStars, storage: InMemoryStorage
) -> None:
    """Продление отключено — второго списания не будет, платить можно чем угодно."""
    cards = FakeCards(recurring=True)
    recurring = replace(deps, cards=cards)
    await payments.start_card(recurring, session, PRO)
    await payments.confirm(recurring, cards.created[0][0])
    await storage.cancel_subscription(session.user.id, deps.now())

    await payments.start_stars(recurring, session, PRO)

    assert len(stars.invoices) == 1


async def test_the_same_method_can_change_the_tariff(
    deps: Deps, session: Session
) -> None:
    """Смена тарифа картой при карточной подписке — та же подписка, не вторая."""
    cards = FakeCards(recurring=True)
    recurring = replace(deps, cards=cards)
    await payments.start_card(recurring, session, PRO)
    await payments.confirm(recurring, cards.created[0][0])

    await payments.start_card(recurring, session, TariffId.MAX)

    assert len(cards.created) == 2


# --- Т0: никогда две подписки сразу --------------------------------------


async def test_a_new_star_subscription_cancels_the_old_one(
    deps: Deps, session: Session, stars: FakeStars, storage: InMemoryStorage
) -> None:
    """Смена тарифа звёздами — это вторая подписка у Telegram, а не та же.

    Каждая звёздная подписка списывает сама. Оставь прежнюю живой — и
    человек платил бы звёздами за оба тарифа сразу, сколько бы мы ни
    переписывали свою запись.
    """
    await payments.start_stars(deps, session, PRO)
    await payments.confirm(deps, stars.invoices[0].order_id, charge_id="charge-pro")

    await payments.start_stars(deps, session, TariffId.MAX)
    await payments.confirm(deps, stars.invoices[1].order_id, charge_id="charge-max")

    assert stars.cancelled == [(session.user.external_id, "charge-pro")]
    subscription = await storage.get_subscription(session.user.id)
    assert subscription is not None
    assert subscription.tariff is TariffId.MAX
    # Отменять в следующий раз придётся новую: по ней Telegram и списывает.
    assert subscription.charge_id == "charge-max"


async def test_a_card_link_paid_after_stars_cancels_the_stars(
    deps: Deps, session: Session, stars: FakeStars, storage: InMemoryStorage
) -> None:
    """Ссылка на карту получена до звёзд, а оплачена после.

    На входе это не поймать: когда человек брал ссылку, подписки ещё не
    было. Ловится только при подтверждении — там, где деньги уже взяты.
    """
    cards = FakeCards(recurring=True)
    both = replace(deps, cards=cards)
    await payments.start_card(both, session, PRO)
    await payments.start_stars(both, session, PRO)
    await payments.confirm(both, stars.invoices[0].order_id, charge_id="charge-1")

    await payments.confirm(both, cards.created[0][0])

    assert stars.cancelled == [(session.user.external_id, "charge-1")]
    subscription = await storage.get_subscription(session.user.id)
    assert subscription is not None
    assert subscription.method == PaymentMethod.CARD.value


async def test_stars_paid_after_a_card_replace_the_card_subscription(
    deps: Deps, session: Session, stars: FakeStars, storage: InMemoryStorage
) -> None:
    """Обратный порядок: карточную подписку списываем мы сами.

    Запись о подписке у человека одна, и новая её заменяет — значит, карту
    больше не тронет ни один проход списаний. Звёздную при этом отменять
    нечего: она и есть новая.
    """
    cards = FakeCards(recurring=True)
    both = replace(deps, cards=cards)
    await payments.start_stars(both, session, PRO)
    await payments.start_card(both, session, PRO)
    await payments.confirm(both, cards.created[0][0])

    await payments.confirm(both, stars.invoices[0].order_id, charge_id="charge-1")

    assert stars.cancelled == []
    subscription = await storage.get_subscription(session.user.id)
    assert subscription is not None
    assert subscription.method == PaymentMethod.STARS.value
    assert subscription.payment_method_id is None


async def test_a_one_time_card_payment_ends_a_star_subscription(
    deps: Deps, session: Session, stars: FakeStars, storage: InMemoryStorage
) -> None:
    """Разовая оплата картой новой подписки не заводит — но и старую не терпит.

    Иначе звёзды продолжили бы списываться за срок, уже оплаченный картой.
    """
    cards = FakeCards(recurring=False)
    both = replace(deps, cards=cards)
    await payments.start_card(both, session, PRO)
    await payments.start_stars(both, session, PRO)
    await payments.confirm(both, stars.invoices[0].order_id, charge_id="charge-1")

    await payments.confirm(both, cards.created[0][0])

    assert stars.cancelled == [(session.user.external_id, "charge-1")]
    subscription = await storage.get_subscription(session.user.id)
    assert subscription is not None
    assert subscription.status == "cancelled"


async def test_a_renewal_does_not_cancel_its_own_subscription(
    deps: Deps, session: Session, stars: FakeStars, storage: InMemoryStorage
) -> None:
    """Продление — та же подписка. Отменить её значило бы прекратить оплаченное."""
    await payments.start_stars(deps, session, PRO)
    order_id = stars.invoices[0].order_id
    await payments.confirm(deps, order_id, charge_id="charge-1")

    await payments.confirm(deps, order_id, charge_id="charge-2", renewal=True)

    assert stars.cancelled == []
    subscription = await storage.get_subscription(session.user.id)
    assert subscription is not None
    assert subscription.charge_id == "charge-1"


async def test_a_failed_cancel_still_grants_and_is_queued(
    deps: Deps,
    session: Session,
    stars: FakeStars,
    storage: InMemoryStorage,
    logger: FakeLogger,
) -> None:
    """Деньги за новую подписку уже взяты — тариф выдаётся в любом случае.

    А прежняя подписка, которую Telegram не дал отменить, встаёт в очередь:
    отмену повторит следующий проход биллинга (0а).
    """
    await payments.start_stars(deps, session, PRO)
    await payments.confirm(deps, stars.invoices[0].order_id, charge_id="charge-pro")
    stars.cancel_error = RuntimeError("telegram is down")

    await payments.start_stars(deps, session, TariffId.MAX)
    granted = await payments.confirm(
        deps, stars.invoices[1].order_id, charge_id="charge-max"
    )

    assert granted is not None
    user = await storage.get_user_by_id(session.user.id)
    assert user is not None and user.tariff is TariffId.MAX
    queued = await storage.star_cancels_due(limit=10)
    assert [(c.user_id, c.charge_id) for c in queued] == [
        (session.user.id, "charge-pro")
    ]
    assert "subscription_replace_failed" in logger.names()


async def test_the_failed_cancel_is_retried_until_it_goes_through(
    deps: Deps, session: Session, stars: FakeStars, storage: InMemoryStorage
) -> None:
    """0а: отмена повторяется каждым проходом, пока Telegram её не примет.

    Ручной отмены у заказчика нет: если повтор не случится сам, человек
    будет платить звёздами за две подписки.
    """
    await payments.start_stars(deps, session, PRO)
    await payments.confirm(deps, stars.invoices[0].order_id, charge_id="charge-pro")
    stars.cancel_error = RuntimeError("telegram is down")
    await payments.start_stars(deps, session, TariffId.MAX)
    await payments.confirm(deps, stars.invoices[1].order_id, charge_id="charge-max")
    billing = Billing(by_messenger={MessengerKind.TELEGRAM: deps})

    await billing.run()
    assert stars.cancelled == []
    assert len(await storage.star_cancels_due(limit=10)) == 1

    stars.cancel_error = None
    await billing.run()
    await billing.run()

    assert stars.cancelled == [(session.user.external_id, "charge-pro")]
    assert await storage.star_cancels_due(limit=10) == []


# --- Подтверждение -------------------------------------------------------


async def test_a_confirmed_payment_grants_the_tariff(
    deps: Deps, session: Session, storage: InMemoryStorage, stars: FakeStars
) -> None:
    await payments.start_stars(deps, session, PRO)

    order = await payments.confirm(deps, stars.invoices[0].order_id)

    assert order is not None
    user = await storage.get_user_by_id(session.user.id)
    assert user is not None
    assert user.tariff is PRO
    assert user.tariff_expires_at == deps.now() + timedelta(days=30)


async def test_the_same_payment_is_never_granted_twice(
    deps: Deps, session: Session, storage: InMemoryStorage, stars: FakeStars
) -> None:
    """Уведомления об оплате приходят по несколько раз.

    Продлевать подписку на каждое значило бы дарить месяцы за одну оплату.
    """
    await payments.start_stars(deps, session, PRO)
    order_id = stars.invoices[0].order_id

    first = await payments.confirm(deps, order_id)
    second = await payments.confirm(deps, order_id)
    third = await payments.confirm(deps, order_id)

    assert first is not None
    assert second is None and third is None
    user = await storage.get_user_by_id(session.user.id)
    assert user is not None
    assert user.tariff_expires_at == deps.now() + timedelta(days=30)


async def test_an_unknown_order_grants_nothing(
    deps: Deps, session: Session, storage: InMemoryStorage
) -> None:
    """Иначе достаточно было бы прислать выдуманный номер заказа."""
    assert await payments.confirm(deps, "выдуманный заказ") is None

    user = await storage.get_user_by_id(session.user.id)
    assert user is not None
    assert user.tariff is TariffId.FREE


async def test_the_order_status_ends_up_paid(
    deps: Deps, session: Session, storage: InMemoryStorage, stars: FakeStars
) -> None:
    await payments.start_stars(deps, session, PRO)
    order_id = stars.invoices[0].order_id

    await payments.confirm(deps, order_id)

    order = await storage.get_payment(order_id)
    assert order is not None
    assert order.status == PaymentStatus.PAID.value
    assert order.paid_at is not None


# --- Продление -----------------------------------------------------------


async def test_paying_again_extends_from_the_old_date(
    deps: Deps,
    session: Session,
    storage: InMemoryStorage,
    stars: FakeStars,
    user: User,
) -> None:
    """Оплативший заранее не должен терять остаток."""
    until = deps.now() + timedelta(days=10)
    await storage.set_tariff(user.id, PRO, until)
    refreshed = await storage.get_user_by_id(user.id)
    assert refreshed is not None

    await payments.start_stars(deps, replace(session, user=refreshed), PRO)
    await payments.confirm(deps, stars.invoices[0].order_id)

    after = await storage.get_user_by_id(user.id)
    assert after is not None
    assert after.tariff_expires_at == until + timedelta(days=30)


async def test_switching_tariffs_starts_the_term_over(
    deps: Deps,
    session: Session,
    storage: InMemoryStorage,
    stars: FakeStars,
    user: User,
) -> None:
    """Остаток чужого тарифа пересчитывать не во что."""
    await storage.set_tariff(user.id, TariffId.LITE, deps.now() + timedelta(days=10))
    refreshed = await storage.get_user_by_id(user.id)
    assert refreshed is not None

    await payments.start_stars(deps, replace(session, user=refreshed), PRO)
    await payments.confirm(deps, stars.invoices[0].order_id)

    after = await storage.get_user_by_id(user.id)
    assert after is not None
    assert after.tariff is PRO
    assert after.tariff_expires_at == deps.now() + timedelta(days=30)


async def test_an_expired_subscription_does_not_extend_the_past(
    deps: Deps,
    session: Session,
    storage: InMemoryStorage,
    stars: FakeStars,
    user: User,
) -> None:
    """Иначе оплата после перерыва давала бы срок, начавшийся вчера."""
    await storage.set_tariff(user.id, PRO, deps.now() - timedelta(days=5))
    refreshed = await storage.get_user_by_id(user.id)
    assert refreshed is not None

    await payments.start_stars(deps, replace(session, user=refreshed), PRO)
    await payments.confirm(deps, stars.invoices[0].order_id)

    after = await storage.get_user_by_id(user.id)
    assert after is not None
    assert after.tariff_expires_at == deps.now() + timedelta(days=30)


# --- Что видит человек ---------------------------------------------------


async def test_the_user_is_told_the_tariff_is_on(
    deps: Deps, session: Session, messenger: FakeMessenger, stars: FakeStars
) -> None:
    await payments.start_stars(deps, session, PRO)
    order = await payments.confirm(deps, stars.invoices[0].order_id)
    assert order is not None

    await payments.announce(deps, session, order)

    assert "Про" in messenger.last_text.text
    assert messenger.last_text.show_menu is True


async def test_an_unknown_person_is_refused_but_still_answered(
    deps: Deps, stars: FakeStars
) -> None:
    """Без ответа платёж повиснет, а заводить человека на этом событии незачем."""
    await payments.approve(deps, "req-1", "любой заказ", user=None)

    assert stars.approvals == [("req-1", False)]


async def test_a_payment_we_cannot_record_is_never_offered(
    deps: Deps, session: Session, storage: InMemoryStorage, messenger: FakeMessenger
) -> None:
    """Ссылка на платёж, который нельзя подтвердить, — это отданные деньги.

    Такого быть не должно: ключом идемпотентности служит наш заказ. Но если
    провайдер всё же вернул чужой платёж, отдавать ссылку нельзя.
    """
    taken = await storage.create_payment(
        user_id=session.user.id,
        tariff=PRO,
        method=PaymentMethod.CARD.value,
        amount=599,
        currency="RUB",
        docs_version="2026-08-31",
    )
    await storage.attach_external_id(taken.id, "ext-1")

    await payments.start_card(deps, session, PRO)

    assert messenger.texts_said() == [texts.PAYMENT_FAILED]


# --- Срок подписки -------------------------------------------------------


async def test_an_expired_subscription_falls_back_to_free(
    deps: Deps, session: Session, storage: InMemoryStorage, user: User
) -> None:
    """Оплата даёт месяц, а не навсегда.

    Без этой проверки запись о сроке в базе была бы, а читать её было бы
    некому: человек платил один раз и оставался на Про пожизненно.
    """
    await storage.set_tariff(user.id, PRO, deps.now() - timedelta(seconds=1))
    expired = await storage.get_user_by_id(user.id)
    assert expired is not None

    active = replace(session, user=expired)

    assert active.tariff.id is TariffId.FREE
    assert active.tariff.daily_messages == 20


async def test_a_live_subscription_gives_its_tariff(
    deps: Deps, session: Session, storage: InMemoryStorage, user: User
) -> None:
    await storage.set_tariff(user.id, PRO, deps.now() + timedelta(days=1))
    paid = await storage.get_user_by_id(user.id)
    assert paid is not None

    assert replace(session, user=paid).tariff.id is PRO


async def test_a_paid_tariff_without_a_date_does_not_last_forever(
    deps: Deps, session: Session, storage: InMemoryStorage, user: User
) -> None:
    """Тариф без срока — это либо ошибка выдачи, либо ручная правка базы.

    Считать такую запись вечной подпиской опаснее, чем вернуть человека на
    бесплатный: во втором случае он пожалуется, в первом — не заплатит.
    """
    await storage.set_tariff(user.id, PRO, None)
    odd = await storage.get_user_by_id(user.id)
    assert odd is not None

    assert replace(session, user=odd).tariff.id is TariffId.FREE


async def test_the_free_tariff_never_expires(session: Session) -> None:
    assert session.user.tariff is TariffId.FREE
    assert session.tariff.id is TariffId.FREE


# --- Атомарная выдача (фаза 11, П4) --------------------------------------


async def test_a_failure_inside_the_grant_leaves_the_order_to_retry(
    deps: Deps,
    session: Session,
    storage: InMemoryStorage,
    stars: FakeStars,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """П4: выдача упала — заказ не paid и тариф прежний; повтор доводит её."""
    await payments.start_stars(deps, session, PRO)
    order_id = stars.invoices[0].order_id
    real = storage.complete_payment

    async def broken(*args: object, **kwargs: object) -> object:
        raise RuntimeError("база отвалилась посреди выдачи")

    monkeypatch.setattr(storage, "complete_payment", broken)
    with pytest.raises(RuntimeError):
        await payments.confirm(deps, order_id)

    order = await storage.get_payment(order_id)
    assert order is not None and order.status == PaymentStatus.PENDING.value
    user = await storage.get_user_by_id(session.user.id)
    assert user is not None and user.tariff is TariffId.FREE

    monkeypatch.setattr(storage, "complete_payment", real)
    assert await payments.confirm(deps, order_id) is not None
    assert await payments.confirm(deps, order_id) is None


async def test_the_provider_is_asked_before_the_grant(
    deps: Deps,
    session: Session,
    storage: InMemoryStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Обращения к провайдеру — до транзакции выдачи, а не внутри неё."""
    cards = FakeCards(recurring=True)
    with_cards = replace(deps, cards=cards)
    steps: list[str] = []
    real_method, real_grant = cards.saved_method_of, storage.complete_payment

    async def method(external_id: str) -> str | None:
        steps.append("провайдер")
        return await real_method(external_id)

    async def grant(*args: object, **kwargs: object) -> object:
        steps.append("выдача")
        return await real_grant(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(cards, "saved_method_of", method)
    monkeypatch.setattr(storage, "complete_payment", grant)
    await payments.start_card(with_cards, session, PRO)
    order_id = cards.created[0][0]

    await payments.confirm(with_cards, order_id)

    assert steps == ["провайдер", "выдача"]
    subscription = await storage.get_subscription(session.user.id)
    assert subscription is not None and subscription.payment_method_id == "card-1"


async def test_a_concurrent_extension_is_recounted_not_lost(
    deps: Deps, session: Session, storage: InMemoryStorage, stars: FakeStars
) -> None:
    """Два разных заказа почти разом: оба месяца на месте, а не один."""
    await payments.start_stars(deps, session, PRO)
    await payments.start_stars(deps, session, PRO)
    first, second = (invoice.order_id for invoice in stars.invoices)

    await asyncio.gather(payments.confirm(deps, first), payments.confirm(deps, second))

    user = await storage.get_user_by_id(session.user.id)
    assert user is not None
    assert user.tariff_expires_at == deps.now() + timedelta(days=60)
