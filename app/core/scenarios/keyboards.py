"""Сборка клавиатур из подписей и действий.

Подписи берутся из core/texts.py, действия — из core/actions.py. Здесь они
соединяются в абстрактную клавиатуру, которую адаптер рисует по-своему:
в Telegram постоянным меню и inline-кнопками, в MAX — только inline
(постоянных клавиатур там нет, docs/research.md §1.6).

Слов «reply» и «inline» в этом файле нет и быть не должно.
"""

from __future__ import annotations

from app.core import texts
from app.core.actions import Action, buy_action, preset_action
from app.core.models import Button, Keyboard
from app.core.tariffs import PAID_TARIFFS

#: Постоянное меню из четырёх кнопок (§2.1).
MENU_ACTIONS: tuple[tuple[tuple[str, Action], ...], ...] = (
    (
        (texts.MENU_IMAGES, Action.MENU_IMAGES),
        (texts.MENU_PRESETS, Action.MENU_PRESETS),
    ),
    (
        (texts.MENU_PROFILE, Action.MENU_PROFILE),
        (texts.MENU_TARIFFS, Action.MENU_TARIFFS),
    ),
)


def main_menu() -> Keyboard:
    """Четвёрка кнопок, доступная с любого экрана."""
    return Keyboard(
        rows=tuple(
            tuple(Button(text=label, action=action) for label, action in row)
            for row in MENU_ACTIONS
        )
    )


def retry(action: Action) -> Keyboard:
    """Одна кнопка «Повторить»."""
    return Keyboard.row(Button(text=texts.BUTTON_RETRY, action=action))


def new_dialog() -> Keyboard:
    """Кнопка «🔄 Новый диалог» — появляется с десятого сообщения (§2.2)."""
    return Keyboard.row(
        Button(text=texts.BUTTON_NEW_DIALOG, action=Action.CHAT_NEW_DIALOG)
    )


def image_result() -> Keyboard:
    """Кнопки под нарисованной картинкой (§2.3)."""
    return Keyboard.row(
        Button(text=texts.BUTTON_DRAW_AGAIN, action=Action.IMAGE_AGAIN),
        Button(text=texts.BUTTON_SHARE, action=Action.IMAGE_SHARE),
    )


def preset_result() -> Keyboard:
    """Кнопки под обработанным фото (§2.4).

    Два ряда, а не один: три подписи в строку на телефоне не помещаются и
    обрезаются до «Отп…другу» и «Др…рикол». Лишний ряд дешевле обрезанной
    подписи — по ней не понять, что делает кнопка.
    """
    return Keyboard(
        rows=(
            (
                Button(text=texts.BUTTON_DRAW_AGAIN, action=Action.PRESET_AGAIN),
                Button(text=texts.BUTTON_SEND_TO_FRIEND, action=Action.PRESET_SHARE),
            ),
            (Button(text=texts.BUTTON_ANOTHER_PRESET, action=Action.PRESET_ANOTHER),),
        )
    )


def preset_cancel() -> Keyboard:
    """Отмена на шаге «пришли ещё одно фото» (§2.4).

    Действие то же, что у «Другого прикола»: и то и другое забывает
    собранные снимки и возвращает к списку. Отдельного действия здесь нет
    намеренно — оно вело бы ровно на тот же экран, а два имени у одного
    поведения расходятся на первой же правке. Разная у кнопок только
    подпись, и это как раз то, что человеку нужно: на середине сбора фото
    он ищет глазами «Отмена», а не «Другой прикол».
    """
    return Keyboard.row(Button(text=texts.BUTTON_CANCEL, action=Action.PRESET_ANOTHER))


def preset_locked() -> Keyboard:
    """Прикол под замком: тарифы и возврат к списку. Тупика быть не должно."""
    return Keyboard.row(
        Button(text=texts.BUTTON_OPEN_TARIFFS, action=Action.OPEN_TARIFFS),
        Button(text=texts.BUTTON_ANOTHER_PRESET, action=Action.PRESET_ANOTHER),
    )


def presets_menu(presets: tuple[tuple[str, str], ...]) -> Keyboard:
    """Меню приколов. На вход — пары «подпись, идентификатор» из реестра.

    Клавиатура строится из реестра, поэтому третий пресет появляется здесь
    сам, без единой правки (критерий приёмки A1).
    """
    return Keyboard(
        rows=tuple(
            (Button(text=label, action=preset_action(preset_id)),)
            for label, preset_id in presets
        )
    )


def paywall(invite_label: str) -> Keyboard:
    """Два выхода с экрана исчерпания (§2.5). Тупика быть не должно."""
    return Keyboard(
        rows=(
            (Button(text=texts.BUTTON_OPEN_TARIFFS, action=Action.OPEN_TARIFFS),),
            (Button(text=invite_label, action=Action.INVITE_FRIEND),),
        )
    )


def profile(*, has_subscription: bool = False) -> Keyboard:
    """Выходы из профиля (§2.6).

    Кнопка подписки появляется только у того, у кого подписка есть. Держать
    её всегда значило бы уводить бесплатного человека на экран, который
    сообщит ему, что смотреть нечего.

    Отдельным рядом: третья подпись в строке на телефоне обрезается.
    """
    rows: list[tuple[Button, ...]] = [
        (
            Button(text=texts.MENU_TARIFFS, action=Action.OPEN_TARIFFS),
            Button(text=texts.BUTTON_MY_LINK, action=Action.MY_LINK),
        )
    ]
    if has_subscription:
        rows.append(
            (Button(text=texts.BUTTON_SUBSCRIPTION, action=Action.SUBSCRIPTION),)
        )
    return Keyboard(rows=tuple(rows))


def subscription_manage() -> Keyboard:
    """Экран подписки, которую есть смысл отключать."""
    return Keyboard.row(
        Button(text=texts.BUTTON_SUBSCRIPTION_OFF, action=Action.SUBSCRIPTION_OFF),
        Button(text=texts.MENU_PROFILE, action=Action.MENU_PROFILE),
    )


def tariffs_or_profile() -> Keyboard:
    """Выход с экранов, где отключать уже нечего."""
    return Keyboard.row(
        Button(text=texts.MENU_TARIFFS, action=Action.OPEN_TARIFFS),
        Button(text=texts.MENU_PROFILE, action=Action.MENU_PROFILE),
    )


def open_tariffs() -> Keyboard:
    """Одна кнопка — в тарифы. Ставится там, где других выходов нет."""
    return Keyboard.row(
        Button(text=texts.BUTTON_OPEN_TARIFFS, action=Action.OPEN_TARIFFS)
    )


def tariffs() -> Keyboard:
    """Три кнопки выбора под витриной тарифов (§2.8).

    В один ряд: подписи короткие, а выбор из трёх должен читаться как выбор
    из трёх, а не как три отдельных предложения.
    """
    return Keyboard.row(
        *(
            Button(
                text=texts.choose_button(tariff_id),
                action=buy_action(tariff_id.value),
            )
            for tariff_id in PAID_TARIFFS
        )
    )


def referral_offer() -> Keyboard:
    """Одна кнопка: отправить другу готовое приглашение (§2.7)."""
    return Keyboard.row(
        Button(text=texts.BUTTON_SEND_TO_FRIEND, action=Action.REFERRAL_SEND)
    )


def payments_soon() -> Keyboard:
    """Заглушка оплаты до фазы 8 — но выход с экрана есть."""
    return Keyboard.row(
        Button(text=texts.BUTTON_MY_LINK, action=Action.MY_LINK),
        Button(text=texts.MENU_PROFILE, action=Action.MENU_PROFILE),
    )


#: Подпись кнопки постоянного меню → действие.
#:
#: Нужно там, где мессенджер возвращает нажатие текстом, а не данными кнопки:
#: в Telegram постоянная клавиатура присылает ровно подпись. Собирается из
#: MENU_ACTIONS, поэтому расходиться с самой клавиатурой не может.
_MENU_BY_LABEL: dict[str, Action] = {
    label: action for row in MENU_ACTIONS for label, action in row
}


def action_for_label(label: str | None) -> str | None:
    """Действие по подписи кнопки меню; None — если это обычный текст."""
    if label is None:
        return None
    return _MENU_BY_LABEL.get(label.strip())
