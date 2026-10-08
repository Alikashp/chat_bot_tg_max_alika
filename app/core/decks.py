"""Черновик презентации — то, что показывает экран параметров (сессия 7).

Экран приходит заполненным и собирает одной кнопкой; каждый параметр можно
поменять и вернуться. Всё, что на нём видно, лежит здесь, а сам черновик —
в памяти процесса под жетоном (порт Tokens): в нём тема, текст доклада или
присланный файл, а им в базе не место. Кнопки экрана несут только жетон.

Модуль чистый: проверка значений — по спискам из документации провайдера
(``app/ports/presentations.py``), никаких обращений наружу.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from app.core.models import Document
from app.ports.presentations import (
    AUDIENCES,
    DEFAULT_AUDIENCE,
    DEFAULT_LANGUAGE,
    DEFAULT_SLIDES,
    DEFAULT_THEME,
    LANGUAGES,
    MATERIAL_EXTENSIONS,
    MAX_SLIDES,
    MIN_SLIDES,
    DeckRequest,
    PresentationTheme,
)

#: Границы темы — те же, что у провайдера (docs/API.md §3.1).
MIN_TOPIC = 3
MAX_TOPIC = 200


@dataclass(frozen=True, slots=True)
class MaterialFile:
    """Присланный файл-материал — ссылкой, а не байтами (сессия 8, М1).

    ``ref`` — ссылка мессенджера, по которой адаптер скачает файл в момент
    сборки; ``filename`` — имя из сообщения: его видит человек на экране, и по
    его расширению API определяет тип. Содержимого здесь нет: между
    получением и сборкой бот файл не держит.
    """

    ref: str
    filename: str


@dataclass(frozen=True, slots=True)
class Draft:
    """Параметры будущей колоды."""

    topic: str
    language: str = DEFAULT_LANGUAGE
    slides: int = DEFAULT_SLIDES
    audience: str = DEFAULT_AUDIENCE
    #: Оформление. Пусто — ещё не выбрано: экран подставит то, что
    #: провайдер считает своим по умолчанию.
    theme_id: str = ""
    #: Текст доклада, по которому собирать. Пусто — доклада нет.
    material: str = ""
    #: Присланный файл-материал — ссылкой. Вместе с текстом не бывает: API
    #: принимает что-то одно.
    file: MaterialFile | None = None
    #: Жетон кнопки «Сделать презентацию по докладу», с которой пришёл
    #: черновик. Забирается, когда колода доставлена: кнопка под докладом
    #: тогда честно скажет «уже сделано».
    report_token: str | None = None

    def request(self, file: Document | None = None) -> DeckRequest:
        """Запрос на сборку ровно по тому, что показано на экране.

        ``file`` — файл-материал, скачанный по ссылке перед самой сборкой.
        """
        return DeckRequest(
            topic=self.topic,
            theme_id=self.theme_id,
            language=self.language,
            slides=self.slides,
            audience=self.audience,
            material=self.material if self.file is None else "",
            file=file,
        )


def with_theme(draft: Draft, themes: tuple[PresentationTheme, ...]) -> Draft:
    """Черновик с оформлением, которое правда есть у провайдера.

    Выбранное раньше могло пропасть из списка; тогда — оформление провайдера
    по умолчанию, а нет и его — первое из списка.
    """
    ids = [theme.id for theme in themes]
    if draft.theme_id in ids:
        return draft
    chosen = DEFAULT_THEME if DEFAULT_THEME in ids else ids[0]
    return replace(draft, theme_id=chosen)


def with_language(draft: Draft, value: str) -> Draft | None:
    """Новый язык; None — API такого не принимает."""
    return replace(draft, language=value) if value in LANGUAGES else None


def with_audience(draft: Draft, value: str) -> Draft | None:
    """Новая аудитория; None — API такой не знает."""
    return replace(draft, audience=value) if value in AUDIENCES else None


def with_slides(draft: Draft, value: str) -> Draft | None:
    """Новое число слайдов; None — не число или вне границ API."""
    if not value.isdigit():
        return None
    count = int(value)
    if not MIN_SLIDES <= count <= MAX_SLIDES:
        return None
    return replace(draft, slides=count)


def with_design(
    draft: Draft, value: str, themes: tuple[PresentationTheme, ...]
) -> Draft | None:
    """Новое оформление; None — его нет в списке провайдера."""
    if value not in {theme.id for theme in themes}:
        return None
    return replace(draft, theme_id=value)


#: Начало PDF и zip-архива (внутри zip — docx и pptx). Расширение называет
#: формат, сигнатура подтверждает: под «отчёт.pdf» может приехать что угодно.
_PDF = b"%PDF-"
_ZIP = b"PK\x03\x04"
_SIGNATURES: dict[str, bytes] = {".pdf": _PDF, ".docx": _ZIP, ".pptx": _ZIP}


def material_extension(filename: str) -> str | None:
    """Расширение файла-материала, если API его принимает; None — не примет.

    Проверяется до скачивания: API определяет тип по расширению имени
    (docs/API.md §3.1), и чужое расширение незачем даже тянуть.
    """
    _, dot, tail = filename.rpartition(".")
    extension = f".{tail.lower()}" if dot else ""
    return extension if extension in MATERIAL_EXTENSIONS else None


def material_ok(document: Document) -> bool:
    """Годится ли скачанный файл в материал: расширение и содержимое сходятся.

    Текстовый файл обязан читаться как UTF-8 без нулевых байтов — иначе это
    двоичное под чужим именем.
    """
    extension = material_extension(document.filename)
    if extension is None or not document.data:
        return False
    if extension == ".txt":
        if b"\x00" in document.data:
            return False
        try:
            document.data.decode("utf-8")
        except UnicodeDecodeError:
            return False
        return True
    return document.data.startswith(_SIGNATURES[extension])


def topic_from_filename(filename: str) -> str | None:
    """Тема по имени файла: «Итоги_квартала.docx» → «Итоги квартала».

    None — из имени темы не выходит (короче трёх знаков): тогда её спросят.
    Тема становится заголовком титульного слайда, и подставлять туда «a»
    нельзя.
    """
    stem, dot, _ = filename.rpartition(".")
    cleaned = " ".join((stem if dot else filename).replace("_", " ").split())
    return cleaned if MIN_TOPIC <= len(cleaned) <= MAX_TOPIC else None
