"""Маршрутизация входящего сообщения по сценариям.

Это ядро адаптера, вынесенное из адаптера. Telegram и MAX присылают разные
объекты, но правила «что делать с нажатием кнопки», «что считать описанием
картинки» и «когда пора здороваться» у них общие. Оставить эти правила в
адаптерах значило бы написать их дважды и разойтись на первой же правке —
ровно то дублирование продуктовой логики, которое запрещает §8 задания.

Поэтому адаптер приводит своё обновление к IncomingMessage и зовёт handle().
Проверка A4 на фазе 7 звучит так: MAX-адаптеру не должно понадобиться ни
строчки в core/. Всё, что ниже, написано ради этой проверки.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from app.core import pending, texts
from app.core.actions import Action, parse_buy_action, parse_preset_action
from app.core.models import IncomingMessage
from app.core.photos import PhotoTooLargeError
from app.core.retry_context import RetryKind
from app.core.scenarios import (
    chat,
    images,
    keyboards,
    onboarding,
    presets,
    profile,
    referral,
    repeat,
    tariffs,
)
from app.core.scenarios.deps import Deps, Session
from config import presets as registry

#: Команда, с которой начинается знакомство. Одинакова в обоих мессенджерах.
START_COMMAND = "/start"

#: Какая кнопка что ожидает найти в запомненном контексте.
#:
#: Контекст один на пользователя, а кнопки живут в переписке вечно. Без этого
#: соответствия «Повторить» под давней ошибкой чата перерисовала бы последнюю
#: картинку и списала бы за неё лимит.
_REPEAT_KINDS: dict[str, RetryKind] = {
    Action.CHAT_RETRY: RetryKind.CHAT,
    Action.IMAGE_AGAIN: RetryKind.IMAGE,
    Action.IMAGE_RETRY: RetryKind.IMAGE,
    Action.PRESET_AGAIN: RetryKind.PRESET,
    Action.PRESET_RETRY: RetryKind.PRESET,
}

#: То же для кнопок «Поделиться» и «Отправить другу».
_SHARE_KINDS: dict[str, RetryKind] = {
    Action.IMAGE_SHARE: RetryKind.IMAGE,
    Action.PRESET_SHARE: RetryKind.PRESET,
}


async def handle(deps: Deps, incoming: IncomingMessage) -> None:
    """Обрабатывает одно обращение пользователя.

    Единственная точка входа для обоих адаптеров.
    """
    if incoming.callback_id is not None:
        # Гасим «часики» на кнопке до всякой работы: она может занять
        # пятнадцать секунд, и всё это время кнопка выглядела бы зависшей.
        await _answer_callback(deps, incoming.callback_id)

    if incoming.start_payload is not None:
        await onboarding.start(
            deps,
            incoming.chat,
            incoming.chat.messenger,
            incoming.external_user_id,
            incoming.start_payload,
        )
        return

    user = await deps.storage.get_user(
        incoming.chat.messenger, incoming.external_user_id
    )
    if user is None:
        # Человек пишет боту, не нажав /start: так бывает после чистки базы
        # или если в чат вернулись по старой переписке. Здороваемся и заводим
        # пользователя — иначе следующая же строка упадёт на пустом месте.
        await onboarding.start(
            deps,
            incoming.chat,
            incoming.chat.messenger,
            incoming.external_user_id,
            "",
        )
        return

    session = Session(user=user, chat=incoming.chat, day=deps.today())

    action = incoming.action or keyboards.action_for_label(incoming.text)
    if action is not None:
        await _route_action(deps, session, action)
        return

    if incoming.photo_ref is not None:
        await _handle_photo(deps, session, incoming.photo_ref)
        return

    if incoming.text:
        await _handle_text(deps, session, incoming.text)
        return

    # Стикер, голосовое, документ, опрос — всё, чего бот пока не умеет.
    await _say(deps, session, texts.unsupported_input().text)


# --- Нажатия -------------------------------------------------------------


async def _route_action(deps: Deps, session: Session, action: str) -> None:
    """Разбирает действие и зовёт нужный сценарий."""
    preset_id = parse_preset_action(action)
    if preset_id is not None:
        await _pick_preset(deps, session, preset_id)
        return

    repeat_kind = _REPEAT_KINDS.get(action)
    if repeat_kind is not None:
        # Повтор чата стоит сообщения, повтор картинки — картинки: ключи
        # ограничителя разные, иначе зажатая кнопка обходила бы его.
        key = (
            _text_key(session) if repeat_kind is RetryKind.CHAT else _image_key(session)
        )
        await _guarded(
            deps,
            session,
            key,
            lambda d, s: repeat.repeat_last(d, s, repeat_kind),
        )
        return

    share_kind = _SHARE_KINDS.get(action)
    if share_kind is not None:
        await repeat.share_last(deps, session, share_kind)
        return

    if parse_buy_action(action) is not None:
        # Настоящая оплата — фаза 8. До неё честная заглушка с выходом.
        await tariffs.payments_not_ready(deps, session)
        return

    match action:
        case Action.MENU_IMAGES:
            await deps.storage.set_pending(session.user.id, pending.AWAIT_IMAGE_PROMPT)
            await images.ask_for_description(deps, session)
        case Action.MENU_PRESETS | Action.PRESET_ANOTHER:
            await _clear_pending(deps, session)
            await presets.show_menu(deps, session)
        case Action.MENU_PROFILE:
            await _clear_pending(deps, session)
            await profile.show(deps, session)
        case Action.MENU_TARIFFS | Action.OPEN_TARIFFS:
            await _clear_pending(deps, session)
            await tariffs.show(deps, session)
        case Action.CHAT_NEW_DIALOG:
            await _clear_pending(deps, session)
            await chat.start_new_dialog(deps, session)
        case Action.INVITE_FRIEND | Action.MY_LINK:
            await referral.show_offer(deps, session)
        case Action.REFERRAL_SEND:
            await referral.send_invitation(deps, session)
        case _:
            # Кнопка из версии, которой больше нет. Тупика быть не должно.
            deps.logger.warning("unknown_action", user_id=int(session.user.id))
            await _say(deps, session, texts.unsupported_input().text)


async def _pick_preset(deps: Deps, session: Session, preset_id: str) -> None:
    """Выбран прикол: запоминаем и просим фото."""
    preset = registry.PRESETS.get(preset_id)
    if preset is None:
        # Пресет убрали из реестра между версиями, а кнопка у человека в
        # переписке осталась. Показываем, что есть сейчас.
        await presets.show_menu(deps, session)
        return

    await deps.storage.set_pending(session.user.id, pending.await_preset(preset.id))
    await presets.ask_for_photo(deps, session, preset)


# --- Содержимое ----------------------------------------------------------


async def _handle_photo(deps: Deps, session: Session, photo_ref: str) -> None:
    """Пришло фото."""
    preset_id = pending.parse_await_preset(session.user.pending)
    preset = registry.PRESETS.get(preset_id) if preset_id is not None else None
    if preset is None:
        # Фото без выбранного прикола. Молчать нельзя, а угадывать нечего —
        # показываем, что с фото вообще можно сделать.
        await presets.show_menu(deps, session)
        return

    async def download_and_apply(d: Deps, s: Session) -> None:
        try:
            photo = await d.messenger.download_photo(
                photo_ref, max_bytes=d.settings.max_photo_bytes
            )
        except PhotoTooLargeError:
            # Размер известен до загрузки байтов, поэтому отказ бесплатный:
            # ни лимита, ни запроса к провайдеру (§3.5).
            await _say(d, s, texts.PHOTO_TOO_BIG)
            return
        await presets.apply(d, s, preset, photo, photo_ref)

    # Скачивание внутри ограничителя, а не до него: иначе десяток фото
    # подряд означал бы десяток закачек, из которых пригодится одна.
    #
    # Ожидание фото не сбрасываем: человек может прислать подряд несколько
    # снимков под тот же прикол, и переспрашивать на каждом было бы глупо.
    # Любое действие из меню и любой текст ожидание снимут.
    await _guarded(deps, session, _image_key(session), download_and_apply)


async def _handle_text(deps: Deps, session: Session, text: str) -> None:
    """Пришёл текст: описание картинки или реплика в чат.

    Ожидание снимается внутри ограничителя, а не до него. Разница
    существенная: если снять его снаружи и получить отказ «дождись
    предыдущего», человек останется и без картинки, и без режима — следующее
    сообщение уедет в чат, хотя он просил нарисовать.
    """
    if pending.is_awaiting_image_prompt(session.user.pending):

        async def draw(d: Deps, s: Session) -> None:
            # Режим одноразовый: следующее сообщение — снова обычный чат, а
            # «ещё раз» работает по кнопке, а не по режиму.
            await _clear_pending(d, s)
            await images.draw(d, s, text)

        await _guarded(deps, session, _image_key(session), draw)
        return

    async def reply(d: Deps, s: Session) -> None:
        # Ждали фото, а пришёл текст. Отправлять человека обратно за фото
        # значило бы запереть его: кнопки «в чат» в меню нет, и выйти из
        # режима было бы нечем. Поэтому ожидание снимаем и отвечаем как в чате.
        await _clear_pending(d, s)
        await chat.handle_message(d, s, text)

    await _guarded(deps, session, _text_key(session), reply)


# --- Вспомогательное -----------------------------------------------------

#: Сценарий, готовый к запуску под ограничителем.
Scenario = Callable[[Deps, Session], Awaitable[None]]


async def _guarded(deps: Deps, session: Session, key: str, scenario: Scenario) -> None:
    """Запускает сценарий, если у пользователя нет такой же работы в ходу.

    Ограничение существует не ради вежливости. Инвариант «списываем после
    доставки» оставляет окно между проверкой остатка и списанием, и два
    одновременных запроса одного человека могли бы пройти проверку оба.
    """
    if not deps.guard.try_acquire(key):
        await _say(deps, session, texts.still_working().text)
        return
    try:
        await scenario(deps, session)
    finally:
        deps.guard.release(key)


def _text_key(session: Session) -> str:
    return f"text:{int(session.user.id)}"


def _image_key(session: Session) -> str:
    return f"image:{int(session.user.id)}"


async def _clear_pending(deps: Deps, session: Session) -> None:
    """Снимает ожидание, если оно было. Лишний запрос в базу ни к чему."""
    if session.user.pending is not None:
        await deps.storage.set_pending(session.user.id, None)


async def _say(deps: Deps, session: Session, text: str) -> None:
    """Короткий ответ с постоянным меню под рукой."""
    # Клавиатуру не передаём: постоянное меню — это show_menu, и адаптер
    # рисует его своим способом. Передать сюда main_menu() значило бы в
    # Telegram превратить постоянное меню в кнопки под одним сообщением.
    await deps.messenger.send_text(session.chat, text, show_menu=True)


async def _answer_callback(deps: Deps, callback_id: str) -> None:
    """Подтверждает нажатие.

    Сбой подтверждения не должен отменять саму работу: у пользователя в
    худшем случае несколько секунд покрутится индикатор на кнопке, а вот
    потерять из-за этого ответ — уже настоящая потеря.
    """
    try:
        await deps.messenger.answer_callback(callback_id)
    except Exception as error:
        deps.logger.warning("callback_answer_failed", error=repr(error))
