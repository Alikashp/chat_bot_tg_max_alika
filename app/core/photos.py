"""Проверка присланных фото до отправки провайдеру (§3.5).

Проверяем по сигнатуре файла, а не по заявленному типу: заявленный тип
приходит от клиента и ничего не гарантирует. Отправить провайдеру что попало
под видом картинки — это как минимум оплаченный впустую запрос, а как
максимум разбор чужого формата чужим кодом.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.core.models import Photo


class PhotoProblem(StrEnum):
    """Почему фото не подходит."""

    TOO_BIG = "too_big"
    NOT_AN_IMAGE = "not_an_image"


class PhotoTooLargeError(Exception):
    """Мессенджер сообщил, что файл больше допустимого.

    Отдельное исключение, а не общий сбой: адаптер узнаёт размер до загрузки
    байтов (§3.5) и должен уметь сказать об этом так, чтобы маршрутизатор
    показал пользователю «пришли поменьше», а не «что-то пошло не так».
    """


@dataclass(frozen=True, slots=True)
class PhotoCheck:
    """Результат проверки."""

    problem: PhotoProblem | None

    @property
    def ok(self) -> bool:
        return self.problem is None


#: Сигнатуры форматов, которые принимаем. Больше и не нужно: мессенджеры
#: присылают фотографии именно в этих форматах.
_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)

#: WEBP опознаётся по двум кускам: RIFF в начале и WEBP через четыре байта
#: размера.
_RIFF = b"RIFF"
_WEBP = b"WEBP"


def detect_mime(data: bytes) -> str | None:
    """Определяет тип по сигнатуре; None — если это не картинка."""
    for signature, mime in _SIGNATURES:
        if data.startswith(signature):
            return mime
    if len(data) >= 12 and data.startswith(_RIFF) and data[8:12] == _WEBP:
        return "image/webp"
    return None


def check_photo(photo: Photo, *, max_bytes: int) -> PhotoCheck:
    """Проверяет размер и формат.

    Порядок проверок важен: размер сначала. Определять формат у файла на
    сотню мегабайт бессмысленно, если мы всё равно его не примем.
    """
    if len(photo.data) > max_bytes:
        return PhotoCheck(problem=PhotoProblem.TOO_BIG)
    if detect_mime(photo.data) is None:
        return PhotoCheck(problem=PhotoProblem.NOT_AN_IMAGE)
    return PhotoCheck(problem=None)
