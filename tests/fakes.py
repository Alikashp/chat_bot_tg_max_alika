"""Фейковые реализации портов для тестов сценариев.

Не моки с проверкой вызовов, а простые записывающие подделки: тест смотрит,
что в итоге получил пользователь, а не в каком порядке дёргались методы.
Проверять последовательность вызовов значит зацементировать текущую
реализацию и ловить ложные падения при любой перестановке строк.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.core.models import (
    Chat,
    ChatTurn,
    Keyboard,
    MessageRef,
    Photo,
)
from app.ports.ai import ImageQuality

#: Минимальный настоящий PNG: восемь байт сигнатуры плюс немного тела.
#: Проверка формата смотрит именно на сигнатуру, поэтому подделка обязана
#: быть похожей на картинку по-настоящему.
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@dataclass
class SentText:
    chat: Chat
    text: str
    keyboard: Keyboard | None
    show_menu: bool


@dataclass
class SentPhoto:
    chat: Chat
    photo: Photo
    caption: str | None
    keyboard: Keyboard | None


@dataclass
class EditedText:
    ref: MessageRef
    text: str
    keyboard: Keyboard | None


@dataclass
class EditedToPhoto:
    ref: MessageRef
    photo: Photo
    caption: str | None
    keyboard: Keyboard | None


class FakeMessenger:
    """Записывает всё, что бот отправил пользователю."""

    def __init__(self) -> None:
        self.texts: list[SentText] = []
        self.photos: list[SentPhoto] = []
        self.text_edits: list[EditedText] = []
        self.photo_edits: list[EditedToPhoto] = []
        self.typing: list[Chat] = []
        self.answered_callbacks: list[str] = []
        #: Если задано, отправка текста падает. Нужно для проверки инварианта:
        #: лимит не списывается, когда результат до пользователя не доехал.
        self.fail_send: Exception | None = None
        #: То же для замены сообщения картинкой.
        self.fail_edit_to_photo: Exception | None = None
        self._next_id = 0

    def _new_ref(self, chat: Chat) -> MessageRef:
        self._next_id += 1
        return MessageRef(chat=chat, message_id=str(self._next_id))

    async def send_text(
        self,
        chat: Chat,
        text: str,
        *,
        keyboard: Keyboard | None = None,
        show_menu: bool = True,
    ) -> MessageRef:
        if self.fail_send is not None:
            raise self.fail_send
        self.texts.append(SentText(chat, text, keyboard, show_menu))
        return self._new_ref(chat)

    async def send_photo(
        self,
        chat: Chat,
        photo: Photo,
        *,
        caption: str | None = None,
        keyboard: Keyboard | None = None,
        show_menu: bool = True,
    ) -> MessageRef:
        self.photos.append(SentPhoto(chat, photo, caption, keyboard))
        return self._new_ref(chat)

    async def edit_text(
        self,
        ref: MessageRef,
        text: str,
        *,
        keyboard: Keyboard | None = None,
    ) -> None:
        self.text_edits.append(EditedText(ref, text, keyboard))

    async def edit_to_photo(
        self,
        ref: MessageRef,
        photo: Photo,
        *,
        caption: str | None = None,
        keyboard: Keyboard | None = None,
    ) -> None:
        if self.fail_edit_to_photo is not None:
            raise self.fail_edit_to_photo
        self.photo_edits.append(EditedToPhoto(ref, photo, caption, keyboard))

    async def send_typing(self, chat: Chat) -> None:
        self.typing.append(chat)

    async def download_photo(self, file_id: str, *, max_bytes: int) -> Photo:
        return Photo(data=PNG_BYTES)

    async def answer_callback(
        self, callback_id: str, *, notification: str | None = None
    ) -> None:
        self.answered_callbacks.append(callback_id)

    # --- Удобства для утверждений ------------------------------------

    @property
    def last_text(self) -> SentText:
        return self.texts[-1]

    def texts_said(self) -> list[str]:
        return [sent.text for sent in self.texts]


class FakeLLM:
    """Провайдер текста с заранее заданным поведением."""

    def __init__(self, answer: str = "Ответ.", error: Exception | None = None):
        self.answer = answer
        self.error = error
        self.calls: list[tuple[tuple[ChatTurn, ...], str]] = []

    async def complete(self, turns: Sequence[ChatTurn], *, model: str) -> str:
        self.calls.append((tuple(turns), model))
        if self.error is not None:
            raise self.error
        return self.answer


class FakeImages:
    """Провайдер картинок с заранее заданным поведением."""

    def __init__(self, error: Exception | None = None):
        self.error = error
        self.generated: list[tuple[str, ImageQuality]] = []
        self.edited: list[tuple[str, ImageQuality]] = []

    async def generate(self, prompt: str, *, quality: ImageQuality) -> Photo:
        self.generated.append((prompt, quality))
        if self.error is not None:
            raise self.error
        return Photo(data=PNG_BYTES)

    async def edit(
        self, source: Photo, instruction: str, *, quality: ImageQuality
    ) -> Photo:
        self.edited.append((instruction, quality))
        if self.error is not None:
            raise self.error
        return Photo(data=PNG_BYTES)


@dataclass
class LoggedEvent:
    level: str
    event: str
    fields: dict[str, str | int | float | bool | None]


class FakeLogger:
    """Логгер, по которому видно, сообщили ли о сбое."""

    def __init__(self) -> None:
        self.events: list[LoggedEvent] = []

    def info(self, event: str, **fields: str | int | float | bool | None) -> None:
        self.events.append(LoggedEvent("info", event, fields))

    def warning(self, event: str, **fields: str | int | float | bool | None) -> None:
        self.events.append(LoggedEvent("warning", event, fields))

    def error(self, event: str, **fields: str | int | float | bool | None) -> None:
        self.events.append(LoggedEvent("error", event, fields))

    def names(self) -> list[str]:
        return [entry.event for entry in self.events]


@dataclass
class FrozenClock:
    """Часы, которые стоят на месте, пока их не двинут."""

    now: datetime = field(
        default_factory=lambda: datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
    )

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        from datetime import timedelta

        self.now += timedelta(**kwargs)
