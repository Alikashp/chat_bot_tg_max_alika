"""Все тексты интерфейса.

Единственное место в проекте, где живут строки, которые видит пользователь.
Строка в обработчике — блокирующая ошибка на ревью (§8 задания).

Правила §2.9 проверяет scripts/check_texts.py. Чтобы проверка была не на
глаз, а механической, каждый экран описан объектом Screen: текст плюс подписи
кнопок. Линтер обходит реестр SCREENS и проверяет каждый экран целиком —
включая то, что с него есть куда уйти.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.models import TariffId
from app.core.tariffs import PAID_TARIFFS, TARIFFS


@dataclass(frozen=True, slots=True)
class Screen:
    """Готовое сообщение бота вместе с подписями кнопок под ним."""

    text: str
    buttons: tuple[str, ...] = ()
    #: Чем сообщение заканчивается, если кнопок под ним нет.
    #: Заполняется только там, где следующий шаг очевиден из самого текста
    #: или сообщение живёт считаные секунды и заменяется результатом.
    #: Пустое значение при пустых кнопках линтер считает ошибкой.
    next_step: str = ""

    @property
    def lines(self) -> list[str]:
        return self.text.split("\n")


def plural(count: int, one: str, few: str, many: str) -> str:
    """Русское согласование числительного: 1 картинка, 3 картинки, 5 картинок."""
    if 11 <= count % 100 <= 14:
        return many
    remainder = count % 10
    if remainder == 1:
        return one
    if 2 <= remainder <= 4:
        return few
    return many


def _images(count: int) -> str:
    return f"{count} {plural(count, 'картинка', 'картинки', 'картинок')}"


def _messages(count: int) -> str:
    return f"{count} {plural(count, 'сообщение', 'сообщения', 'сообщений')}"


def _friends(count: int) -> str:
    return f"{count} {plural(count, 'друга', 'друзей', 'друзей')}"


def _rubles(amount: int) -> str:
    """1490 -> «1 490». Разряды разделяем пробелом, как принято в русском."""
    return f"{amount:,}".replace(",", " ")


# --- Постоянное меню (§2.1) ---------------------------------------------

MENU_IMAGES = "🎨 Картинки"
MENU_PRESETS = "🎭 Приколы с фото"
MENU_PROFILE = "👤 Профиль"
MENU_TARIFFS = "⭐ Тарифы"

#: Четвёрка кнопок, доступная с любого экрана. В Telegram это постоянная
#: клавиатура, в MAX постоянных клавиатур не бывает и та же четвёрка
#: прикрепляется к каждому сообщению (docs/research.md §1.6). Ядро про
#: разницу не знает.
MENU: tuple[tuple[str, ...], ...] = (
    (MENU_IMAGES, MENU_PRESETS),
    (MENU_PROFILE, MENU_TARIFFS),
)

# --- Прочие подписи кнопок ----------------------------------------------

BUTTON_RETRY = "Повторить"
BUTTON_NEW_DIALOG = "🔄 Новый диалог"
BUTTON_DRAW_AGAIN = "🔄 Ещё раз"
BUTTON_SHARE = "📤 Поделиться"
BUTTON_SEND_TO_FRIEND = "📤 Отправить другу"
BUTTON_ANOTHER_PRESET = "🎭 Другой прикол"
BUTTON_OPEN_TARIFFS = "⭐ Открыть тарифы"
BUTTON_MY_LINK = "🎁 Моя ссылка"
BUTTON_INVITE_FOR_IMAGES = "🎁 Позвать друга → +5 картинок сразу"
BUTTON_INVITE_FOR_MESSAGES = "🎁 Позвать друга → +50 сообщений сразу"

# --- Названия тарифов ----------------------------------------------------

TARIFF_TITLES: dict[TariffId, str] = {
    TariffId.FREE: "Бесплатный",
    TariffId.LITE: "Лайт",
    TariffId.PRO: "Про",
    TariffId.MAX: "Макс",
}

#: Что обещает каждый платный тариф (§2.8), дословно.
TARIFF_FEATURES: dict[TariffId, str] = {
    TariffId.LITE: "100 сообщений в день · 40 картинок · голосовой ввод",
    TariffId.PRO: "100 сообщений в день, отвечает умнее · 60 картинок",
    TariffId.MAX: "200 сообщений в день · 150 картинок · 2 видео",
}

POPULAR_MARK = "⭐ популярный"


# --- Онбординг (§2.1) ----------------------------------------------------

_GREETING = "Привет! Я отвечу на любой вопрос, решу задачу и сделаю картинку."
_GREETING_FROM_PRESENTATIONS = (
    "Привет! Ты из бота презентаций — здесь ещё чат и картинки. "
    "Держи 5 картинок вместо 3 за переход."
)
_INVITATION = "Просто напиши мне что-нибудь 👇"


def onboarding(
    *,
    daily_messages: int,
    daily_images: int,
    from_presentations: bool = False,
    referral_gift: bool = False,
) -> Screen:
    """Первый экран. Три строки, не больше (§2.1).

    Четвёртая появляется только у приглашённого другом: подарок надо назвать
    сразу, иначе человек не поймёт, откуда у него больше лимитов.
    """
    greeting = _GREETING_FROM_PRESENTATIONS if from_presentations else _GREETING
    lines = [
        greeting,
        _INVITATION,
        f"У тебя {_messages(daily_messages)} в день "
        f"и {_images(daily_images)} бесплатно.",
    ]
    if referral_gift:
        lines.append(REFERRAL_GIFT)
    return Screen(text="\n".join(lines), buttons=_menu_buttons())


#: Приглашённому — в онбординге (§2.7).
REFERRAL_GIFT = "Тебе подарок от друга: +50 сообщений и +5 картинок."

#: Пригласившему — сразу, как только друг нажал /start (§2.7).
REFERRAL_REWARD = "🎁 Твой друг зашёл! Тебе +50 сообщений и +5 картинок."


def _menu_buttons() -> tuple[str, ...]:
    return tuple(button for row in MENU for button in row)


# --- Чат (§2.2) ----------------------------------------------------------

#: Показывается вместо ответа, если провайдер не справился.
#: Вторая половина фразы — обещание, которое обязано быть правдой: лимит
#: списывается только по факту доставленного ответа.
CHAT_ERROR = "Что-то пошло не так, попробуй ещё раз 🤷 Сообщение не потратилось."


def chat_error() -> Screen:
    return Screen(text=CHAT_ERROR, buttons=(BUTTON_RETRY,))


def chat_answer(answer: str, *, offer_new_dialog: bool) -> Screen:
    """Ответ бота.

    Кнопка «Новый диалог» появляется начиная с десятого сообщения (§2.2):
    раньше она только мешает, а к десятому разговор обычно уже ушёл в сторону.
    """
    buttons = (BUTTON_NEW_DIALOG,) if offer_new_dialog else ()
    return Screen(
        text=answer,
        buttons=buttons,
        next_step="ответ на вопрос, меню под рукой",
    )


NEW_DIALOG_STARTED = "Начали заново. О чём поговорим? 👇"


def new_dialog_started() -> Screen:
    return Screen(text=NEW_DIALOG_STARTED, buttons=_menu_buttons())


# --- Картинки (§2.3) -----------------------------------------------------

IMAGE_ASK = "Опиши, что нарисовать. Например: кот-космонавт в стиле аниме"
IMAGE_DRAWING = "Рисую… ~15 сек"
IMAGE_ERROR = "Что-то пошло не так, попробуй ещё раз 🤷 Картинка не потратилась."


def image_ask() -> Screen:
    return Screen(text=IMAGE_ASK, next_step="ждём описание от пользователя")


def image_drawing() -> Screen:
    return Screen(
        text=IMAGE_DRAWING,
        next_step="живёт секунды и заменяется готовой картинкой",
    )


def image_error() -> Screen:
    return Screen(text=IMAGE_ERROR, buttons=(BUTTON_RETRY,))


def image_result() -> Screen:
    return Screen(
        text="",
        buttons=(BUTTON_DRAW_AGAIN, BUTTON_SHARE),
        next_step="сама картинка, подписи не нужно",
    )


def share_caption(bot_username: str, referral_url: str) -> Screen:
    """Подпись к картинке, которой делятся (§2.3).

    Реферальная ссылка здесь не украшение: это единственный виральный канал,
    встроенный прямо в результат, которым и так хочется похвастаться.
    """
    return Screen(
        text=f"Сделано в @{bot_username}\n{referral_url}",
        next_step="готовое сообщение для пересылки",
    )


# --- Пресеты (§2.4) ------------------------------------------------------

PRESETS_ASK = "Выбери, что сделаем с фото:"


def presets_menu(preset_buttons: tuple[str, ...]) -> Screen:
    return Screen(text=PRESETS_ASK, buttons=preset_buttons)


def preset_ask_photo(invitation: str) -> Screen:
    """Приглашение прислать фото. Текст берётся из реестра пресетов."""
    return Screen(text=invitation, next_step="ждём фото от пользователя")


PRESET_WORKING = "Делаю… ~15 сек"
PRESET_ERROR = "Что-то пошло не так, попробуй ещё раз 🤷 Картинка не потратилась."
PHOTO_TOO_BIG = "Фото слишком большое, пришли поменьше 🙏"
PHOTO_NOT_AN_IMAGE = "Это не похоже на фото. Пришли картинку 🙏"


def preset_working() -> Screen:
    return Screen(
        text=PRESET_WORKING,
        next_step="живёт секунды и заменяется готовой картинкой",
    )


def preset_error() -> Screen:
    return Screen(text=PRESET_ERROR, buttons=(BUTTON_RETRY,))


def photo_rejected(reason: str) -> Screen:
    return Screen(text=reason, next_step="ждём другое фото")


def preset_result() -> Screen:
    return Screen(
        text="",
        buttons=(BUTTON_DRAW_AGAIN, BUTTON_SEND_TO_FRIEND, BUTTON_ANOTHER_PRESET),
        next_step="сама картинка, подписи не нужно",
    )


# --- Пейволл (§2.5) ------------------------------------------------------


def paywall_images() -> Screen:
    """Показывается только при исчерпании и всегда даёт два выхода."""
    return Screen(
        text=(
            "Картинки на сегодня закончились 😔\n"
            "Завтра будет ещё одна, а можно не ждать:"
        ),
        buttons=(BUTTON_OPEN_TARIFFS, BUTTON_INVITE_FOR_IMAGES),
    )


def paywall_messages() -> Screen:
    """Тот же экран для сообщений.

    В §2.5 задания дан текст только про картинки, но кончиться могут и
    сообщения — 20 в день на бесплатном тарифе. Оставить этот случай без
    экрана значило бы получить тупик, а тупиков быть не должно.
    """
    return Screen(
        text=(
            "Сообщения на сегодня закончились 😔\nЗавтра будут ещё, а можно не ждать:"
        ),
        buttons=(BUTTON_OPEN_TARIFFS, BUTTON_INVITE_FOR_MESSAGES),
    )


# --- Профиль (§2.6) ------------------------------------------------------


def profile(
    *,
    tariff_id: TariffId,
    messages_used: int,
    messages_limit: int,
    images_left: int,
    friends: int,
) -> Screen:
    """Четыре реальных числа и два выхода."""
    return Screen(
        text=(
            f"Твой тариф: {TARIFF_TITLES[tariff_id]}\n"
            f"Сообщений сегодня: {messages_used} из {messages_limit}\n"
            f"Картинок: {images_left}\n"
            f"Друзей позвал: {friends}"
        ),
        buttons=(MENU_TARIFFS, BUTTON_MY_LINK),
    )


# --- Рефералка (§2.7) ----------------------------------------------------


def referral_invite(referral_url: str) -> Screen:
    """Готовое сообщение для пересылки, а не голая ссылка.

    Пользователю остаётся одно действие — «Переслать». Если отдать только
    ссылку, ему придётся придумывать, что к ней написать, и большинство
    просто не станет.
    """
    return Screen(
        text=(
            "Тут бесплатный ChatGPT и картинки, "
            f"без VPN и регистрации 👉 {referral_url}"
        ),
        next_step="готовое сообщение, остаётся переслать",
    )


def referral_reward() -> Screen:
    return Screen(text=REFERRAL_REWARD, buttons=_menu_buttons())


def friends_invited(count: int) -> str:
    """Строка про приглашённых друзей — для профиля."""
    return _friends(count)


# --- Тарифы (§2.8) -------------------------------------------------------

PAYMENTS_SOON = "Оплата скоро заработает 🙏 А пока лимиты можно поднять бесплатно:"


def tariff_card(tariff_id: TariffId) -> Screen:
    """Одна карточка тарифа. Три карточки, без сравнительной таблицы."""
    tariff = TARIFFS[tariff_id]
    title = f"{TARIFF_TITLES[tariff_id]} — {_rubles(tariff.price_rub)} ₽/мес"
    if tariff_id is TariffId.PRO:
        title = f"{title} {POPULAR_MARK}"
    return Screen(
        text=f"{title}\n{TARIFF_FEATURES[tariff_id]}",
        buttons=(choose_button(tariff_id),),
    )


def choose_button(tariff_id: TariffId) -> str:
    return f"Выбрать {TARIFF_TITLES[tariff_id]}"


def payments_soon() -> Screen:
    """Заглушка до фазы 8. Тупика не создаёт: выход с экрана есть."""
    return Screen(
        text=PAYMENTS_SOON,
        buttons=(BUTTON_MY_LINK, MENU_PROFILE),
    )


# --- Временный ответ фазы 1 ----------------------------------------------

#: Сквозная проверка контура «вебхук → очередь → воркер → ответ». Уходит на
#: фазе 4, когда бота начнут вести настоящие сценарии. Держим здесь, а не в
#: обработчике, чтобы правило «все тексты в texts.py» не знало исключений и
#: чтобы линтер проверял даже временный текст.
PONG = "понг"


def pong() -> Screen:
    return Screen(text=PONG, buttons=_menu_buttons())


# --- Перегрузка (§3.4.2) -------------------------------------------------

TOO_BUSY = "Сейчас много запросов, попробуй через минуту 🙏"


def too_busy() -> Screen:
    """Честный отказ при переполнении очереди. Лимит при этом не списывается."""
    return Screen(text=TOO_BUSY, buttons=(BUTTON_RETRY,))


# --- Реестр для линтера --------------------------------------------------


def _all_screens() -> tuple[Screen, ...]:
    """Каждый экран, отрисованный представительными значениями.

    Реестр нужен линтеру, но заодно он служит проверкой, что каждый экран
    вообще собирается: опечатка в шаблоне падает здесь, а не у пользователя.
    """
    return (
        onboarding(daily_messages=20, daily_images=3),
        onboarding(daily_messages=20, daily_images=5, from_presentations=True),
        onboarding(daily_messages=20, daily_images=3, referral_gift=True),
        chat_answer("Ответ на вопрос.", offer_new_dialog=False),
        chat_answer("Ответ на вопрос.", offer_new_dialog=True),
        chat_error(),
        new_dialog_started(),
        image_ask(),
        image_drawing(),
        image_error(),
        image_result(),
        share_caption("mybot", "https://t.me/mybot?start=ref_abc123"),
        presets_menu(("🧱 Лего", "🏚 Плохой день")),
        preset_ask_photo("Кинь фото — сделаю из тебя лего"),
        preset_working(),
        preset_error(),
        photo_rejected(PHOTO_TOO_BIG),
        photo_rejected(PHOTO_NOT_AN_IMAGE),
        preset_result(),
        paywall_images(),
        paywall_messages(),
        profile(
            tariff_id=TariffId.FREE,
            messages_used=12,
            messages_limit=20,
            images_left=2,
            friends=3,
        ),
        referral_invite("https://t.me/mybot?start=ref_abc123"),
        referral_reward(),
        payments_soon(),
        too_busy(),
        pong(),
        *(tariff_card(tariff_id) for tariff_id in PAID_TARIFFS),
    )


#: Все экраны бота. Линтер проверяет именно этот список.
SCREENS: tuple[Screen, ...] = _all_screens()
