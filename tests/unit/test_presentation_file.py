"""Презентация по файлу (сессия 7, В3, В6).

Файл можно прислать вместо темы или кнопкой «Материал» на экране
параметров. Форматы и размер — по docs/API.md: pdf, docx, pptx, txt до
20 МБ. Тип и размер проверяются до провайдера; неподходящий файл — понятный
отказ с выходом, презентация не тратится. Содержимое файла — только в
памяти, не в базе и не в логах.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.adapters.storage.memory import InMemoryStorage
from app.core import texts
from app.core.actions import Action
from app.core.documents import DocumentTooLargeError
from app.core.models import Chat, Document, IncomingMessage, MessengerKind, User
from app.core.router import handle
from app.core.scenarios.deps import Deps
from app.ports.presentations import MATERIAL_MAX_BYTES
from tests.fakes import FakeLogger, FakeMessenger, FakePresentations

CHAT = Chat(messenger=MessengerKind.TELEGRAM, chat_id="1")
SECRET = "Секретная выручка за квартал: 12 345 678 рублей"

DOCX = Document(
    data=b"PK\x03\x04" + SECRET.encode(),
    filename="Итоги_квартала.docx",
    mime_type="application/octet-stream",
)
TXT = Document(data=SECRET.encode(), filename="заметки.txt", mime_type="text/plain")


def incoming(**fields: object) -> IncomingMessage:
    return IncomingMessage(chat=CHAT, external_user_id="1", **fields)  # type: ignore[arg-type]


def sent_file(name: str) -> IncomingMessage:
    return incoming(document_ref="file-ref", document_name=name)


@pytest.fixture
def presentations() -> FakePresentations:
    return FakePresentations()


@pytest.fixture
def enabled(deps: Deps, presentations: FakePresentations) -> Deps:
    return replace(deps, presentations=presentations)


@pytest.fixture
async def owner(storage: InMemoryStorage, user: User) -> User:
    await storage.add_bonus(user.id, presentations=1)
    fresh = await storage.get_user_by_id(user.id)
    assert fresh is not None
    return fresh


def buttons(messenger: FakeMessenger) -> list[tuple[str, str | None]]:
    keyboard = messenger.last_text.keyboard
    assert keyboard is not None
    return [(b.text, b.action) for row in keyboard.rows for b in row]


async def press(enabled: Deps, messenger: FakeMessenger, label: str) -> None:
    for text, action in buttons(messenger):
        if text == label:
            assert action is not None
            await handle(enabled, incoming(action=action))
            return
    raise AssertionError(f"нет кнопки «{label}»: {buttons(messenger)}")


async def _left(storage: InMemoryStorage, user: User) -> int:
    fresh = await storage.get_user_by_id(user.id)
    assert fresh is not None
    return fresh.bonus_presentations


# --- В3: колода по файлу ----------------------------------------------------


async def test_a_file_instead_of_a_topic_builds_a_deck_from_it(
    enabled: Deps,
    owner: User,
    messenger: FakeMessenger,
    presentations: FakePresentations,
    storage: InMemoryStorage,
) -> None:
    messenger.incoming_document = DOCX
    await handle(enabled, incoming(action=Action.MENU_PRESENTATIONS))

    await handle(enabled, sent_file(DOCX.filename))

    lines = messenger.last_text.text.split("\n")
    assert "📝 Тема: Итоги квартала" in lines
    assert "📎 Материал: файл «Итоги_квартала.docx» — соберём по нему" in lines
    await press(enabled, messenger, texts.BUTTON_DECK_BUILD)
    request = presentations.requests[0]
    assert request.file == DOCX and request.material == ""
    assert await _left(storage, owner) == 0


async def test_a_file_from_the_screen_becomes_its_material(
    enabled: Deps,
    owner: User,
    messenger: FakeMessenger,
    presentations: FakePresentations,
) -> None:
    messenger.incoming_document = TXT
    await handle(enabled, incoming(action=Action.MENU_PRESENTATIONS))
    await handle(enabled, incoming(text="Итоги пилота"))
    await press(enabled, messenger, texts.BUTTON_DECK_MATERIAL)
    assert messenger.last_text.text == texts.DECK_ASK_FILE

    await handle(enabled, sent_file(TXT.filename))

    assert "📝 Тема: Итоги пилота" in messenger.last_text.text.split("\n")
    assert "файл «заметки.txt»" in messenger.last_text.text
    await press(enabled, messenger, texts.BUTTON_DECK_BUILD)
    assert presentations.requests[0].file == TXT


async def test_the_screen_shows_the_name_the_person_sent(
    enabled: Deps,
    owner: User,
    messenger: FakeMessenger,
    presentations: FakePresentations,
) -> None:
    """Telegram отдаёт файл под служебным именем вроде «file_12.docx».

    Человеку на экране и провайдеру нужно имя из сообщения: по нему тема, и
    по его расширению API определяет тип.
    """
    messenger.incoming_document = replace(DOCX, filename="file_12.docx")
    await handle(enabled, incoming(action=Action.MENU_PRESENTATIONS))

    await handle(enabled, sent_file(DOCX.filename))

    assert "файл «Итоги_квартала.docx»" in messenger.last_text.text
    await press(enabled, messenger, texts.BUTTON_DECK_BUILD)
    file = presentations.requests[0].file
    assert file is not None and file.filename == DOCX.filename


async def test_the_material_can_be_taken_back(
    enabled: Deps,
    owner: User,
    messenger: FakeMessenger,
    presentations: FakePresentations,
) -> None:
    messenger.incoming_document = TXT
    await handle(enabled, incoming(action=Action.MENU_PRESENTATIONS))
    await handle(enabled, sent_file(TXT.filename))
    await press(enabled, messenger, texts.BUTTON_DECK_MATERIAL)

    await press(enabled, messenger, texts.BUTTON_DECK_NO_MATERIAL)

    assert "📎 Материал: нет — соберём по теме" in messenger.last_text.text
    await press(enabled, messenger, texts.BUTTON_DECK_BUILD)
    assert presentations.requests[0].file is None


async def test_a_file_name_too_short_for_a_topic_asks_for_one(
    enabled: Deps,
    owner: User,
    messenger: FakeMessenger,
    presentations: FakePresentations,
) -> None:
    """Тема станет заголовком титульного слайда — из «a.pdf» её не сделать."""
    messenger.incoming_document = replace(
        DOCX, data=b"%PDF-1.7 " + SECRET.encode(), filename="a.pdf"
    )
    await handle(enabled, incoming(action=Action.MENU_PRESENTATIONS))
    await handle(enabled, sent_file("a.pdf"))
    assert messenger.last_text.text == texts.DECK_FILE_TOPIC

    await handle(enabled, incoming(text="Итоги квартала"))

    assert "📝 Тема: Итоги квартала" in messenger.last_text.text.split("\n")
    assert "файл «a.pdf»" in messenger.last_text.text


# --- В3: неподходящий файл — до провайдера ---------------------------------


@pytest.mark.parametrize(
    "document",
    [
        Document(data=b"MZ\x90\x00", filename="вирус.exe", mime_type="x"),
        Document(data=b"\x89PNG\r\n", filename="фото.png", mime_type="image/png"),
        # Расширение врёт: под «.pdf» не PDF.
        Document(data=b"PK\x03\x04zip", filename="отчёт.pdf", mime_type="x"),
        # Под «.txt» — двоичное.
        Document(data=b"\x00\x01\x02\xff", filename="данные.txt", mime_type="x"),
        # Читается как UTF-8, но с нулевыми байтами — тоже двоичное.
        Document(data=b"abc\x00def", filename="данные.txt", mime_type="x"),
    ],
)
async def test_a_wrong_file_is_refused_before_the_provider(
    enabled: Deps,
    owner: User,
    messenger: FakeMessenger,
    presentations: FakePresentations,
    storage: InMemoryStorage,
    document: Document,
) -> None:
    messenger.incoming_document = document
    await handle(enabled, incoming(action=Action.MENU_PRESENTATIONS))

    await handle(enabled, sent_file(document.filename))

    assert messenger.last_text.text == texts.DECK_FILE_WRONG
    assert buttons(messenger) == [(texts.BUTTON_CANCEL, Action.MENU_SHOW)]
    assert presentations.requests == []
    assert presentations.themes_calls == 0
    assert await _left(storage, owner) == 1


async def test_a_wrong_extension_is_refused_without_downloading(
    enabled: Deps,
    owner: User,
    messenger: FakeMessenger,
) -> None:
    """Тип по расширению проверяется ещё до скачивания: чужое не тянем."""
    messenger.fail_download_document = AssertionError("скачивать было не нужно")
    await handle(enabled, incoming(action=Action.MENU_PRESENTATIONS))

    await handle(enabled, sent_file("архив.zip"))

    assert messenger.last_text.text == texts.DECK_FILE_WRONG


async def test_a_file_over_the_limit_is_refused_before_the_provider(
    enabled: Deps,
    owner: User,
    messenger: FakeMessenger,
    presentations: FakePresentations,
) -> None:
    """Размер мессенджер сообщает до загрузки: больше 20 МБ не тянем вовсе.

    Предел — из docs/API.md, а не общий предел файлов раздела «Файлы»: тот
    настраивается отдельно и с материалом презентации не связан.
    """
    enabled = replace(
        enabled, settings=replace(enabled.settings, max_document_bytes=1024)
    )
    messenger.fail_download_document = DocumentTooLargeError()
    await handle(enabled, incoming(action=Action.MENU_PRESENTATIONS))
    await handle(enabled, incoming(text="Итоги пилота"))
    await press(enabled, messenger, texts.BUTTON_DECK_MATERIAL)

    await handle(enabled, sent_file("большой.pdf"))

    assert messenger.last_text.text == texts.DECK_FILE_TOO_BIG
    assert buttons(messenger) == [(texts.BUTTON_BACK, buttons(messenger)[0][1])]
    assert presentations.requests == []
    assert messenger.downloads_limited_to == [MATERIAL_MAX_BYTES]


async def test_after_a_refusal_another_file_is_still_welcome(
    enabled: Deps,
    owner: User,
    messenger: FakeMessenger,
) -> None:
    messenger.incoming_document = Document(data=b"MZ", filename="x.exe", mime_type="x")
    await handle(enabled, incoming(action=Action.MENU_PRESENTATIONS))
    await handle(enabled, sent_file("x.exe"))

    messenger.incoming_document = TXT
    await handle(enabled, sent_file(TXT.filename))

    assert "файл «заметки.txt»" in messenger.last_text.text


# --- В6: содержимое файла — только в памяти ---------------------------------


async def test_the_file_never_reaches_the_database_or_the_log(
    enabled: Deps,
    owner: User,
    messenger: FakeMessenger,
    presentations: FakePresentations,
    storage: InMemoryStorage,
    logger: FakeLogger,
) -> None:
    from app.ports.presentations import PresentationError

    messenger.incoming_document = DOCX
    presentations.errors = [PresentationError("RENDER_FAILED")]
    await handle(enabled, incoming(action=Action.MENU_PRESENTATIONS))
    await handle(enabled, sent_file(DOCX.filename))
    await press(enabled, messenger, texts.BUTTON_DECK_BUILD)

    user = await storage.get_user_by_id(owner.id)
    assert user is not None
    stored = f"{user.pending} {user.retry_context}"
    logged = str([(e.event, e.fields) for e in logger.events])
    for secret in ("Секретная", "12 345 678", "Итоги_квартала", "Итоги квартала"):
        assert secret not in stored
        assert secret not in logged
