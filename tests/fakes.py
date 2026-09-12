"""Фейковые реализации портов для тестов сценариев.

Не моки с проверкой вызовов, а простые записывающие подделки: тест смотрит,
что в итоге получил пользователь, а не в каком порядке дёргались методы.
Проверять последовательность вызовов значит зацементировать текущую
реализацию и ловить ложные падения при любой перестановке строк.
"""

from __future__ import annotations

import asyncio
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
from app.core.receipts import Receipt
from app.ports.ai import Answer, ImageQuality
from app.ports.payments import PaymentIntent

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
class SentPhotoRef:
    chat: Chat
    photo_ref: str
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
        self.photo_refs: list[SentPhotoRef] = []
        self.text_edits: list[EditedText] = []
        self.photo_edits: list[EditedToPhoto] = []
        self.typing: list[Chat] = []
        self.downloaded: list[str] = []
        self.answered_callbacks: list[str] = []
        #: Если задано, отправка текста падает. Нужно для проверки инварианта:
        #: лимит не списывается, когда результат до пользователя не доехал.
        self.fail_send: Exception | None = None
        #: То же для замены сообщения картинкой.
        self.fail_edit_to_photo: Exception | None = None
        #: Ссылка, которую мессенджер возвращает на доставленную картинку.
        #: None означает «мессенджер ссылки не даёт» — такое тоже надо уметь.
        self.delivered_photo_ref: str | None = "photo-ref"
        #: Чем падает скачивание присланного фото.
        self.fail_download: Exception | None = None
        #: Отправленные альбомы: куда и что именно, в порядке отправки.
        self.albums: list[tuple[Chat, tuple[Photo, ...]]] = []
        #: Чем падает отправка альбома. Примеры — вежливость, и их сбой не
        #: должен ронять сам выбор прикола.
        self.fail_album: Exception | None = None
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
    ) -> str | None:
        if self.fail_edit_to_photo is not None:
            raise self.fail_edit_to_photo
        self.photo_edits.append(EditedToPhoto(ref, photo, caption, keyboard))
        return self.delivered_photo_ref

    async def send_photo_by_ref(
        self,
        chat: Chat,
        photo_ref: str,
        *,
        caption: str | None = None,
        keyboard: Keyboard | None = None,
        show_menu: bool = True,
    ) -> MessageRef:
        self.photo_refs.append(SentPhotoRef(chat, photo_ref, caption, keyboard))
        return self._new_ref(chat)

    async def send_album(self, chat: Chat, photos: Sequence[Photo]) -> None:
        if self.fail_album is not None:
            raise self.fail_album
        self.albums.append((chat, tuple(photos)))

    async def send_typing(self, chat: Chat) -> None:
        self.typing.append(chat)

    async def download_photo(self, file_id: str, *, max_bytes: int) -> Photo:
        # Уступаем управление, как это делает всякая настоящая закачка. Без
        # этого фейк проходит насквозь, не прерываясь, и проверки про два
        # одновременных обращения проверяли бы пустоту.
        await asyncio.sleep(0)
        if self.fail_download is not None:
            raise self.fail_download
        self.downloaded.append(file_id)
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
        #: Оборвался ли ответ на потолке длины. Про это бот обязан сказать
        #: человеку кнопкой, а не выдавать огрызок за законченную мысль.
        self.truncated = False
        #: Сколько токенов «посчитал» провайдер. None — не сказал: не всякий
        #: шлюз возвращает usage, и учёт обязан это пережить.
        self.tokens_in: int | None = 11
        self.tokens_out: int | None = 22
        #: Часы, которые вызов двигает вперёд. Нужны там, где проверяется
        #: измеренная длительность: с замороженным временем она всегда ноль.
        self.clock: FrozenClock | None = None
        self.takes_seconds = 0.0

    async def complete(self, turns: Sequence[ChatTurn], *, model: str) -> Answer:
        self.calls.append((tuple(turns), model))
        if self.clock is not None and self.takes_seconds:
            self.clock.advance(seconds=self.takes_seconds)
        if self.error is not None:
            raise self.error
        return Answer(
            text=self.answer,
            truncated=self.truncated,
            tokens_in=self.tokens_in,
            tokens_out=self.tokens_out,
        )


class FakeImages:
    """Провайдер картинок с заранее заданным поведением."""

    def __init__(self, error: Exception | None = None):
        self.error = error
        self.generated: list[tuple[str, ImageQuality]] = []
        self.edited: list[tuple[str, ImageQuality]] = []
        #: Исходники каждой правки — по ним видно и сколько фото уехало, и в
        #: каком порядке. Порядок у «я и я в детстве» меняет результат.
        self.edited_sources: list[tuple[Photo, ...]] = []
        #: Какую модель просили на каждый вызов. Пусто — общую, настроенную
        #: у провайдера.
        self.models: list[str] = []
        #: Какой input_fidelity просили на каждую правку. None — настроенный
        #: у провайдера, пустая строка — «не посылать этой модели».
        self.fidelities: list[str | None] = []

    async def generate(
        self, prompt: str, *, quality: ImageQuality, model: str = ""
    ) -> Photo:
        self.models.append(model)
        self.generated.append((prompt, quality))
        if self.error is not None:
            raise self.error
        return Photo(data=PNG_BYTES)

    async def edit(
        self,
        sources: Sequence[Photo],
        instruction: str,
        *,
        quality: ImageQuality,
        model: str = "",
        input_fidelity: str | None = None,
    ) -> Photo:
        self.models.append(model)
        self.fidelities.append(input_fidelity)
        self.edited.append((instruction, quality))
        self.edited_sources.append(tuple(sources))
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


class FakeGuard:
    """Ограничитель одновременных задач, за которым видно, что он сработал."""

    def __init__(self, *, limit: int = 1) -> None:
        self._limit = limit
        self._active: dict[str, int] = {}
        self.refused: list[str] = []
        #: Сколько времени ограничителю разрешали ждать на каждом обращении.
        self.waited: list[float] = []

    def try_acquire(self, key: str) -> bool:
        current = self._active.get(key, 0)
        if current >= self._limit:
            self.refused.append(key)
            return False
        self._active[key] = current + 1
        return True

    async def acquire(self, key: str, *, wait_seconds: float = 0.0) -> bool:
        """Ждать в тесте нечего: слот освобождает только сам тест.

        Готовность подождать записывается, чтобы было видно, где ограничитель
        отказывает сразу, а где сначала даёт шанс.
        """
        self.waited.append(wait_seconds)
        return self.try_acquire(key)

    def release(self, key: str) -> None:
        current = self._active.get(key, 0)
        if current <= 1:
            self._active.pop(key, None)
            return
        self._active[key] = current - 1

    def active(self, key: str) -> int:
        return self._active.get(key, 0)


class FakeChannel:
    """Канал, про который заранее известно, кто на него подписан.

    ``error`` отличает «не подписан» от «проверить не удалось»: сценарий
    обязан вести себя в этих случаях по-разному, и без такой подделки
    разница не проверяется ничем.
    """

    def __init__(self, *, members: set[str] | None = None) -> None:
        self.members = members if members is not None else set()
        self.error: Exception | None = None
        #: Кого спрашивали. Нужен там, где важно, что спросили про того.
        self.asked: list[str] = []

    async def has_member(self, external_user_id: str) -> bool:
        self.asked.append(external_user_id)
        if self.error is not None:
            raise self.error
        return external_user_id in self.members


class FakeCards:
    """Провайдер оплаты картой с заранее заданным поведением."""

    def __init__(self, *, recurring: bool = False) -> None:
        self.created: list[tuple[str, int]] = []
        self.charged: list[tuple[str, int, str]] = []
        #: О каких платежах спрашивали «оплачено ли». Нужен там, где важно
        #: не только что спросили, а у кого именно.
        self.asked: list[str] = []
        self.error: Exception | None = None
        #: Умеет ли провайдер повторные списания.
        self.recurring = recurring
        #: Что провайдер отвечает на вопрос «оплачено ли».
        self.paid: bool = True
        #: Вернули ли деньги по платежу. Отдельно от paid: у ЮKassa возврат
        #: не меняет статус платежа, и это ровно та разница, которую код
        #: обязан различать.
        self.refunded: bool = False
        #: Ссылка, которую он возвращает. None — провайдер её не дал.
        self.confirmation_url: str | None = "https://pay.example/checkout"
        #: Сохранённый способ оплаты. None — сохранить не удалось.
        self.saved_method: str | None = "card-1"
        #: Проходит ли повторное списание.
        self.charge_succeeds: bool = True
        #: Просили ли сохранить способ оплаты при последнем платеже.
        self.saved_requested: bool = False
        #: Чеки, ушедшие вместе с платежами, — по одному на каждый вызов.
        #: None означает, что чек не отправляли вовсе.
        self.receipts: list[Receipt | None] = []

    async def create_payment(
        self,
        *,
        order_id: str,
        amount_rub: int,
        description: str,
        save_method: bool = False,
        receipt: Receipt | None = None,
    ) -> PaymentIntent:
        if self.error is not None:
            raise self.error
        self.saved_requested = save_method
        self.receipts.append(receipt)
        self.created.append((order_id, amount_rub))
        return PaymentIntent(
            external_id=f"ext-{len(self.created)}",
            confirmation_url=self.confirmation_url,
        )

    async def charge_saved(
        self,
        *,
        order_id: str,
        amount_rub: int,
        description: str,
        payment_method_id: str,
        receipt: Receipt | None = None,
    ) -> str | None:
        if self.error is not None:
            raise self.error
        self.receipts.append(receipt)
        self.charged.append((order_id, amount_rub, payment_method_id))
        if not self.charge_succeeds:
            return None
        return f"charge-{len(self.charged)}"

    async def saved_method_of(self, external_id: str) -> str | None:
        return self.saved_method

    async def is_paid(self, external_id: str, *, expected_rub: int) -> bool:
        self.asked.append(external_id)
        return self.paid

    async def is_refunded(self, external_id: str) -> bool:
        self.asked.append(external_id)
        return self.refunded


@dataclass
class SentInvoice:
    title: str
    order_id: str
    stars: int
    period_days: int


class FakeStars:
    """Оплата звёздами: записывает счета, отмены и предварительные ответы."""

    def __init__(self) -> None:
        self.invoices: list[SentInvoice] = []
        self.approvals: list[tuple[str, bool]] = []
        self.cancelled: list[tuple[str, str]] = []
        self.error: Exception | None = None
        #: Сбой отмены — отдельно от сбоя выставления счёта.
        self.cancel_error: Exception | None = None

    async def subscription_link(
        self,
        *,
        title: str,
        description: str,
        order_id: str,
        stars: int,
        period_days: int,
    ) -> str:
        if self.error is not None:
            raise self.error
        self.invoices.append(SentInvoice(title, order_id, stars, period_days))
        return f"https://t.me/invoice/{order_id}"

    async def cancel(self, *, user_id: str, charge_id: str) -> None:
        if self.cancel_error is not None:
            raise self.cancel_error
        self.cancelled.append((user_id, charge_id))

    async def approve(
        self, request_id: str, *, ok: bool, reason: str | None = None
    ) -> None:
        self.approvals.append((request_id, ok))
