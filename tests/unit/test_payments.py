"""Оплата: тариф выдаётся только по подтверждённой оплате и ровно один раз.

Симметрия с главным инвариантом проекта неслучайна. Там лимит не списывается,
пока результат не доставлен; здесь тариф не выдаётся, пока деньги не
подтверждены. Оба правила про одно и то же — не брать чужого и не отдавать
своего по чужому слову.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from app.adapters.storage.memory import InMemoryStorage
from app.core import texts
from app.core.models import TariffId, User
from app.core.scenarios import payments
from app.core.scenarios.deps import Deps, Session
from app.core.tariffs import tariff_of
from app.ports.payments import PaymentMethod, PaymentStatus
from tests.fakes import FakeCards, FakeMessenger, FakeStars

PRO = TariffId.PRO


# --- Выбор способа -------------------------------------------------------


async def test_both_methods_are_offered_when_both_are_configured(
    deps: Deps, session: Session, messenger: FakeMessenger
) -> None:
    await payments.choose_method(deps, session, PRO)

    keyboard = messenger.last_text.keyboard
    assert keyboard is not None
    labels = [button.text for row in keyboard.rows for button in row]
    assert labels == [texts.BUTTON_PAY_CARD, texts.BUTTON_PAY_STARS]


async def test_the_star_price_is_named_before_paying(
    deps: Deps, session: Session, messenger: FakeMessenger
) -> None:
    """Цену в звёздах человек всё равно увидит на счёте — лучше здесь."""
    await payments.choose_method(deps, session, PRO)

    assert "⭐" in messenger.last_text.text


async def test_a_single_method_is_not_a_choice(
    deps: Deps, session: Session, stars: FakeStars
) -> None:
    """В MAX карт нет альтернативы — не заставляем нажимать лишний раз."""
    await payments.choose_method(replace(deps, cards=None), session, PRO)

    assert len(stars.invoices) == 1


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


async def test_a_star_payment_sends_an_invoice(
    deps: Deps, session: Session, stars: FakeStars
) -> None:
    await payments.start_stars(deps, session, PRO)

    assert len(stars.invoices) == 1
    assert stars.invoices[0].stars > 0


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
        daily_image_quota=3,
    )
    order = await storage.create_payment(
        user_id=stranger.id,
        tariff=PRO,
        method=PaymentMethod.STARS.value,
        amount=100,
        currency="XTR",
    )

    await payments.approve(deps, "req-1", order.id, user=session.user)

    assert stars.approvals == [("req-1", False)]


async def test_an_already_paid_order_is_refused(
    deps: Deps, session: Session, storage: InMemoryStorage, stars: FakeStars
) -> None:
    """Второй счёт по оплаченному заказу — это вторые деньги за то же."""
    await payments.start_stars(deps, session, PRO)
    order_id = stars.invoices[0].order_id
    await storage.mark_paid(order_id)

    await payments.approve(deps, "req-2", order_id, user=session.user)

    assert stars.approvals == [("req-2", False)]


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
