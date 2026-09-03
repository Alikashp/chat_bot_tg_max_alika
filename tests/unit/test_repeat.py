"""Кнопки «Ещё раз» и «Поделиться».

Их четыре штуки на три сценария, и все они работают через один запомненный
контекст. Проверяется главным образом то, что контекст переживает сбой: после
неудачи повторять надо именно то, что не получилось, а не просить человека
набирать всё заново.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from app.adapters.storage.memory import InMemoryStorage
from app.core import texts
from app.core.models import Photo, TariffId, User
from app.core.retry_context import RetryContext, RetryKind
from app.core.scenarios import chat, images, presets, repeat
from app.core.scenarios.deps import Deps, Session
from config import presets as registry
from tests.fakes import PNG_BYTES, FakeImages, FakeLLM, FakeMessenger


async def test_repeat_after_a_chat_failure_resends_the_same_question(
    deps: Deps, session: Session, llm: FakeLLM
) -> None:
    llm.error = RuntimeError("провайдер лёг")
    await chat.handle_message(deps, session, "какая столица Франции?")
    llm.error = None

    await repeat.repeat_last(deps, await _refreshed(deps, session), RetryKind.CHAT)

    assert llm.calls[-1][0][-1].content == "какая столица Франции?"


async def test_repeat_after_a_drawing_failure_uses_the_same_description(
    deps: Deps, session: Session, images_: FakeImages
) -> None:
    images_.error = RuntimeError("провайдер лёг")
    await images.draw(deps, session, "кот-космонавт")
    images_.error = None

    await repeat.repeat_last(deps, await _refreshed(deps, session), RetryKind.IMAGE)

    assert images_.generated[-1][0] == "кот-космонавт"


async def test_repeat_after_a_preset_failure_reuses_the_same_photo(
    deps: Deps, session: Session, images_: FakeImages, messenger: FakeMessenger
) -> None:
    preset = registry.PRESETS["lego"]
    images_.error = RuntimeError("провайдер лёг")
    await presets.apply(deps, session, preset, [Photo(data=PNG_BYTES)], ["source-1"])
    images_.error = None

    await repeat.repeat_last(deps, await _refreshed(deps, session), RetryKind.PRESET)

    assert messenger.downloaded == ["source-1"]
    assert len(images_.edited) == 2


async def test_share_forwards_the_delivered_photo_by_reference(
    deps: Deps, session: Session, messenger: FakeMessenger, user: User
) -> None:
    """Байты у мессенджера уже есть — заливать их второй раз незачем."""
    await images.draw(deps, session, "кот-космонавт")

    await repeat.share_last(deps, await _refreshed(deps, session), RetryKind.IMAGE)

    assert len(messenger.photo_refs) == 1
    assert messenger.photo_refs[0].photo_ref == "photo-ref"
    assert user.referral_code in (messenger.photo_refs[0].caption or "")


async def test_share_without_a_result_does_not_dead_end(
    deps: Deps, session: Session, messenger: FakeMessenger
) -> None:
    await repeat.share_last(deps, session, RetryKind.IMAGE)

    assert messenger.texts_said() == [texts.NOTHING_TO_REPEAT]


async def test_repeat_without_context_does_not_dead_end(
    deps: Deps, session: Session, messenger: FakeMessenger
) -> None:
    """Кнопка пережила контекст: перезапуск, чистка, старое сообщение."""
    await repeat.repeat_last(deps, session, RetryKind.CHAT)

    assert messenger.texts_said() == [texts.NOTHING_TO_REPEAT]


async def test_a_corrupted_context_is_survivable(
    deps: Deps, session: Session, storage: InMemoryStorage, messenger: FakeMessenger
) -> None:
    """Формат мог поменяться между версиями — падать из-за этого нельзя."""
    await storage.set_retry_context(session.user.id, "{это не наш json")

    await repeat.repeat_last(deps, await _refreshed(deps, session), RetryKind.CHAT)

    assert messenger.texts_said() == [texts.NOTHING_TO_REPEAT]


async def test_a_preset_removed_from_the_registry_does_not_dead_end(
    deps: Deps, session: Session, storage: InMemoryStorage, messenger: FakeMessenger
) -> None:
    await storage.set_retry_context(
        session.user.id,
        RetryContext(
            kind=RetryKind.PRESET, preset_id="gone", source_photos=("p1",)
        ).encode(),
    )

    await repeat.repeat_last(deps, await _refreshed(deps, session), RetryKind.PRESET)

    assert messenger.texts_said() == [texts.PRESETS_ASK]


async def test_a_successful_chat_answer_clears_the_retry_context(
    deps: Deps, session: Session, storage: InMemoryStorage
) -> None:
    """«Повторить» под ответом не показывается, и повторять уже нечего."""
    await chat.handle_message(deps, session, "привет")

    refreshed = await storage.get_user_by_id(session.user.id)
    assert refreshed is not None
    assert refreshed.retry_context is None


async def _refreshed(deps: Deps, session: Session) -> Session:
    """Сессия с перечитанным пользователем — как её собирает маршрутизатор."""
    user = await deps.storage.get_user_by_id(session.user.id)
    assert user is not None
    return Session(user=user, chat=session.chat, day=session.day, now=session.now)


async def test_a_stale_button_does_not_repeat_the_wrong_thing(
    deps: Deps,
    session: Session,
    llm: FakeLLM,
    images_: FakeImages,
    messenger: FakeMessenger,
) -> None:
    """Кнопки живут в переписке вечно, а контекст один на пользователя.

    Человек получил ошибку в чате, потом нарисовал картинку, а потом
    прокрутил переписку вверх и нажал ту самую «Повторить». Без проверки вида
    он получил бы вторую картинку — и списанный за неё лимит, которого не
    просил тратить.
    """
    llm.error = RuntimeError("провайдер лёг")
    await chat.handle_message(deps, session, "какая столица Франции?")
    llm.error = None
    await images.draw(deps, await _refreshed(deps, session), "кот-космонавт")

    await repeat.repeat_last(deps, await _refreshed(deps, session), RetryKind.CHAT)

    assert len(images_.generated) == 1, "кнопка чата перерисовала картинку"
    # Единственный вызов провайдера текста — тот самый, который упал.
    assert len(llm.calls) == 1
    assert messenger.texts_said()[-1] == texts.NOTHING_TO_REPEAT


async def test_a_context_written_before_two_photo_presets_still_works(
    deps: Deps, session: Session, storage: InMemoryStorage, images_: FakeImages
) -> None:
    """Записи в старом виде лежат у всех, кто хоть раз пользовался приколами.

    Человек нажмёт «Ещё раз» под картинкой, которую получил вчера, и кнопка
    обязана сработать: одно поле-строка читается как список из одного снимка.
    """
    await storage.set_retry_context(
        session.user.id,
        '{"kind":"preset","preset_id":"lego","source_photo":"yesterday"}',
    )
    refreshed = await storage.get_user_by_id(session.user.id)
    assert refreshed is not None

    await repeat.repeat_last(deps, replace(session, user=refreshed), RetryKind.PRESET)

    assert len(images_.edited) == 1
    assert images_.edited[0][0] == registry.PRESETS["lego"].instruction


async def test_repeating_a_two_photo_preset_reuses_both_photos(
    deps: Deps,
    session: Session,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    images_: FakeImages,
) -> None:
    # Тариф ставится в хранилище, а не только в снимке сессии: ниже
    # пользователь перечитывается оттуда, и подмена в копии не пережила бы
    # перечитывание — а замок читает именно перечитанного.
    await storage.set_tariff(
        session.user.id, TariffId.LITE, expires_at=session.now + timedelta(days=30)
    )
    await storage.set_retry_context(
        session.user.id,
        RetryContext(
            kind=RetryKind.PRESET,
            preset_id="polaroid_child",
            source_photos=("adult", "child"),
        ).encode(),
    )
    refreshed = await storage.get_user_by_id(session.user.id)
    assert refreshed is not None

    await repeat.repeat_last(deps, replace(session, user=refreshed), RetryKind.PRESET)

    assert messenger.downloaded == ["adult", "child"]
    assert len(images_.edited_sources[0]) == 2


async def test_a_preset_that_changed_its_number_of_photos_does_not_dead_end(
    deps: Deps, session: Session, storage: InMemoryStorage, messenger: FakeMessenger
) -> None:
    """Контекст помнит один снимок, а прикол просит два — повторять нечего."""
    await storage.set_retry_context(
        session.user.id,
        RetryContext(
            kind=RetryKind.PRESET,
            preset_id="polaroid_child",
            source_photos=("adult",),
        ).encode(),
    )
    refreshed = await storage.get_user_by_id(session.user.id)
    assert refreshed is not None

    await repeat.repeat_last(deps, replace(session, user=refreshed), RetryKind.PRESET)

    assert messenger.texts_said()[-1] == texts.PRESETS_ASK


async def test_a_lapsed_subscription_closes_the_preset_again(
    deps: Deps,
    session: Session,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    images_: FakeImages,
) -> None:
    """Кнопка «Ещё раз» остаётся в переписке навсегда — и замок тоже.

    Человек оформил подписку, сделал фигурку, подписка кончилась. Кнопка под
    прошлогодней картинкой никуда не делась, и без проверки на этой двери она
    рисовала бы ему новую фигурку бесплатно сколько угодно раз.
    """
    await storage.set_retry_context(
        session.user.id,
        RetryContext(
            kind=RetryKind.PRESET,
            preset_id="figurine",
            source_photos=("last-year",),
        ).encode(),
    )
    refreshed = await storage.get_user_by_id(session.user.id)
    assert refreshed is not None

    await repeat.repeat_last(deps, replace(session, user=refreshed), RetryKind.PRESET)

    assert messenger.texts_said()[-1] == texts.PRESET_LOCKED
    assert images_.edited == []


async def test_a_stale_photo_link_does_not_kill_the_handler(
    deps: Deps,
    session: Session,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    images_: FakeImages,
) -> None:
    """Кнопка живёт в переписке вечно, а ссылка на фото — нет.

    Без обработки человек не получил бы вообще ничего: обработчик упал бы
    молча, и «Ещё раз» выглядело бы неработающей кнопкой.
    """
    await storage.set_retry_context(
        session.user.id,
        RetryContext(
            kind=RetryKind.PRESET, preset_id="lego", source_photos=("stale",)
        ).encode(),
    )
    refreshed = await storage.get_user_by_id(session.user.id)
    assert refreshed is not None
    messenger.fail_download = RuntimeError("ссылка протухла")

    await repeat.repeat_last(deps, replace(session, user=refreshed), RetryKind.PRESET)

    assert messenger.last_text.text == texts.PRESET_PHOTO_LOST
    assert images_.edited == []
