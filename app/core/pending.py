"""Чего бот ждёт от пользователя следующим сообщением.

Нажал «🎨 Картинки» — дальше любое сообщение считается описанием картинки, а
не вопросом в чат. Выбрал прикол — дальше ждём фото. Это состояние диалога, и
живёт оно в ядре, а не в механизмах конкретного мессенджера: правило одинаково
для Telegram и MAX, а FSM у aiogram — вещь в себе, которая в MAX не поедет.

Хранится в базе, а не в памяти процесса: иначе выкатка посреди разговора
превращает «Опиши, что нарисовать» в потерянный вопрос — человек пишет
описание, а бот отвечает на него как на реплику в чате.

У приколов, которым нужно два фото, ожидание несёт ещё и то, что уже прислали:
между первым снимком и вторым проходит отдельное обращение, и помнить первый
больше негде.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Ждём описание будущей картинки (§2.3).
AWAIT_IMAGE_PROMPT = "await:image"

#: Ждём фото под выбранный прикол (§2.4). За префиксом — идентификатор пресета
#: и ссылки на уже присланные фото.
_AWAIT_PRESET_PREFIX = "await:preset:"

#: Ждём почту для фискального чека. За префиксом — тариф, за которым человек
#: шёл: спросив адрес, надо вернуть его туда же, а не в начало витрины.
_AWAIT_EMAIL_PREFIX = "await:email:"

#: Чем разделены части.
#:
#: Перевод строки, а не двоеточие и не вертикальная черта: ссылка на фото в
#: MAX — это обычный http-адрес, в котором есть и двоеточие, и косые черты, а
#: file_id в Telegram — base64url. Символа перевода строки не бывает ни в том,
#: ни в другом, поэтому разбор однозначен при любой ссылке.
_SEPARATOR = "\n"

#: Чем порядковый ключ отделён от ссылки внутри одной записи.
#:
#: Двоеточие в ссылках встречается (в MAX это http-адрес), поэтому делим по
#: первому вхождению: слева всегда только цифры, и разбор однозначен.
_ORDER_MARK = ":"


@dataclass(frozen=True, slots=True)
class CollectedPhoto:
    """Присланный снимок: чем мессенджер его упорядочил и как его забрать.

    Порядок хранится рядом со ссылкой, потому что по нему потом сортируют:
    два снимка, отправленные разом, разбираются параллельно, и дойти до базы
    они могут в любом порядке. А инструкция провайдеру ссылается на них по
    номерам — переставленные местами лица дают другую картинку.
    """

    order: int
    ref: str


@dataclass(frozen=True, slots=True)
class AwaitedPreset:
    """Прикол, под который ждём фото, и то, что уже прислали."""

    preset_id: str
    #: Присланные снимки в порядке записи.
    collected: tuple[CollectedPhoto, ...] = ()

    @property
    def refs(self) -> tuple[str, ...]:
        """Ссылки в том порядке, в каком их отправлял человек.

        Сортировка устойчивая: снимки, про которые мессенджер ничего не
        сказал (порядок 0), остаются в порядке записи.
        """
        return tuple(photo.ref for photo in sorted(self.collected, key=_by_order))


def _by_order(photo: CollectedPhoto) -> int:
    return photo.order


def await_preset(preset_id: str, collected: tuple[CollectedPhoto, ...] = ()) -> str:
    """Состояние «ждём фото под такой-то прикол»."""
    if not preset_id:
        raise ValueError("нужен идентификатор пресета")
    if _SEPARATOR in preset_id or any(_SEPARATOR in each.ref for each in collected):
        raise ValueError("перевода строки не должно быть ни в id, ни в ссылке")
    entries = tuple(f"{each.order}{_ORDER_MARK}{each.ref}" for each in collected)
    return _AWAIT_PRESET_PREFIX + _SEPARATOR.join((preset_id, *entries))


def parse_await_preset(pending: str | None) -> AwaitedPreset | None:
    """Разбирает ожидание фото; None — если ждём не его.

    Испорченное значение — не повод падать: оно могло остаться от прошлой
    версии формата, и тогда честнее считать, что ожидания нет, чем гадать.
    """
    if pending is None or not pending.startswith(_AWAIT_PRESET_PREFIX):
        return None

    preset_id, *entries = pending.removeprefix(_AWAIT_PRESET_PREFIX).split(_SEPARATOR)
    if not preset_id or not all(entries):
        return None
    return AwaitedPreset(
        preset_id=preset_id, collected=tuple(_entry(each) for each in entries)
    )


def _entry(entry: str) -> CollectedPhoto:
    """Разбирает запись «порядок:ссылка».

    Запись без числа впереди — это формат прошлой версии, оставшийся у тех,
    кто прислал первое фото до выкладки. Такой снимок не теряем: считаем, что
    порядка мессенджер не назвал, и оставляем его там, где он записан.
    """
    order, mark, ref = entry.partition(_ORDER_MARK)
    if not mark or not ref or not order.isdigit():
        return CollectedPhoto(order=0, ref=entry)
    return CollectedPhoto(order=int(order), ref=ref)


def await_email(tariff_id: str) -> str:
    """Состояние «ждём почту, чтобы вернуться к оплате такого-то тарифа»."""
    if not tariff_id:
        raise ValueError("нужен идентификатор тарифа")
    return f"{_AWAIT_EMAIL_PREFIX}{tariff_id}"


def parse_await_email(pending: str | None) -> str | None:
    """Возвращает тариф, к оплате которого вернуться после почты."""
    if pending is None or not pending.startswith(_AWAIT_EMAIL_PREFIX):
        return None
    return pending.removeprefix(_AWAIT_EMAIL_PREFIX) or None


def is_awaiting_image_prompt(pending: str | None) -> bool:
    """Ждём ли описание картинки."""
    return pending == AWAIT_IMAGE_PROMPT
