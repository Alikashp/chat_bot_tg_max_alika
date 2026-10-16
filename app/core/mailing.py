"""Цепочка реактивации R1–R3 (сессия 12, шаг 3). Только Telegram.

Человеку, который замолчал, бот пишет первым не больше трёх раз за цикл
неактивности: на 3–5, 10–12 и 21–23 сутки от ``last_active_at``. Любое
действие человека сдвигает эту отметку, и цикл начинается заново.

Почему шаг уходит только внутри своего интервала и не догоняется: R1 на
шестые сутки — уже не «есть тема следующего доклада?», а навязчивость. Если
сервис лежал или проход упал, шаг честно пропадает.

Почему отправка идёт мимо очереди обновлений: рассылка — не ответ человеку,
и стоять в одной очереди с ответами ей нельзя (правило 6). Темп — своим
ограничителем, ``pace``, по одному сообщению за раз.

Защита от двойной отправки — в базе: строка (человек, шаг, ключ цикла)
заводится до отправки, и второй проход, параллельный или повторный, её не
заведёт. Ключ цикла — ``last_active_at`` на момент отправки.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.core import gifts, pending, texts
from app.core.limits import LimitKind
from app.core.models import (
    Keyboard,
    MailingStatus,
    MessengerKind,
    TariffId,
    User,
    UserId,
)
from app.core.referral import referral_url
from app.core.scenarios import keyboards
from app.core.scenarios.deps import Deps, session_for
from app.core.tariffs import active_tariff
from app.ports.messenger import RecipientGoneError


@dataclass(frozen=True, slots=True)
class Step:
    """Шаг цепочки: имя и интервал неактивности, в котором он уходит."""

    name: str
    after: timedelta
    before: timedelta


R1 = Step("R1", timedelta(days=3), timedelta(days=5))
R2 = Step("R2", timedelta(days=10), timedelta(days=12))
R3 = Step("R3", timedelta(days=21), timedelta(days=23))
STEPS = (R1, R2, R3)

#: Окно отправки по местному времени (по умолчанию Москва): с 15:00 до 20:00.
SEND_FROM_HOUR = 15
SEND_UNTIL_HOUR = 20

#: Не больше одного маркетингового сообщения в 7 дней на человека.
MARKETING_EVERY = timedelta(days=7)

#: Сколько раз пробовать записать исход уже ушедшего сообщения.
FINISH_ATTEMPTS = 3

#: Сколько кандидатов брать из базы за раз.
BATCH = 100


async def _no_pace() -> None:
    return None


@dataclass(slots=True)
class Mailer:
    """Один проход рассылки. Запускается по расписанию, сам ничего не ждёт."""

    deps: Deps
    #: Ограничитель темпа: вызывается перед каждой отправкой. В сервисе —
    #: ведро на 20 сообщений в секунду.
    pace: Callable[[], Awaitable[None]] = _no_pace
    batch: int = BATCH

    async def run(self) -> None:
        settings = self.deps.settings
        if not settings.mailings_enabled:
            return
        # Все три текста — про презентации (R3 обещает другу презентацию в
        # подарок). Без раздела презентаций обещать нечего.
        if self.deps.presentations is None:
            return
        now = self.deps.now()
        local = now.astimezone(ZoneInfo(settings.timezone))
        if not SEND_FROM_HOUR <= local.hour < SEND_UNTIL_HOUR:
            return
        for step in STEPS:
            if self._enabled(step):
                await self._send_step(step, now)

    def _enabled(self, step: Step) -> bool:
        settings = self.deps.settings
        return {
            R1.name: settings.mailing_r1_enabled,
            R2.name: settings.mailing_r2_enabled,
            R3.name: settings.mailing_r3_enabled,
        }[step.name]

    async def _send_step(self, step: Step, now: datetime) -> None:
        # Кого ядро отсеяло при перепроверке, база вернёт снова: запоминаем,
        # чтобы не ходить по кругу.
        seen: set[UserId] = set()
        while True:
            found = await self.deps.storage.mailing_candidates(
                messenger=MessengerKind.TELEGRAM,
                step=step.name,
                active_after=now - step.before,
                active_before=now - step.after,
                marketing_before=now - MARKETING_EVERY,
                now=now,
                limit=self.batch,
            )
            fresh = [user for user in found if user.id not in seen]
            if not fresh:
                return
            for user in fresh:
                seen.add(user.id)
                await self._send_one(step, user.id, now)
            if len(found) < self.batch:
                return

    async def _send_one(self, step: Step, user_id: UserId, now: datetime) -> None:
        storage = self.deps.storage
        # Перечитываем: пока шла пачка, человек мог написать или оплатить.
        user = await storage.get_user_by_id(user_id)
        if user is None or not _eligible(user, step, now):
            return
        assert user.last_active_at is not None
        cycle = user.last_active_at
        if not await storage.claim_mailing(user.id, step.name, cycle, now):
            return

        await self.pace()
        session = session_for(self.deps, user)
        text, keyboard = self._message(step, user)
        try:
            await self.deps.messenger.send_text(session.chat, text, keyboard=keyboard)
        except RecipientGoneError:
            # Отметку «остановил бота» ставит обёртка мессенджера, как и для
            # любого другого отказа доставки (сессия 8).
            await self._finish(user, step, cycle, MailingStatus.BLOCKED, now)
            return
        except Exception as error:
            # Одна неудачная отправка не валит проход: в строке остаётся
            # только имя класса ошибки — её текст может нести адрес и данные.
            await self._finish(
                user, step, cycle, MailingStatus.ERROR, now, type(error).__name__
            )
            return

        # Подарок — только доставленному R2 и в одной записи с отметкой
        # «доставлено» (сессия 13): не дошло — обещания не было, а «sent» без
        # подарка не бывает.
        gift = (
            gifts.GiftGrant(
                kind=LimitKind.PRESENTATIONS,
                source=gifts.R2_GIFT_SOURCE,
                expires_at=gifts.r2_expiry(now),
            )
            if step is R2
            else None
        )
        if not await self._finish(
            user, step, cycle, MailingStatus.SENT, now, gift=gift
        ):
            return
        if step is not R3 and user.pending is None:
            # Текст обещает: «напиши тему одним сообщением». Значит,
            # следующее сообщение — тема, а не вопрос в чат, но не дольше
            # настройки: через неделю это уже обычный вопрос (сессия 13).
            wait = timedelta(hours=self.deps.settings.mailing_topic_wait_hours)
            await storage.set_pending(user.id, pending.await_mailing_topic(now + wait))

    async def _finish(
        self,
        user: User,
        step: Step,
        cycle: datetime,
        status: MailingStatus,
        now: datetime,
        error_code: str | None = None,
        gift: gifts.GiftGrant | None = None,
    ) -> bool:
        """Записывает исход отправки; True — записан.

        Сообщение уже ушло, и потерять его исход — значит оставить человека
        без обещанного подарка. Поэтому запись повторяется. Повтор безопасен:
        хранилище ставит исход только строке в ``sending``, и если первая
        попытка на деле прошла, а потерялся лишь ответ, вторая ничего не
        меняет — это и есть «записано».
        """
        failed: str | None = None
        for _ in range(FINISH_ATTEMPTS):
            try:
                done = await self.deps.storage.finish_mailing(
                    user.id,
                    step.name,
                    cycle,
                    status=status,
                    at=now,
                    error_code=error_code,
                    gift=gift,
                )
            except Exception as error:
                failed = type(error).__name__
                continue
            if not done and failed is None:
                # Исход уже стоит, и не нами: отправку довёл другой проход.
                return False
            self.deps.logger.info(
                "mailing_sent",
                user_id=user.id,
                step=step.name,
                status=status.value,
                error=error_code,
            )
            return True
        # База так и не приняла исход. Строка остаётся «sending»: шаг этого
        # цикла больше не уйдёт, а «sent» без подарка не появится.
        self.deps.logger.error(
            "mailing_finish_failed", user_id=user.id, step=step.name, error=failed
        )
        return False

    def _message(self, step: Step, user: User) -> tuple[str, Keyboard | None]:
        if step is R1:
            screen = texts.mailing_r1()
            return screen.text, keyboards.mailing(texts.BUTTON_MAILING_R1, R1.name)
        if step is R2:
            screen = texts.mailing_r2()
            return screen.text, keyboards.mailing(texts.BUTTON_MAILING_R2, R2.name)
        settings = self.deps.settings
        url = referral_url(
            settings.referral_link_host, settings.bot_username, user.referral_code
        )
        return texts.mailing_r3(url).text, None


def _eligible(user: User, step: Step, now: datetime) -> bool:
    """Всё верно сразу: Telegram, бесплатный, не остановил, в интервале шага."""
    if user.messenger is not MessengerKind.TELEGRAM:
        return False
    if user.stopped_at is not None or user.last_active_at is None:
        return False
    # Пробный период — это Лайт со сроком, поэтому он здесь тоже отсеивается.
    if active_tariff(user.tariff, user.tariff_expires_at, now) is not TariffId.FREE:
        return False
    quiet = now - user.last_active_at
    if not step.after < quiet <= step.before:
        return False
    marketing = user.last_marketing_at
    return marketing is None or now - marketing > MARKETING_EVERY
