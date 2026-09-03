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
from datetime import date

from app.core.models import TariffId
from app.core.tariffs import PAID_TARIFFS, RUB, STARS, TARIFFS


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
    #: Сколько строк допустимо. По умолчанию пять (§2.9): простыни в
    #: мессенджере не читают. Поднимать это значение можно только там, где
    #: сравнение и есть смысл экрана, — и каждое такое место видно в тестах.
    max_lines: int = 5
    #: Можно ли обращаться на «вы». По умолчанию нельзя (§2.9).
    #:
    #: Единственное исключение — строка согласия перед оплатой. Там человек
    #: становится стороной договора, и слова «нажимая кнопку, ты соглашаешься
    #: с офертой» звучали бы как приятельская просьба, а не как согласие с
    #: условиями. Послабление касается только текста: подписи кнопок
    #: остаются на «ты» в любом случае.
    formal_address: bool = False

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


def _days(count: int) -> str:
    return f"{count} {plural(count, 'день', 'дня', 'дней')}"


#: Месяцы в родительном падеже: «до 30 сентября», а не «до 30 сентябрь».
_MONTHS = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


def format_date(value: date) -> str:
    """Дата по-человечески: «30 сентября»."""
    return f"{value.day} {_MONTHS[value.month - 1]}"


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

#: Что обещает каждый платный тариф (§2.8).
#:
#: Списком, а не строкой через точки: в одну строку человек не читает
#: перечисление, он его просматривает. И накопительно — старший тариф
#: перечисляет всё, что умеет младший. В §2.8 голосовой ввод назван только у
#: Лайта, из-за чего Про выглядел так, будто ввод в нём пропадает.
TARIFF_FEATURES: dict[TariffId, tuple[str, ...]] = {
    TariffId.LITE: (
        "100 сообщений в день",
        "40 картинок",
        "голосовой ввод",
    ),
    TariffId.PRO: (
        "100 сообщений в день",
        "60 картинок",
        "голосовой ввод",
        "отвечает умнее",
    ),
    TariffId.MAX: (
        "200 сообщений в день",
        "150 картинок",
        "голосовой ввод",
        "отвечает умнее",
        "2 видео",
    ),
}

#: Отметка самого ходового тарифа. Без звезды: в Telegram звезда — это
#: валюта, и «⭐ популярный» читается как «купить за звёзды».
POPULAR_MARK = "берут чаще всего"


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


#: Провайдер отказался рисовать по правилам содержания. Кнопки «Повторить»
#: здесь нет намеренно: сколько ни повторяй, ответ будет тот же — а кнопка
#: обещала бы обратное.
IMAGE_REFUSED = "Такое я нарисовать не могу 🙅 Давай что-нибудь другое 👇"


def image_refused() -> Screen:
    return Screen(text=IMAGE_REFUSED, buttons=_menu_buttons())


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


PRESET_REFUSED = "С этим фото так не выйдет 🙅 Пришли другое или выбери прикол"


def preset_refused(preset_buttons: tuple[str, ...]) -> Screen:
    """Отказ по содержанию на фото. Выход с экрана — тот же список приколов."""
    return Screen(text=PRESET_REFUSED, buttons=preset_buttons)


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
    user_number: int | None = None,
) -> Screen:
    """Реальные числа и два выхода.

    ``user_number`` — номер для поддержки. Появляется не везде: в Telegram
    человека видно по @username, а в MAX username есть не у всех, и без
    номера опознать написавшего нечем.
    """
    lines = [
        f"Твой тариф: {TARIFF_TITLES[tariff_id]}",
        f"Сообщений сегодня: {messages_used} из {messages_limit}",
        f"Картинок: {images_left}",
        f"Друзей позвал: {friends}",
    ]
    if user_number is not None:
        lines.append(f"Твой номер: {user_number}")
    return Screen(text="\n".join(lines), buttons=(MENU_TARIFFS, BUTTON_MY_LINK))


# --- Рефералка (§2.7) ----------------------------------------------------


def referral_offer(*, bonus_messages: int, bonus_images: int) -> Screen:
    """Что человек получит за друга — до того, как он что-то отправит.

    Голая ссылка сама по себе не объясняет, зачем её пересылать. Сначала
    выгода, потом кнопка: одно действие, и обоим понятно, за что.
    """
    return Screen(
        text=(
            f"Позови друга — тебе +{_messages(bonus_messages)} "
            f"и +{_images(bonus_images)}.\n"
            "Другу столько же в подарок 🎁"
        ),
        buttons=(BUTTON_SEND_TO_FRIEND,),
    )


def referral_invite(referral_url: str) -> Screen:
    """Готовое сообщение для пересылки, а не голая ссылка.

    Пользователю остаётся одно действие — «Переслать». Если отдать только
    ссылку, ему придётся придумывать, что к ней написать, и большинство
    просто не станет.
    """
    return Screen(
        text=f"Тут бесплатный ChatGPT и картинки, без регистрации 👉 {referral_url}",
        next_step="готовое сообщение, остаётся переслать",
    )


def referral_reward() -> Screen:
    return Screen(text=REFERRAL_REWARD, buttons=_menu_buttons())


# --- Тарифы (§2.8) -------------------------------------------------------

PAYMENTS_SOON = "Оплата скоро заработает 🙏 А пока лимиты можно поднять бесплатно:"

BUTTON_PAY_CARD = "💳 Картой"
BUTTON_PAY_STARS = "⭐ Звёздами"
BUTTON_PAY_OPEN = "💳 Перейти к оплате"
BUTTON_OFFER = "📄 Оферта"
BUTTON_PRIVACY = "🔒 Данные"


def tariffs_screen() -> Screen:
    """Все три тарифа одним сообщением (§2.8).

    Одним, а не тремя: тремя сообщениями сравнить их нельзя — пока листаешь
    до третьего, первое уже за экраном. Возможности идут списком, по одной в
    строке: перечисление через точки глаз не читает, а пробегает.

    Отсюда и превышение обычного потолка в пять строк. Здесь сравнение и есть
    смысл экрана, поэтому потолок поднят явно и только для него.
    """
    blocks = [_tariff_block(tariff_id) for tariff_id in PAID_TARIFFS]
    return Screen(
        text="\n\n".join(blocks),
        buttons=tuple(choose_button(tariff_id) for tariff_id in PAID_TARIFFS),
        max_lines=24,
    )


def _tariff_block(tariff_id: TariffId) -> str:
    tariff = TARIFFS[tariff_id]
    title = f"{TARIFF_TITLES[tariff_id]} — {_rubles(tariff.price_rub)} ₽/мес"
    if tariff_id is TariffId.PRO:
        title = f"{title} · {POPULAR_MARK}"
    features = "\n".join(f"· {feature}" for feature in TARIFF_FEATURES[tariff_id])
    return f"{title}\n{features}"


def choose_button(tariff_id: TariffId) -> str:
    """Подпись кнопки выбора.

    Одно слово, а не «Выбрать Лайт»: три кнопки стоят в ряд, и с длинными
    подписями ряд обрезается до нечитаемого. Что это выбор тарифа, ясно из
    сообщения прямо над кнопками.
    """
    return TARIFF_TITLES[tariff_id]


def payments_soon() -> Screen:
    """Заглушка до фазы 8. Тупика не создаёт: выход с экрана есть."""
    return Screen(
        text=PAYMENTS_SOON,
        buttons=(BUTTON_MY_LINK, MENU_PROFILE),
    )


# --- Перегрузка (§3.4.2) -------------------------------------------------

TOO_BUSY = "Сейчас много запросов, попробуй через минуту 🙏"


def too_busy() -> Screen:
    """Честный отказ при переполнении очереди. Лимит при этом не списывается."""
    return Screen(text=TOO_BUSY, buttons=(BUTTON_RETRY,))


# --- Оплата и подписка (§2.8) --------------------------------------------


#: Строка про согласие. Стоит на экране оформления заказа и нигде больше:
#: именно здесь человек делает то, что превращает его в сторону договора.
#:
#: Единственное место во всём боте, где мы обращаемся на «вы». Так и должно
#: быть: это не разговор, а условия, под которыми человек ставит подпись.
CONSENT = "Нажимая кнопку оплаты, вы соглашаетесь с офертой и политикой данных."

#: Что человек увидит на кнопке отмены и в напоминаниях.
BUTTON_SUBSCRIPTION = "⚙️ Подписка"
BUTTON_SUBSCRIPTION_OFF = "Отключить продление"


def _price(amount: int, currency: str) -> str:
    """Сумма так, как её увидит человек: «599 ₽» или «524 ⭐»."""
    if currency == STARS:
        return f"{amount} ⭐"
    return f"{_rubles(amount)} ₽"


def payment_methods(tariff_id: TariffId, *, price_rub: int, stars: int) -> Screen:
    """Выбор способа оплаты.

    Показывается только когда способов правда два. Когда он один, выбирать
    нечего, и человек сразу попадает на экран заказа — туда, где условия.
    """
    return Screen(
        text=(
            f"Тариф «{TARIFF_TITLES[tariff_id]}» — {_rubles(price_rub)} ₽ в месяц.\n"
            f"Звёздами Telegram — {stars} ⭐, чуть дороже.\n"
            "Как удобнее платить?"
        ),
        buttons=(BUTTON_PAY_CARD, BUTTON_PAY_STARS),
    )


def payment_order(
    tariff_id: TariffId,
    *,
    days: int,
    amount: int,
    currency: str,
    next_charge: str,
    recurring: bool,
    statement: str = "",
) -> Screen:
    """Экран оформления заказа: всё, под чем человек подписывается.

    Здесь и только здесь стоят одновременно: название тарифа, сумма, валюта,
    периодичность, дата ближайшего списания, право отменить в любой момент и
    ссылки на документы (§4.4 и §4.11 оферты). Кнопка оплаты — на этом же
    экране: согласие даётся её нажатием, и разносить их по разным сообщениям
    значило бы брать согласие вслепую.

    ``recurring`` разделяет два разных договора. Подписка продлевается сама, и
    об этом надо сказать до денег. Разовая оплата не продлевается, и обещать
    продление было бы враньём — а именно оно случилось бы, оставь мы один
    текст на оба случая.

    ``statement`` — как платёж подпишется в банковской выписке. Строка нужна
    затем, что через месяц человек увидит в приложении банка незнакомое
    название и позвонит оспаривать списание. Показанная заранее, она этот
    звонок предотвращает. Пусто — значит выписки не будет вовсе: у звёзд
    списывает мессенджер, банк тут ни при чём.
    """
    if recurring:
        lines = [
            f"Подписка «{TARIFF_TITLES[tariff_id]}» — "
            f"{_price(amount, currency)} каждые {_days(days)}.",
            f"Следующее списание — {next_charge}.",
            "Отключить продление можно в профиле в любой момент.",
        ]
    else:
        lines = [
            f"Тариф «{TARIFF_TITLES[tariff_id]}» — "
            f"{_price(amount, currency)} на {_days(days)}.",
            "Продлевать надо будет вручную — сам ничего не спишется.",
        ]
    if statement:
        lines.append(f"В выписке банка: {statement}")
    lines.append(CONSENT)
    return Screen(
        text="\n".join(lines),
        buttons=(BUTTON_PAY_OPEN, BUTTON_OFFER, BUTTON_PRIVACY),
        formal_address=True,
    )


PAYMENT_FAILED = "Не получилось открыть оплату 🤷 Попробуй ещё раз или напиши нам."

#: Что видит человек, если мессенджер спросил про заказ, которого у нас нет.
#: Не экран бота, а поле ответа мессенджера, — но текст всё равно наш, и
#: правила §2.9 на него распространяются.
PAYMENT_REFUSED = "Счёт устарел. Открой тарифы и выбери ещё раз 🙏"


def payment_refused() -> Screen:
    return Screen(
        text=PAYMENT_REFUSED,
        next_step="человек возвращается к тарифам сам",
    )


def payment_failed() -> Screen:
    """Провайдер не ответил. Денег с человека при этом не взяли."""
    return Screen(text=PAYMENT_FAILED, buttons=_menu_buttons())


def payment_done(tariff_id: TariffId, *, until: str, renewing: bool) -> Screen:
    """Подтверждение после оплаты. Единственный экран, где важна точность.

    ``renewing`` решает вторую строку. Сказать «продлится сам» там, где
    продления не будет, — значит оставить человека без тарифа в тот день,
    когда он на него рассчитывал.
    """
    tail = (
        "Дальше продлевается сам — отключить можно в профиле 👇"
        if renewing
        else "Лимиты уже обновились — пиши 👇"
    )
    return Screen(
        text=f"Готово! Тариф «{TARIFF_TITLES[tariff_id]}» включён до {until}.\n{tail}",
        buttons=_menu_buttons(),
    )


def invoice(tariff_id: TariffId, *, days: int) -> tuple[str, str]:
    """Заголовок и описание счёта в мессенджере.

    Не Screen: это не экран бота, а поля счёта, которые рисует сам мессенджер.
    Но текст всё равно наш, поэтому живёт здесь.
    """
    features = TARIFF_FEATURES[tariff_id]
    return (
        f"Тариф {TARIFF_TITLES[tariff_id]}",
        f"{features[0]} · {features[1]}. Подписка на {_days(days)}.",
    )


# --- Управление подпиской (§4.14 оферты) ---------------------------------


def subscription_none() -> Screen:
    """Подписки нет. Экран всё равно нужен: кнопка в профиле ведёт сюда."""
    return Screen(
        text="Подписки пока нет 🙂 Платные тарифы — по кнопке 👇",
        buttons=(MENU_TARIFFS, MENU_PROFILE),
    )


def subscription_active(
    tariff_id: TariffId, *, days: int, amount: int, currency: str, next_charge: str
) -> Screen:
    """Действующая подписка: сколько, как часто и когда следующее списание."""
    return Screen(
        text=(
            f"Подписка «{TARIFF_TITLES[tariff_id]}» — "
            f"{_price(amount, currency)} каждые {_days(days)}.\n"
            f"Следующее списание — {next_charge}."
        ),
        buttons=(BUTTON_SUBSCRIPTION_OFF, MENU_PROFILE),
    )


def subscription_failing(tariff_id: TariffId, *, amount: int, currency: str) -> Screen:
    """Списание не прошло, но мы ещё пробуем (§4.16 оферты)."""
    return Screen(
        text=(
            f"Не вышло списать {_price(amount, currency)} "
            f"за тариф «{TARIFF_TITLES[tariff_id]}» 🤷\n"
            "Проверь карту — попробуем ещё раз в ближайшие дни."
        ),
        buttons=(BUTTON_SUBSCRIPTION_OFF, MENU_PROFILE),
    )


def subscription_stopped(tariff_id: TariffId, *, until: str) -> Screen:
    """Продление отключено, оплаченный срок дорабатывает (§4.15 оферты)."""
    return Screen(
        text=(
            f"Продление отключено. Тариф «{TARIFF_TITLES[tariff_id]}» "
            f"работает до {until}.\n"
            "Вернуть можно в любой момент 👇"
        ),
        buttons=(MENU_TARIFFS, MENU_PROFILE),
    )


def subscription_cancelled(tariff_id: TariffId, *, until: str) -> Screen:
    """Ответ сразу после отмены. Главное здесь — что оплаченное не пропало."""
    return Screen(
        text=(
            f"Готово, больше не спишем 👌 Тариф «{TARIFF_TITLES[tariff_id]}» "
            f"работает до {until}.\n"
            "Вернуть подписку можно в любой момент 👇"
        ),
        buttons=(MENU_TARIFFS, MENU_PROFILE),
    )


def subscription_cancel_failed() -> Screen:
    """Отключить продление не вышло.

    Отдельный текст, а не общий «что-то пошло не так»: здесь важно, что
    подписка осталась включённой. Умолчать об этом значило бы дать человеку
    уйти в уверенности, что с него больше не спишут.
    """
    return Screen(
        text="Не вышло отключить продление 🤷 Попробуй ещё раз или напиши нам.",
        buttons=(BUTTON_SUBSCRIPTION_OFF, MENU_PROFILE),
    )


def subscription_reminder(
    tariff_id: TariffId, *, amount: int, currency: str, on: str
) -> Screen:
    """Предупреждение за сутки до списания (§4.13 оферты).

    Не реклама и не просьба: обязанность. Человек должен успеть передумать до
    того, как деньги ушли, а не после.
    """
    return Screen(
        text=(
            f"Завтра, {on}, продлим тариф «{TARIFF_TITLES[tariff_id]}» — "
            f"{_price(amount, currency)}.\n"
            "Не нужно? Отключи продление 👇"
        ),
        buttons=(BUTTON_SUBSCRIPTION_OFF, MENU_PROFILE),
    )


def subscription_price_changed(
    tariff_id: TariffId, *, was: int, now: int, currency: str, on: str
) -> Screen:
    """Предупреждение о новой цене за неделю до списания (§4.17 оферты)."""
    return Screen(
        text=(
            f"Тариф «{TARIFF_TITLES[tariff_id]}» меняется в цене: было "
            f"{_price(was, currency)}, станет {_price(now, currency)}.\n"
            f"Спишем по-новому {on}. Не нужно? Отключи продление 👇"
        ),
        buttons=(BUTTON_SUBSCRIPTION_OFF, MENU_PROFILE),
    )


def subscription_renewed(
    tariff_id: TariffId, *, amount: int, currency: str, until: str
) -> Screen:
    """Списание прошло, тариф продлён."""
    return Screen(
        text=(
            f"Продлили тариф «{TARIFF_TITLES[tariff_id]}» — "
            f"{_price(amount, currency)}. Работает до {until}.\n"
            "Отключить продление — в профиле 👇"
        ),
        buttons=_menu_buttons(),
    )


def subscription_charge_failed(
    tariff_id: TariffId, *, amount: int, currency: str, until: str
) -> Screen:
    """Списание не прошло, но оплаченный срок ещё идёт (§4.16 оферты)."""
    return Screen(
        text=(
            f"Не вышло списать {_price(amount, currency)} "
            f"за тариф «{TARIFF_TITLES[tariff_id]}» 🤷\n"
            f"Проверь карту — попробуем ещё раз. Тариф работает до {until}."
        ),
        buttons=(MENU_TARIFFS, MENU_PROFILE),
    )


def subscription_ended(tariff_id: TariffId) -> Screen:
    """Три дня попыток кончились: человек вернулся на бесплатные лимиты."""
    return Screen(
        text=(
            f"Продлить тариф «{TARIFF_TITLES[tariff_id]}» не вышло — "
            "вернули бесплатные лимиты.\n"
            "Оформить снова можно в тарифах 👇"
        ),
        buttons=(BUTTON_OPEN_TARIFFS,),
    )


# --- Повтор и параллельная работа ----------------------------------------

NOTHING_TO_REPEAT = "Повторять пока нечего 🤷 Напиши что-нибудь или выбери в меню 👇"
STILL_WORKING = "Секунду, я ещё делаю прошлое 🙏"


def nothing_to_repeat() -> Screen:
    """Кнопка повтора пережила контекст: перезапуск, чистка, старое сообщение.

    Случай редкий, но тупика из него быть не должно — экран возвращает в меню.
    """
    return Screen(text=NOTHING_TO_REPEAT, buttons=_menu_buttons())


def still_working() -> Screen:
    """Второе нажатие, пока первое ещё в работе (§3.4.1, anti-flood).

    Отдельный текст, а не TOO_BUSY: «много запросов, попробуй через минуту»
    здесь соврал бы — запрос ровно один, и он уже выполняется.
    """
    return Screen(
        text=STILL_WORKING,
        next_step="ждём результат предыдущего запроса",
    )


# --- Непредвиденный сбой --------------------------------------------------

INTERNAL_ERROR = "Что-то пошло не так, попробуй ещё раз 🤷 Ничего не потратилось."


def internal_error() -> Screen:
    """Последний рубеж: сюда попадает всё, что мы не предусмотрели.

    Вторая фраза — не утешение, а правда: списание происходит в самом конце
    сценария, после доставки результата, поэтому упавший запрос не стоит
    пользователю ничего.
    """
    return Screen(text=INTERNAL_ERROR, buttons=_menu_buttons())


# --- Непонятое сообщение --------------------------------------------------

UNSUPPORTED_INPUT = "Я понимаю текст и фото 🙂 Напиши словами или пришли фото 👇"


def unsupported_input() -> Screen:
    """Стикер, голосовое, документ, опрос — всё, чего бот пока не умеет.

    Молчать в ответ нельзя: человек решит, что бот сломался, и уйдёт. Экран
    возвращает в меню, откуда доступно всё остальное.
    """
    return Screen(text=UNSUPPORTED_INPUT, buttons=_menu_buttons())


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
        image_refused(),
        image_result(),
        share_caption("mybot", "https://t.me/mybot?start=ref_abc123"),
        presets_menu(("🧱 Лего", "🏚 Плохой день")),
        preset_ask_photo("Кинь фото — сделаю из тебя лего"),
        preset_working(),
        preset_error(),
        preset_refused(("🧱 Лего", "🏚 Плохой день")),
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
        profile(
            tariff_id=TariffId.FREE,
            messages_used=12,
            messages_limit=20,
            images_left=2,
            friends=3,
            user_number=1234,
        ),
        referral_offer(bonus_messages=50, bonus_images=5),
        referral_invite("https://t.me/mybot?start=ref_abc123"),
        referral_reward(),
        tariffs_screen(),
        payment_methods(TariffId.PRO, price_rub=599, stars=524),
        payment_order(
            TariffId.PRO,
            days=30,
            amount=599,
            currency=RUB,
            next_charge="30 сентября",
            recurring=True,
            statement="YM*ChatAIBot",
        ),
        payment_order(
            TariffId.PRO,
            days=30,
            amount=599,
            currency=RUB,
            next_charge="30 сентября",
            recurring=False,
            statement="YM*ChatAIBot",
        ),
        payment_order(
            TariffId.PRO,
            days=30,
            amount=525,
            currency=STARS,
            next_charge="30 сентября",
            recurring=True,
        ),
        payment_failed(),
        payment_refused(),
        payment_done(TariffId.PRO, until="30 сентября", renewing=True),
        payment_done(TariffId.PRO, until="30 сентября", renewing=False),
        payments_soon(),
        subscription_none(),
        subscription_active(
            TariffId.PRO,
            days=30,
            amount=599,
            currency=RUB,
            next_charge="30 сентября",
        ),
        subscription_active(
            TariffId.PRO, days=30, amount=524, currency=STARS, next_charge="30 сентября"
        ),
        subscription_failing(TariffId.PRO, amount=599, currency=RUB),
        subscription_stopped(TariffId.PRO, until="30 сентября"),
        subscription_cancelled(TariffId.PRO, until="30 сентября"),
        subscription_cancel_failed(),
        subscription_reminder(TariffId.PRO, amount=599, currency=RUB, on="30 сентября"),
        subscription_price_changed(
            TariffId.PRO, was=599, now=699, currency=RUB, on="30 сентября"
        ),
        subscription_renewed(
            TariffId.PRO, amount=599, currency=RUB, until="30 октября"
        ),
        subscription_charge_failed(
            TariffId.PRO, amount=599, currency=RUB, until="30 сентября"
        ),
        subscription_ended(TariffId.PRO),
        too_busy(),
        nothing_to_repeat(),
        still_working(),
        unsupported_input(),
        internal_error(),
    )


#: Все экраны бота. Линтер проверяет именно этот список.
SCREENS: tuple[Screen, ...] = _all_screens()
