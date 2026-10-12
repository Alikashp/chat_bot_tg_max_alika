"""Разбор большого файла не останавливает остальных (сессия 10, шаг 4, С4).

Разбор PDF, DOCX и PPTX — работа процессора в чужих библиотеках, и в петле
событий она держит всех: пока разбирается двадцатимегабайтный файл одного
человека, другой не получает ответа даже на «привет». Поэтому разбор и
сборка файлов уходят в поток.

Тест держит разбор, пока другой человек не получит ответ чата. Если разбор
занимает петлю, другой не начнёт, пока разбор не кончится, и ожидание
разбора истечёт по таймауту — тест упадёт.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator

import pytest

from app.adapters.documents import reader as reader_module
from app.adapters.storage.memory import InMemoryStorage
from app.core import support
from app.core.documents import DocumentFormat
from app.core.models import Chat, Document, MessengerKind
from app.core.scenarios import chat, documents
from app.core.scenarios.deps import Deps, Session
from config.documents import DOCUMENT_ACTIONS
from tests.fakes import FakeLLM, FakeMessenger
from tests.unit.test_documents import _docx

#: Сколько разбор ждёт ответа другому человеку. Дольше — значит, петля
#: занята разбором, и ответ не начнётся, пока разбор не кончится.
_PATIENCE_SECONDS = 2.0


async def test_a_big_file_being_read_does_not_keep_others_waiting(
    deps: Deps,
    session: Session,
    storage: InMemoryStorage,
    llm: FakeLLM,
    messenger: FakeMessenger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parsing = threading.Event()
    answered = threading.Event()
    others_answered_meanwhile: list[bool] = []
    real = reader_module._readers[DocumentFormat.DOCX]

    def slow(source: object) -> Iterator[str]:
        # Разбор «большого файла»: держит поток, пока другой не получит ответ.
        parsing.set()
        others_answered_meanwhile.append(answered.wait(_PATIENCE_SECONDS))
        return real(source)  # type: ignore[arg-type]

    monkeypatch.setitem(reader_module._readers, DocumentFormat.DOCX, slow)

    other_user = await storage.create_user(
        messenger=MessengerKind.TELEGRAM,
        external_id="2",
        referral_code="code2",
        support_number=support.generate_number(),
        bonus_images=0,
        bonus_documents=0,
    )
    other = Session(
        user=other_user,
        chat=Chat(messenger=MessengerKind.TELEGRAM, chat_id="2"),
        day=deps.today(),
        now=deps.now(),
    )
    await storage.add_bonus(session.user.id, documents=1)
    action = next(a for a in DOCUMENT_ACTIONS.values() if a.needs_file)
    upload = _docx("Глава первая", "Текст главы")
    document = Document(
        data=upload.data, filename="big.docx", mime_type=upload.mime_type
    )

    async def someone_else_asks() -> None:
        # Ждём начала разбора в потоке, не занимая петлю.
        assert await asyncio.to_thread(parsing.wait, 5)
        await chat.handle_message(deps, other, "привет")
        answered.set()

    await asyncio.wait_for(
        asyncio.gather(
            documents.apply(deps, session, action, document), someone_else_asks()
        ),
        timeout=10,
    )

    assert others_answered_meanwhile == [True]
    assert len(llm.calls) == 2
