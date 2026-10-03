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
from app.core.tariffs import PAID_TARIFFS, RUB, STARS, TARIFFS, TRIAL


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


def _presentations(count: int) -> str:
    return f"{count} {plural(count, 'презентация', 'презентации', 'презентаций')}"


def _documents(count: int) -> str:
    return f"{count} {plural(count, 'доклад', 'доклада', 'докладов')}"


def _gifts(*parts: str) -> str:
    """«+20 сообщений, +2 картинки и +1 презентация» — через запятую и «и»."""
    marked = [f"+{part}" for part in parts]
    if len(marked) == 1:
        return marked[0]
    return f"{', '.join(marked[:-1])} и {marked[-1]}"


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
#: «Документы» человеку ничего не говорили: слово описывает, что бот берёт на
#: вход, а не что он отдаёт. Кнопка называется тем, за чем в неё приходят.
MENU_DOCUMENTS = "📄 Доклад / Реферат"
#: Есть в меню только тогда, когда задан ключ API презентаций (фаза 10, К2).
MENU_PRESENTATIONS = "📑 Презентации"

#: Подписи, которые эта кнопка носила раньше.
#:
#: Постоянная клавиатура в Telegram возвращает нажатие ровно своей подписью, и
#: у человека на экране может лежать прошлая версия меню — та, что пришла с
#: последним сообщением до выкладки. Одно нажатие по ней иначе уехало бы в чат
#: обычным вопросом и стоило бы ему сообщения.
RETIRED_MENU_DOCUMENTS = ("📄 Документы",)

#: Приколы были пунктом меню до фазы 10. Теперь вход в них — кнопкой под
#: экраном «Картинки», а подпись осталась прежней: старое меню на экране у
#: человека должно приводить туда же.
RETIRED_MENU_PRESETS = (MENU_PRESETS,)

#: Кнопка, открывающая само меню. Видна только там, где постоянного меню не
#: бывает (MAX): вешать под каждым сообщением все пять пунктов — значит
#: закрывать ими переписку, а человек смотрит на присланный файл, а не на меню.
BUTTON_SHOW_MENU = "☰ В меню"

MENU_ASK = "Что делаем?"

#: Сопровождает новое постоянное меню, когда первый ответ после выкладки нёс
#: свои кнопки и меню с ним не поместилось. Раз на версию меню.
MENU_UPDATED = "Обновил меню — новые кнопки внизу 👇"

#: Кнопки, доступные с любого экрана. В Telegram это постоянная клавиатура, в
#: MAX постоянных клавиатур не бывает, и меню открывается кнопкой «В меню»
#: (docs/research.md §1.6). Ядро про разницу не знает.
MENU: tuple[tuple[str, ...], ...] = (
    (MENU_IMAGES, MENU_DOCUMENTS),
    (MENU_PROFILE, MENU_TARIFFS),
)

# --- Прочие подписи кнопок ----------------------------------------------

BUTTON_RETRY = "Повторить"
BUTTON_NEW_DIALOG = "🔄 Новый диалог"
BUTTON_CONTINUE = "▶️ Продолжить"
BUTTON_DRAW_AGAIN = "🔄 Ещё раз"
BUTTON_SHARE = "📤 Поделиться"
BUTTON_SEND_TO_FRIEND = "📤 Отправить другу"
BUTTON_ANOTHER_PRESET = "🎭 Другой прикол"
BUTTON_CANCEL = "✖️ Отмена"
BUTTON_OPEN_TARIFFS = "⭐ Открыть тарифы"
BUTTON_MY_LINK = "🎁 Моя ссылка"
BUTTON_OPEN_CHANNEL = "📣 Открыть канал"
BUTTON_CHANNEL_CHECK = "✅ Я подписался"


def button_invite_for_images(bonus: int) -> str:
    """Подпись зовущей кнопки на экране, где кончились картинки.

    Число в подписи, а не в тексте рядом: человек читает кнопку последней и
    решает по ней. И приходит оно из настроек, а не зашито здесь, — иначе
    награду поменяли бы в одном месте, а обещали бы по-старому в другом.
    """
    return f"🎁 Позвать друга → +{_images(bonus)} сразу"


def button_invite_for_messages(bonus: int) -> str:
    """То же, когда кончились сообщения."""
    return f"🎁 Позвать друга → +{_messages(bonus)} сразу"


def button_invite_for_presentations(bonus: int) -> str:
    """То же, когда кончились презентации."""
    return f"🎁 Позвать друга → +{_presentations(bonus)}"


def button_channel_bonus(bonus: int) -> str:
    """Кнопка «получить картинки за подписку на канал»."""
    return f"📣 Канал → +{_images(bonus)}"


# --- Названия тарифов ----------------------------------------------------

TARIFF_TITLES: dict[TariffId, str] = {
    TariffId.FREE: "Бесплатный",
    TariffId.LITE: "Лайт",
    TariffId.PRO: "Про",
    TariffId.MAX: "Макс",
}

#: Значок в заголовке карточки тарифа (фаза 11). Только на экране тарифов:
#: в кнопках и в остальных текстах тариф называется словом.
TARIFF_ICONS: dict[TariffId, str] = {
    TariffId.LITE: "⚡️",
    TariffId.PRO: "🚀",
    TariffId.MAX: "💥",
}


def tariff_features(
    tariff_id: TariffId, *, with_presentations: bool
) -> tuple[str, ...]:
    """Что обещает платный тариф — строками карточки (фаза 11, Т6).

    Числа берутся из реестра тарифов, а не пишутся здесь второй раз: иначе
    норму поменяли бы в одном месте, а продавали бы по-старому в другом.

    Каждая строка — то, что в боте правда есть. Голосового ввода и видео в
    нём нет, и с реальной оплатой обещать их значило бы продавать
    несуществующее. По той же причине строки о презентациях нет, когда
    раздела презентаций нет (не задан ключ API). Порядок у всех тарифов один:
    старший отличается от младшего только числами.
    """
    tariff = TARIFFS[tariff_id]
    features = [
        f"{_messages(tariff.daily_messages)} в день",
        f"{_images(tariff.monthly_images)} в месяц",
    ]
    if with_presentations:
        features.append(f"{_presentations(tariff.monthly_presentations)} в месяц")
    features.append(f"{_documents(tariff.monthly_documents)} в месяц")
    return tuple(features)


#: Отметка самого ходового тарифа. Без звезды: в Telegram звезда — это
#: валюта, и «⭐ популярный» читается как «купить за звёзды».
POPULAR_MARK = "берут чаще всего"


# --- Онбординг (§2.1) ----------------------------------------------------

_GREETING = "Привет! Я отвечу на любой вопрос, решу задачу и сделаю картинку."
_GREETING_FROM_PRESENTATIONS = (
    "Привет! Ты из бота презентаций — здесь ещё чат и картинки. "
    "Держи бонусные картинки за переход."
)
_INVITATION = "Просто напиши мне что-нибудь 👇"


def onboarding(*, from_presentations: bool = False) -> Screen:
    """Первый экран. Две строки — поздороваться и позвать написать.

    Числа лимитов отсюда убраны по решению заказчика. Первый экран должен
    звать попробовать, а не отчитываться: человек ещё ничего не сделал, а ему
    уже называют, сколько ему можно. Свои остатки он в любой момент видит в
    профиле, и там они всегда свежие.

    Строки о подарке от друга здесь больше нет (фаза 10): награду получает
    только пригласивший, и обещать приглашённому то, чего он не получит,
    нельзя.
    """
    greeting = _GREETING_FROM_PRESENTATIONS if from_presentations else _GREETING
    return Screen(text=f"{greeting}\n{_INVITATION}", buttons=_menu_buttons())


def _menu_buttons() -> tuple[str, ...]:
    return tuple(button for row in MENU for button in row)


# --- Чат (§2.2) ----------------------------------------------------------

#: Показывается вместо ответа, если провайдер не справился.
#: Вторая половина фразы — обещание, которое обязано быть правдой: лимит
#: списывается только по факту доставленного ответа.
CHAT_ERROR = "Что-то пошло не так, попробуй ещё раз 🤷 Сообщение не потратилось."


def chat_error() -> Screen:
    return Screen(text=CHAT_ERROR, buttons=(BUTTON_RETRY,))


def chat_answer(
    answer: str, *, offer_new_dialog: bool, truncated: bool = False
) -> Screen:
    """Ответ бота.

    Кнопка «Новый диалог» появляется начиная с десятого сообщения (§2.2):
    раньше она только мешает, а к десятому разговор обычно уже ушёл в сторону.

    «Продолжить» — только у оборванного ответа. Модель, упёршаяся в потолок
    длины, замолкает на полуслове, и без кнопки человек читает огрызок как
    законченную мысль.
    """
    buttons = (
        *((BUTTON_CONTINUE,) if truncated else ()),
        *((BUTTON_NEW_DIALOG,) if offer_new_dialog else ()),
    )
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
    return Screen(
        text=IMAGE_ASK,
        buttons=(MENU_PRESETS,),
        next_step="ждём описание от пользователя",
    )


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


# --- Документы -----------------------------------------------------------

DOCUMENTS_ASK = "Выбери, что сделать с файлом:"

#: Что бот умеет прочитать. Перечислены расширениями, а не словами «Word» и
#: «презентация»: человек выбирает файл в списке, где видит именно их.
DOCUMENTS_FORMATS = "Понимаю docx, pdf и pptx — до 20 МБ"

DOCUMENT_WORKING = "🔄 Читаю файл…"
DOCUMENT_READY = "Готово! Файлы выше — Word и PDF"

#: Документ упёрся в потолок длины и оборван на полуслове. Сказать об этом
#: обязательно: иначе человек отдаст обрубок как готовую работу.
DOCUMENT_READY_CUT = (
    "Готово, но текст вышел длинным и оборвался в конце.\nФайлы выше — Word и PDF"
)
DOCUMENT_ERROR = "Что-то пошло не так, попробуй ещё раз 🤷 Доклад не потратился."

#: Файл не открылся. Про пароль сказано отдельно: это самая частая причина, и
#: человек её может исправить сам, а «не читается» звучит как приговор.
DOCUMENT_UNREADABLE = (
    "Файл не открылся 🤷 Бывает с повреждёнными и с теми, что под паролем"
)

#: В файле нет текстового слоя. Почти всегда это скан, и сказать надо именно
#: про него: иначе человек пришлёт тот же файл ещё раз.
DOCUMENT_EMPTY = (
    "В файле нет текста — похоже, это скан. Распознавать картинки я пока не умею"
)

DOCUMENT_UNSUPPORTED = "Такой файл я не прочитаю. Пришли docx, pdf или pptx 🙏"

#: Тема в одно слово даёт сочинение ни о чём, а заплатит за него человек
#: полным разбором. Просим написать подробнее до всякого обращения.
DOCUMENT_TOPIC_TOO_SHORT = "Напиши тему подробнее — одного слова мало 🙏"
DOCUMENT_TOO_BIG = "Файл слишком большой, пришли до 20 МБ 🙏"


def menu_updated() -> Screen:
    return Screen(text=MENU_UPDATED, buttons=_menu_buttons())


def menu(menu_buttons: tuple[str, ...]) -> Screen:
    """Само меню отдельным экраном — для мессенджера без постоянных кнопок."""
    return Screen(text=MENU_ASK, buttons=menu_buttons)


def documents_menu(action_buttons: tuple[str, ...]) -> Screen:
    return Screen(text=f"{DOCUMENTS_ASK}\n{DOCUMENTS_FORMATS}", buttons=action_buttons)


def document_ask_file(invitation: str) -> Screen:
    """Приглашение прислать файл. Текст берётся из реестра действий."""
    return Screen(text=invitation, buttons=(BUTTON_CANCEL,))


def document_working() -> Screen:
    return Screen(
        text=DOCUMENT_WORKING,
        next_step="живёт до минуты и заменяется готовыми файлами",
    )


#: Чем становится «Читаю файл…», когда файлы отправлены: оно стоит над ними.
DOCUMENT_FILES = "Вот файлы 👇"


def document_files() -> Screen:
    return Screen(text=DOCUMENT_FILES, next_step="файлы и итог — ниже")


def document_ready(*, with_presentation: bool, truncated: bool = False) -> Screen:
    """Итог под файлами доклада (сессия 7).

    Приходит после файлов, а не над ними: «файлы выше» должно указывать на
    файлы. Кнопок две — презентация по этому докладу и меню; без ключа API
    презентаций остаётся одно меню. Кнопок действий («Доклад», «Реферат»…)
    здесь больше нет: их место — в меню раздела.
    """
    buttons: tuple[str, ...] = (BUTTON_SHOW_MENU,)
    if with_presentation:
        buttons = (BUTTON_PRESENTATION_FROM_REPORT, BUTTON_SHOW_MENU)
    return Screen(
        text=DOCUMENT_READY_CUT if truncated else DOCUMENT_READY,
        buttons=buttons,
    )


def document_error() -> Screen:
    return Screen(text=DOCUMENT_ERROR, buttons=(BUTTON_RETRY,))


def document_rejected(reason: str, action_buttons: tuple[str, ...]) -> Screen:
    """Файл не подошёл. Причина приходит готовой строкой из этого же файла.

    Кнопки действий остаются: человек уже выбрал, что хотел сделать, и
    выкидывать его в начало из-за неподходящего файла значит заставить
    выбирать заново.
    """
    return Screen(text=reason, buttons=action_buttons)


# --- Презентации (фаза 10) -----------------------------------------------

PRESENTATION_ASK = "О чём презентация? Напиши тему, например: Как работает фотосинтез"

#: Границы — те же, что у провайдера (docs/API.md §3.1). Отсекаем до
#: обращения: ответ провайдера на неверную тему человеку мы всё равно не
#: показываем, а свой отказ быстрее и понятнее.
PRESENTATION_TOPIC_BAD = "Тема нужна от 3 до 200 знаков. Напиши её ещё раз 🙏"

PRESENTATION_PICK_THEME = "Выбери оформление 👇"

#: Под вопросом о теме. Дословно из поручения.
BUTTON_SUGGEST_TOPIC = "Придумай сам"

#: Дословно из поручения заказчика. Многоточия нет намеренно: обещание
#: «около минуты» и так говорит, что ждать.
PRESENTATION_WORKING = "Готовлю презентацию, около минуты"

#: Во что превращается «Готовлю презентацию…», когда файлы ушли. Сообщение
#: стоит над файлами, и висеть там с «готовлю» ему нельзя — это неправда.
PRESENTATION_DONE = "Готово 👇"

#: Итог после файлов. Дословно от заказчика, вместе с пробелами вокруг
#: дефисов и без пробела перед эмодзи.
PRESENTATION_RESULT = (
    "С заботой о тебе отправляем 2 файла:\n"
    "1. PDF - можно сразу использовать🤝🏻\n"
    "2. PowerPoint - если нужно отредактировать✍🏻"
)

#: То же, когда PDF не собрался и ушёл один PowerPoint. Текст не обещает ни
#: двух файлов, ни PDF: человек искал бы второй файл, которого нет.
PRESENTATION_RESULT_PPTX_ONLY = (
    "С заботой о тебе отправляем файл:\n"
    "PowerPoint - можно сразу открыть и отредактировать✍🏻\n"
    "PDF в этот раз не собрался 🤷"
)

#: Вторая половина фразы — обещание, которое обязано быть правдой: презентация
#: списывается только после доставки файла.
PRESENTATION_ERROR = (
    "Не получилось собрать презентацию 🤷 Попробуй ещё раз — она не потратилась."
)

PRESENTATION_BUSY = (
    "Сейчас много запросов, попробуй позже 🙏 Презентация не потратилась."
)

#: Второе нажатие, пока первая сборка идёт. Вторую колоду мы не начинаем.
PRESENTATION_IN_PROGRESS = "Презентация уже готовится — дождись её 🙏"

BUTTON_PRESENTATION_AGAIN = "Ещё одну презентацию"
BUTTON_REPORT_FROM_PRESENTATION = "📑 Сделать доклад по презентации"
BUTTON_PRESENTATION_FROM_REPORT = "📑 Сделать презентацию по докладу"

#: Кнопку-связку уже нажимали: доклад или презентация по ней уже сделаны или
#: делаются. Второго результата не будет, а выход — кнопкой раздела.
LINK_ALREADY_USED = (
    "По этой кнопке уже сделано — результат в чате выше 👆\n"
    "Нужен ещё один — начни заново 👇"
)

#: Данных под кнопкой больше нет: прошло шесть часов или бот перезапускался.
#: Говорим как есть и ведём туда, где то же самое делается с начала.
LINK_EXPIRED_REPORT = (
    "Эта кнопка устарела — тему презентации я уже не помню 🤷\n"
    "Сделай доклад по теме заново 👇"
)
LINK_EXPIRED_PRESENTATION = (
    "Эта кнопка устарела — текст доклада я не храню 🤷\nСделай презентацию по теме 👇"
)

#: Имя файла, если из темы ничего пригодного для имени не осталось.
PRESENTATION_FILENAME = "Презентация"


def presentation_ask() -> Screen:
    return Screen(text=PRESENTATION_ASK, buttons=(BUTTON_SUGGEST_TOPIC, BUTTON_CANCEL))


def presentation_topic_bad() -> Screen:
    return Screen(text=PRESENTATION_TOPIC_BAD, next_step="ждём тему ещё раз")


def presentation_suggested(topic: str, theme_buttons: tuple[str, ...]) -> Screen:
    """Тема из «Придумай сам» — и сразу выбор оформления, одним сообщением.

    Тему показываем обязательно: человек должен видеть, о чём будет его
    презентация, до того как она соберётся и спишется.
    """
    return Screen(
        text=f"Тема: {topic}\n{PRESENTATION_PICK_THEME}",
        buttons=(*theme_buttons, BUTTON_CANCEL),
    )


def presentation_pick_theme(theme_buttons: tuple[str, ...]) -> Screen:
    return Screen(text=PRESENTATION_PICK_THEME, buttons=(*theme_buttons, BUTTON_CANCEL))


def presentation_working() -> Screen:
    return Screen(
        text=PRESENTATION_WORKING,
        next_step="живёт до пяти минут и заменяется готовыми файлами",
    )


def presentation_done() -> Screen:
    return Screen(text=PRESENTATION_DONE, next_step="файлы и итог — ниже")


def presentation_result(*, with_pdf: bool = True) -> Screen:
    return Screen(
        text=PRESENTATION_RESULT if with_pdf else PRESENTATION_RESULT_PPTX_ONLY,
        buttons=(BUTTON_REPORT_FROM_PRESENTATION, BUTTON_PRESENTATION_AGAIN),
    )


def link_already_used(exit_button: str) -> Screen:
    return Screen(text=LINK_ALREADY_USED, buttons=(exit_button,))


def link_expired_report() -> Screen:
    return Screen(text=LINK_EXPIRED_REPORT, buttons=(MENU_DOCUMENTS,))


def link_expired_presentation() -> Screen:
    return Screen(text=LINK_EXPIRED_PRESENTATION, buttons=(MENU_PRESENTATIONS,))


def presentation_error() -> Screen:
    return Screen(text=PRESENTATION_ERROR, buttons=(BUTTON_RETRY,))


def presentation_busy() -> Screen:
    return Screen(text=PRESENTATION_BUSY, buttons=(BUTTON_RETRY,))


def presentation_in_progress() -> Screen:
    return Screen(
        text=PRESENTATION_IN_PROGRESS, next_step="файлы придут сообщением ниже"
    )


def paywall_presentations(
    invite_presentations: int, *, renews_on: str | None, by_charge: bool = False
) -> Screen:
    """Презентации кончились (фаза 11: они вошли в тарифы).

    Выходов два: тарифы и друг — за друга презентацию дают сразу. Когда
    придёт новая норма, сказано, только если она правда придёт: у
    бесплатного тарифа презентаций в месяц нет.
    """
    if renews_on is None:
        text = "Презентации закончились 😔\nЕщё будут с тарифом или за друга:"
    else:
        text = _monthly_paywall("Презентации", renews_on, by_charge=by_charge)
    return Screen(
        text=text,
        buttons=(
            BUTTON_OPEN_TARIFFS,
            button_invite_for_presentations(invite_presentations),
        ),
    )


def _monthly_paywall(what: str, renews_on: str, *, by_charge: bool) -> str:
    """Две строки пейволла месячной нормы: что кончилось и когда будет новое.

    «Новые придут с продлением» — там, где новая норма зависит от списания
    по подписке: не пройдёт оно — тариф кончится, а с ним и эта норма.
    «А можно не ждать» правдиво на любом тарифе: любая оплата начинает
    период заново, с полной нормой.
    """
    when = (
        f"Новые придут с продлением {renews_on}"
        if by_charge
        else f"Новые будут {renews_on}"
    )
    return f"{what} на этот месяц закончились 😔\n{when}, а можно не ждать:"


# --- Пресеты (§2.4) ------------------------------------------------------

PRESETS_ASK = "Выбери, что сделаем с фото:"

#: Чем помечен прикол, который откроется только на платном тарифе.
#:
#: В конце подписи, а не в начале: каждый прикол начинается со своей картинки,
#: и два значка подряд читаются как один непонятный. Замок и без того говорит
#: сам за себя — объяснять его отдельной строкой на экране незачем.
LOCK_MARK = " 🔒"


def locked_button(button: str) -> str:
    """Подпись прикола с замком — для того, у кого он ещё не открыт."""
    return f"{button}{LOCK_MARK}"


def presets_menu(preset_buttons: tuple[str, ...]) -> Screen:
    return Screen(text=PRESETS_ASK, buttons=preset_buttons)


def preset_ask_photo(invitation: str, *, cancellable: bool = False) -> Screen:
    """Приглашение прислать фото. Текст берётся из реестра пресетов.

    ``cancellable`` — просим не первое фото, а следующее. Кнопка отмены на
    этом шаге обязательна: человек уже что-то отдал боту, и уйти из режима
    молча означало бы гадать, засчитан присланный снимок или нет.
    """
    if cancellable:
        return Screen(text=invitation, buttons=(BUTTON_CANCEL,))
    return Screen(text=invitation, next_step="ждём фото от пользователя")


#: Прикол закрыт замком, а тариф бесплатный.
#:
#: Не отказ, а предложение: человек нажал ровно на то, за что мы просим
#: деньги, и это лучший момент показать ему тарифы. Второй выход обязателен —
#: покупать прямо сейчас он не обязан.
PRESET_LOCKED = "Этот прикол открывается на платном тарифе 🔒"


def preset_locked() -> Screen:
    return Screen(
        text=PRESET_LOCKED,
        buttons=(BUTTON_OPEN_TARIFFS, BUTTON_ANOTHER_PRESET),
    )


#: Первое фото у мессенджера уже не забрать.
#:
#: В MAX ссылка на снимок живёт не вечно, и между первым фото и вторым человек
#: может уйти надолго. Честнее сказать, что снимок потерялся, чем показать
#: ошибку обработки: обрабатывать было нечего.
PRESET_PHOTO_LOST = "Первое фото потерялось 🤷 Давай начнём заново"


def preset_photo_lost(preset_buttons: tuple[str, ...]) -> Screen:
    return Screen(text=PRESET_PHOTO_LOST, buttons=preset_buttons)


#: Эмодзи впереди не украшение: в Telegram он подменяется анимированным
#: премиальным аналогом (см. adapters/telegram/emoji.py), и человеку видно,
#: что бот работает, а не завис. В MAX остаётся обычный символ.
PRESET_WORKING = "🔄 Делаю… ~15 сек"
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


def paywall_documents(*, renews_on: str | None, by_charge: bool = False) -> Screen:
    """Доклады кончились.

    Ни канала, ни награды за друга здесь нет, и это не забывчивость: разовые
    подарки заведены под картинки и обещают картинки. Обещать за друга
    доклады значило бы сказать неправду на экране, который человек читает
    ровно в тот момент, когда решает, платить ли.

    ``renews_on`` — когда придёт новая норма. None — сама она не придёт:
    у бесплатного тарифа докладов в месяц нет, только разовые, и обещать
    «новые будут» там нельзя.
    """
    if renews_on is None:
        text = "Доклады закончились 😔\nЕщё будут с тарифом — выбери подходящий 👇"
    else:
        text = _monthly_paywall("Доклады", renews_on, by_charge=by_charge)
    return Screen(text=text, buttons=(BUTTON_OPEN_TARIFFS,))


def paywall_images(
    *,
    renews_on: str | None,
    invite_images: int,
    channel_images: int = 0,
    by_charge: bool = False,
) -> Screen:
    """Показывается только при исчерпании и всегда даёт выход.

    Первая строка говорит, когда придут новые картинки: у них месячная
    норма на любом тарифе, и «завтра будут ещё» было бы обманом — человек
    прождал бы сутки впустую. ``renews_on`` в None — новая норма сама не
    придёт; при нынешних тарифах такого не бывает (у бесплатного три
    картинки в месяц), но экран к этому готов.

    ``channel_images`` в нуле означает, что бонус за канал предлагать нечего:
    канал не настроен, человек его уже получил или пришёл из мессенджера, где
    канала у нас нет.
    """
    if renews_on is None:
        text = "Картинки закончились 😔\nМожно взять ещё бесплатно или открыть тарифы:"
    else:
        text = _monthly_paywall("Картинки", renews_on, by_charge=by_charge)
    buttons: tuple[str, ...] = (
        BUTTON_OPEN_TARIFFS,
        button_invite_for_images(invite_images),
    )
    if channel_images:
        buttons = (*buttons, button_channel_bonus(channel_images))
    return Screen(text=text, buttons=buttons)


def paywall_messages(*, invite_messages: int) -> Screen:
    """Тот же экран для сообщений.

    В §2.5 задания дан текст только про картинки, но кончиться могут и
    сообщения — 20 в день на бесплатном тарифе. Оставить этот случай без
    экрана значило бы получить тупик, а тупиков быть не должно.

    Здесь «завтра» безусловно: сообщения дневные на любом тарифе.
    """
    return Screen(
        text=(
            "Сообщения на сегодня закончились 😔\nЗавтра будут ещё, а можно не ждать:"
        ),
        buttons=(BUTTON_OPEN_TARIFFS, button_invite_for_messages(invite_messages)),
    )


# --- Профиль (§2.6) ------------------------------------------------------


def profile(
    *,
    tariff_id: TariffId,
    messages_used: int,
    messages_limit: int,
    images_left: int,
    documents_left: int,
    friends: int,
    presentations_left: int | None = None,
    user_number: int | None = None,
    period_ends: str | None = None,
    tariff_continues: bool = True,
) -> Screen:
    """Реальные числа и два выхода.

    Остаток — одно число: норма плюс подарки (Т8, решение заказчика).
    Человеку важно, сколько он ещё может сделать, а не из какой корзины это
    спишется.

    ``period_ends`` — день, когда кончается период месячной нормы. В первой
    строке он говорит то, что на этот день правда случится: у бесплатного —
    придут новые картинки, у платного с продлением — начнётся новый месяц,
    у платного без продления — кончится тариф.

    ``presentations_left`` — None, когда раздела презентаций нет (нет ключа API):
    тогда и в профиле о них ни слова.

    ``user_number`` — номер для поддержки. Появляется не везде: в Telegram
    человека видно по @username, а в MAX username есть не у всех, и без
    номера опознать написавшего нечем.
    """
    title = f"Твой тариф: {TARIFF_TITLES[tariff_id]}"
    if period_ends is not None:
        if tariff_id is TariffId.FREE:
            title = f"{title} · новые картинки {period_ends}"
        elif tariff_continues:
            title = f"{title} · новый месяц с {period_ends}"
        else:
            title = f"{title} · до {period_ends}"
    left = [
        f"{_button_name(MENU_IMAGES)}: {images_left}",
        f"{_button_name(MENU_DOCUMENTS)}: {documents_left}",
    ]
    if presentations_left is not None:
        left.append(f"{_button_name(MENU_PRESENTATIONS)}: {presentations_left}")
    lines = [
        title,
        f"Сообщений сегодня: {messages_used} из {messages_limit}",
        # Остатки — одной строкой: экран ограничен пятью (§2.9), а номер для
        # поддержки в MAX берёт пятую. Подписи — названия кнопок меню без
        # значка: человек ищет в профиле то же слово, что нажимал. Раньше
        # здесь стояли «разборы» — внутреннее имя раздела документов, и
        # заказчик его не узнал.
        " · ".join(left),
        f"Друзей позвал: {friends}",
    ]
    if user_number is not None:
        lines.append(f"Твой номер: {user_number}")
    return Screen(text="\n".join(lines), buttons=(MENU_TARIFFS, BUTTON_MY_LINK))


def _button_name(menu_label: str) -> str:
    """Название кнопки меню без ведущего значка: «🎨 Картинки» → «Картинки»."""
    return menu_label.split(" ", 1)[1]


# --- Рефералка (§2.7) ----------------------------------------------------


def referral_offer(
    *, bonus_messages: int, bonus_images: int, bonus_presentations: int = 0
) -> Screen:
    """Что человек получит за друга — до того, как он что-то отправит.

    Голая ссылка сама по себе не объясняет, зачем её пересылать. Сначала
    выгода, потом кнопка: одно действие, и понятно, за что.

    Другу здесь ничего не обещано, и это не забывчивость (фаза 10): награду
    получает только пригласивший. Вторая строка говорит, когда она придёт, —
    иначе человек ждал бы её сразу после пересылки.

    Презентация называется, только когда её правда дают: без ключа API
    ``bonus_presentations`` — ноль, и обещания нет (К2).
    """
    gifts = _gifts(
        _messages(bonus_messages),
        _images(bonus_images),
        *((_presentations(bonus_presentations),) if bonus_presentations else ()),
    )
    return Screen(
        text=(
            f"Позови друга — тебе {gifts} 🎁\nНачислю, как только друг запустит бота"
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


def referral_reward(*, messages: int, images: int, presentations: int = 0) -> Screen:
    """Пригласившему — сразу, как только друг нажал /start (§2.7)."""
    gifts = _gifts(
        _messages(messages),
        _images(images),
        *((_presentations(presentations),) if presentations else ()),
    )
    return Screen(
        text=f"🎁 Твой друг зашёл! Тебе {gifts}.",
        buttons=_menu_buttons(),
    )


# --- Бонус за подписку на канал ------------------------------------------


def channel_offer(*, bonus_images: int) -> Screen:
    """Предложение подписаться на канал за разовый бонус.

    Отдельным экраном, а не парой кнопок в пейволле: у ссылки на канал и у
    проверки подписки разное назначение, и человеку надо один раз объяснить,
    за что именно ему дадут картинки. Двух кнопок под текстом хватает —
    сначала уйти в канал, потом вернуться и нажать проверку.
    """
    return Screen(
        text=(
            f"Подпишись на канал — и получишь +{_images(bonus_images)} 🎁\n"
            "Там новые приколы с фото и всё, чему бот научился."
        ),
        buttons=(BUTTON_OPEN_CHANNEL, BUTTON_CHANNEL_CHECK),
    )


def channel_granted(*, bonus_images: int) -> Screen:
    """Подписка нашлась, картинки начислены."""
    return Screen(
        text=f"Спасибо! +{_images(bonus_images)} уже на балансе 🎁",
        buttons=_menu_buttons(),
    )


def channel_not_subscribed() -> Screen:
    """Подписки нет. Не упрёк, а подсказка, что делать дальше."""
    return Screen(
        text=(
            "Подписки пока не вижу 🤔\n"
            "Открой канал, подпишись и нажми проверку ещё раз."
        ),
        buttons=(BUTTON_OPEN_CHANNEL, BUTTON_CHANNEL_CHECK),
    )


def channel_already_taken(*, invite_images: int) -> Screen:
    """Бонус за канал разовый, и второй раз его не дают.

    Тупика тут быть не должно, поэтому экран сразу называет то, чем ещё можно
    добрать картинки.
    """
    return Screen(
        text="Бонус за канал ты уже получил 🎁 Картинки можно взять ещё так:",
        buttons=(BUTTON_OPEN_TARIFFS, button_invite_for_images(invite_images)),
    )


def channel_check_failed() -> Screen:
    """Проверить не удалось — и это не то же самое, что «не подписан».

    Отказать здесь молча значило бы не выдать заслуженный бонус и оставить
    человека думать, что его обманули.
    """
    return Screen(
        text="Не получилось проверить подписку 🤷 Попробуй ещё раз.",
        buttons=(BUTTON_CHANNEL_CHECK, BUTTON_OPEN_TARIFFS),
    )


# --- Тарифы (§2.8) -------------------------------------------------------

PAYMENTS_SOON = "Оплата скоро заработает 🙏 А пока лимиты можно поднять бесплатно:"

BUTTON_PAY_CARD = "💳 Картой"
BUTTON_PAY_STARS = "⭐ Звёздами"
BUTTON_PAY_OPEN = "💳 Перейти к оплате"
BUTTON_OFFER = "📄 Оферта"
BUTTON_PRIVACY = "🔒 Данные"
BUTTON_EMAIL_CHANGE = "✏️ Другая почта"


def tariffs_screen(*, with_presentations: bool) -> Screen:
    """Все три тарифа одним сообщением (§2.8).

    Одним, а не тремя: тремя сообщениями сравнить их нельзя — пока листаешь
    до третьего, первое уже за экраном. Возможности идут списком, по одной в
    строке: перечисление через точки глаз не читает, а пробегает.

    Отсюда и превышение обычного потолка в пять строк. Здесь сравнение и есть
    смысл экрана, поэтому потолок поднят явно и только для него.
    """
    blocks = [
        _tariff_block(tariff_id, with_presentations=with_presentations)
        for tariff_id in PAID_TARIFFS
    ]
    return Screen(
        text="\n\n".join(blocks),
        buttons=tuple(choose_button(tariff_id) for tariff_id in PAID_TARIFFS),
        max_lines=24,
    )


def _tariff_block(tariff_id: TariffId, *, with_presentations: bool) -> str:
    tariff = TARIFFS[tariff_id]
    title = (
        f"{TARIFF_ICONS[tariff_id]} {TARIFF_TITLES[tariff_id]} — "
        f"{_rubles(tariff.price_rub)} ₽/мес"
    )
    if tariff_id is TariffId.PRO:
        title = f"{title} · {POPULAR_MARK}"
    features = tariff_features(tariff_id, with_presentations=with_presentations)
    lines = "\n".join(f"· {feature}" for feature in features)
    return f"{title}\n{lines}"


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

#: Куда прислать чек. Спрашивается один раз, перед первой оплатой картой.
#:
#: Причина названа прямо, и это не вежливость: человека посреди покупки
#: просят личные данные, и «зачем» он спросит сам. Ответ «так велит закон»
#: короче любого объяснения и не вызывает подозрений.
EMAIL_ASK = "Куда прислать чек? Напиши почту — этого требует закон 📧"
EMAIL_BAD = "Не похоже на почту 🤔 Напиши ещё раз, например alika@mail.ru"


def email_ask() -> Screen:
    return Screen(text=EMAIL_ASK, next_step="ждём почту от пользователя")


def email_bad() -> Screen:
    return Screen(text=EMAIL_BAD, next_step="ждём почту ещё раз")


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
    receipt_to: str = "",
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

    ``receipt_to`` — почта, на которую уйдёт фискальный чек. Показывается
    здесь же затем, что человек назвал её парой сообщений раньше и мог
    ошибиться в букве: увидев адрес перед оплатой, он ошибку заметит, а
    получив пустоту вместо чека через месяц — уже нет.

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
    # Выписка и чек — одной строкой, а не двумя. Экран и без них упирается в
    # потолок в пять строк, а поднимать потолок здесь нельзя: каждая строка
    # выше — обязательное раскрытие условий, и убрать вместо этого одну из
    # них значило бы сэкономить место на том единственном, ради чего экран
    # существует. Обе части отвечают на один вопрос — что придёт человеку
    # после оплаты, — так что вместе они читаются не хуже.
    destinations = []
    if statement:
        destinations.append(f"В выписке банка: {statement}")
    if receipt_to:
        destinations.append(f"чек на {receipt_to}")
    if destinations:
        lines.append(" · ".join(destinations) if statement else f"Чек на {receipt_to}")
    lines.append(CONSENT)
    # Кнопка правки адреса появляется только там, где адрес есть. Ошибиться
    # в букве человек мог парой сообщений раньше, а заметить это — здесь;
    # без кнопки исправить ошибку он смог бы только через месяц, на
    # следующей оплате.
    buttons: tuple[str, ...] = (BUTTON_PAY_OPEN, BUTTON_OFFER, BUTTON_PRIVACY)
    if receipt_to:
        buttons = (*buttons, BUTTON_EMAIL_CHANGE)
    return Screen(
        text="\n".join(lines),
        buttons=buttons,
        formal_address=True,
    )


# --- Пробный период (фаза 11, часть 3) -----------------------------------

BUTTON_TRIAL = f"⚡️ Попробовать за {TRIAL.price_rub} ₽"


def _trial_terms() -> tuple[str, str, int]:
    """Название тарифа, срок и цена после пробного — из реестра, а не текстом."""
    full = TARIFFS[TRIAL.tariff].price_rub
    return TARIFF_TITLES[TRIAL.tariff], _days(TRIAL.days), full


def trial_offer() -> Screen:
    """Предложение пробного периода под карточками тарифов.

    Отдельным сообщением, а не строкой в карточках: карточки заказчик дал
    дословно, а предложение видят не все — только те, кто ещё ни разу не
    платил. Главное в нём — что продление не случится молча: напомним
    заранее, и отключить можно до денег.
    """
    title, days, full = _trial_terms()
    return Screen(
        text=(
            f"Можно начать с пробы: «{title}» на {days} за {TRIAL.price_rub} ₽, "
            f"дальше {_rubles(full)} ₽ в месяц.\n"
            "Напомним заранее — успеешь отключить, если не понравится 👇"
        ),
        buttons=(BUTTON_TRIAL,),
    )


def trial_order(*, first_charge: str, statement: str, receipt_to: str = "") -> Screen:
    """Условия пробного периода и кнопка оплаты — в одном сообщении (ПП2).

    Как и у обычного заказа, здесь всё, под чем человек подписывается:
    сколько сейчас и за что, сколько потом и как часто, когда первое полное
    списание, что о нём предупредят, как отключить и ссылки на документы.
    """
    title, days, full = _trial_terms()
    lines = [
        f"Пробный период «{title}» — {days} за {TRIAL.price_rub} ₽.",
        f"Дальше {_rubles(full)} ₽ каждые {_days(30)}, "
        f"первое списание — {first_charge}.",
        "Напомним за день до него. Отключить продление можно в профиле в любой момент.",
    ]
    destinations = []
    if statement:
        destinations.append(f"В выписке банка: {statement}")
    if receipt_to:
        destinations.append(f"чек на {receipt_to}")
    if destinations:
        lines.append(" · ".join(destinations) if statement else f"Чек на {receipt_to}")
    lines.append(CONSENT)
    buttons: tuple[str, ...] = (BUTTON_PAY_OPEN, BUTTON_OFFER, BUTTON_PRIVACY)
    if receipt_to:
        buttons = (*buttons, BUTTON_EMAIL_CHANGE)
    return Screen(text="\n".join(lines), buttons=buttons, formal_address=True)


def trial_started(*, until: str, amount: int) -> Screen:
    """Пробный период включён: до какого числа и что будет потом."""
    title, _, _ = _trial_terms()
    return Screen(
        text=(
            f"Готово! Пробный «{title}» включён до {until}.\n"
            f"Потом {_rubles(amount)} ₽ каждые {_days(30)} — напомним за день "
            "до списания. Отключить можно в профиле 👇"
        ),
        buttons=_menu_buttons(),
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
    # Без презентаций: заголовок счёта короткий, и в нём только первые две
    # строки карточки — сообщения и картинки, они есть всегда.
    features = tariff_features(tariff_id, with_presentations=False)
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


def subscription_other_method(*, by_stars: bool) -> Screen:
    """Подписка уже продлевается другим способом оплаты.

    Второй способ — это вторая подписка: звёздную продлевает сам Telegram, и
    наша оплата картой её не остановит, как и звёзды не остановят карту.
    Человек платил бы дважды за один и тот же срок.
    """
    way = "звёздами" if by_stars else "картой"
    return Screen(
        text=(
            f"Подписка уже продлевается {way} — второй способ означал бы "
            "платить дважды за один срок 🙂\n"
            "Чтобы сменить способ, отключи продление (оплаченное доработает) "
            "и оформи подписку заново 👇"
        ),
        buttons=(BUTTON_SUBSCRIPTION_OFF, MENU_PROFILE),
    )


def subscription_reminder(
    tariff_id: TariffId, *, amount: int, currency: str, on: str
) -> Screen:
    """Предупреждение за сутки до первого списания после пробного периода.

    Не реклама и не просьба: обязанность. Человек должен успеть передумать до
    того, как деньги ушли, а не после. Напоминание бывает только здесь —
    перед обычными продлениями его нет (решение заказчика).
    """
    return Screen(
        text=(
            f"Завтра, {on}, пробный период кончится — спишем "
            f"{_price(amount, currency)} за тариф «{TARIFF_TITLES[tariff_id]}».\n"
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
    tariff_id: TariffId, *, amount: int, currency: str, next_try: str
) -> Screen:
    """Списание не прошло; следующая попытка — тогда-то (§4.16 оферты).

    В нём сумма, дата следующей попытки и выход: человек успевает отключить
    продление до неё. Отдельного «завтра спишем» перед повтором нет.
    """
    return Screen(
        text=(
            f"Не вышло списать {_price(amount, currency)} "
            f"за тариф «{TARIFF_TITLES[tariff_id]}» 🤷\n"
            f"Проверь карту — попробуем ещё раз {next_try}. "
            "Не нужно? Отключи продление 👇"
        ),
        buttons=(BUTTON_SUBSCRIPTION_OFF, MENU_PROFILE),
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
        onboarding(),
        onboarding(from_presentations=True),
        chat_answer("Ответ на вопрос.", offer_new_dialog=False),
        chat_answer("Ответ на вопрос.", offer_new_dialog=True),
        chat_answer(
            "Ответ оборвался на полусло", offer_new_dialog=False, truncated=True
        ),
        chat_answer(
            "Ответ оборвался на полусло", offer_new_dialog=True, truncated=True
        ),
        chat_error(),
        new_dialog_started(),
        image_ask(),
        image_drawing(),
        image_error(),
        image_refused(),
        image_result(),
        share_caption("mybot", "https://t.me/mybot?start=ref_abc123"),
        presets_menu(("🧱 Лего", locked_button("🧸 Фигурка в коробке"))),
        preset_ask_photo("Кинь фото — сделаю из тебя лего"),
        preset_ask_photo("Отлично. Теперь кинь детское фото 👶", cancellable=True),
        preset_locked(),
        preset_photo_lost(("🧱 Лего", "🏚 Плохой день")),
        preset_working(),
        preset_error(),
        preset_refused(("🧱 Лего", "🏚 Плохой день")),
        photo_rejected(PHOTO_TOO_BIG),
        photo_rejected(PHOTO_NOT_AN_IMAGE),
        preset_result(),
        paywall_images(renews_on="27 сентября", invite_images=2),
        paywall_images(renews_on=None, invite_images=2),
        paywall_images(renews_on="27 сентября", invite_images=2, channel_images=2),
        paywall_images(renews_on="27 сентября", invite_images=2, by_charge=True),
        paywall_messages(invite_messages=50),
        channel_offer(bonus_images=2),
        channel_granted(bonus_images=2),
        channel_not_subscribed(),
        channel_already_taken(invite_images=2),
        channel_check_failed(),
        profile(
            tariff_id=TariffId.FREE,
            messages_used=12,
            messages_limit=20,
            images_left=5,
            documents_left=2,
            friends=3,
            period_ends="27 сентября",
        ),
        profile(
            tariff_id=TariffId.MAX,
            messages_used=12,
            messages_limit=200,
            images_left=160,
            documents_left=100,
            presentations_left=61,
            friends=3,
            user_number=1234,
            period_ends="27 сентября",
        ),
        profile(
            tariff_id=TariffId.PRO,
            messages_used=0,
            messages_limit=100,
            images_left=0,
            documents_left=0,
            presentations_left=0,
            friends=0,
            user_number=1234,
            period_ends="27 сентября",
            tariff_continues=False,
        ),
        referral_offer(bonus_messages=20, bonus_images=2),
        referral_offer(bonus_messages=20, bonus_images=2, bonus_presentations=1),
        referral_invite("https://t.me/mybot?start=ref_abc123"),
        referral_reward(messages=20, images=2),
        referral_reward(messages=20, images=2, presentations=1),
        trial_offer(),
        trial_order(first_charge="31 августа", statement="YM*ChatAIBot"),
        trial_order(
            first_charge="31 августа",
            statement="YM*ChatAIBot",
            receipt_to="alika@mail.ru",
        ),
        trial_started(until="31 августа", amount=299),
        tariffs_screen(with_presentations=True),
        tariffs_screen(with_presentations=False),
        payment_methods(TariffId.PRO, price_rub=599, stars=524),
        email_ask(),
        email_bad(),
        payment_order(
            TariffId.PRO,
            days=30,
            amount=599,
            currency=RUB,
            next_charge="30 сентября",
            recurring=True,
            statement="YM*ChatAIBot",
            receipt_to="alika@mail.ru",
        ),
        payment_order(
            TariffId.PRO,
            days=30,
            amount=599,
            currency=RUB,
            next_charge="30 сентября",
            recurring=True,
            receipt_to="alika@mail.ru",
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
        subscription_other_method(by_stars=True),
        subscription_other_method(by_stars=False),
        subscription_reminder(TariffId.PRO, amount=599, currency=RUB, on="30 сентября"),
        subscription_price_changed(
            TariffId.PRO, was=599, now=699, currency=RUB, on="30 сентября"
        ),
        subscription_renewed(
            TariffId.PRO, amount=599, currency=RUB, until="30 октября"
        ),
        subscription_charge_failed(
            TariffId.PRO, amount=599, currency=RUB, next_try="1 октября"
        ),
        subscription_ended(TariffId.PRO),
        too_busy(),
        nothing_to_repeat(),
        still_working(),
        unsupported_input(),
        internal_error(),
        menu((MENU_IMAGES, MENU_DOCUMENTS, MENU_PROFILE, MENU_TARIFFS)),
        menu_updated(),
        menu(
            (
                MENU_IMAGES,
                MENU_DOCUMENTS,
                MENU_PRESENTATIONS,
                MENU_PROFILE,
                MENU_TARIFFS,
            )
        ),
        presentation_ask(),
        presentation_topic_bad(),
        presentation_pick_theme(("Графит светлая", "Лазурь", "Свежая зелёная")),
        presentation_suggested(
            "Искусственный интеллект: польза и риски", ("Графит светлая", "Лазурь")
        ),
        presentation_working(),
        presentation_done(),
        presentation_result(),
        presentation_result(with_pdf=False),
        link_already_used(MENU_DOCUMENTS),
        link_already_used(MENU_PRESENTATIONS),
        link_expired_report(),
        link_expired_presentation(),
        presentation_error(),
        presentation_busy(),
        presentation_in_progress(),
        paywall_presentations(1, renews_on="27 сентября"),
        paywall_presentations(1, renews_on="27 сентября", by_charge=True),
        paywall_presentations(1, renews_on=None),
        paywall_documents(renews_on="27 сентября"),
        paywall_documents(renews_on="27 сентября", by_charge=True),
        paywall_documents(renews_on=None),
        documents_menu(("📊 Доклад", "📝 Реферат", "📌 Конспект")),
        document_ask_file("Кинь файл — сделаю по нему доклад"),
        document_working(),
        document_files(),
        document_ready(with_presentation=True),
        document_ready(with_presentation=False, truncated=True),
        document_error(),
        document_rejected(DOCUMENT_UNREADABLE, ("📊 Доклад",)),
        document_rejected(DOCUMENT_EMPTY, ("📊 Доклад",)),
        document_rejected(DOCUMENT_UNSUPPORTED, ("📊 Доклад",)),
        document_rejected(DOCUMENT_TOO_BIG, ("📊 Доклад",)),
        document_rejected(DOCUMENT_TOPIC_TOO_SHORT, ("📊 Доклад",)),
    )


#: Все экраны бота. Линтер проверяет именно этот список.
SCREENS: tuple[Screen, ...] = _all_screens()
