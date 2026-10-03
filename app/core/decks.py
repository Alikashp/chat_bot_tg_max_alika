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
    MAX_SLIDES,
    MIN_SLIDES,
    DeckRequest,
    PresentationTheme,
)


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
    #: Присланный файл-материал. Вместе с текстом не бывает: API принимает
    #: что-то одно.
    file: Document | None = None
    #: Жетон кнопки «Сделать презентацию по докладу», с которой пришёл
    #: черновик. Забирается, когда колода доставлена: кнопка под докладом
    #: тогда честно скажет «уже сделано».
    report_token: str | None = None

    def request(self) -> DeckRequest:
        """Запрос на сборку ровно по тому, что показано на экране."""
        return DeckRequest(
            topic=self.topic,
            theme_id=self.theme_id,
            language=self.language,
            slides=self.slides,
            audience=self.audience,
            material=self.material if self.file is None else "",
            file=self.file,
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


def weight(draft: Draft) -> int:
    """Сколько памяти держит черновик: файл и текст доклада."""
    return len(draft.file.data if draft.file is not None else b"") + len(draft.material)
