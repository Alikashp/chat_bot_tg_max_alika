"""Учёт обращений к провайдерам ИИ.

Каждая попытка — чат, картинка по описанию, прикол с фото — записывается
отдельной строкой: и удавшаяся, и упавшая. Упавшие тут не для полноты
коллекции. Провайдер берёт деньги за попытку, а не за успех, и без строк с
отказами доля брака видна только по счёту в конце месяца.

Запись идёт сразу после ответа провайдера, а не после доставки результата
человеку. Это намеренно расходится с правилом списания лимита (core/scenarios/
spending.py): лимит не списывается за недоставленное, потому что человек его
не получил, — но деньги за такой вызов уже уплачены, и не записать его значит
потерять расход.

Содержимого запросов и ответов здесь нет и быть не должно (§3.5): ни текста
сообщения, ни описания картинки. Только идентификатор человека, вид работы,
модель, исход и числа.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from app.core.models import UserId

#: Потолок длины кода ошибки. Код — это имя класса исключения, а не его
#: сообщение: сообщение несёт текст запроса, которому в базе не место.
MAX_ERROR_CODE = 64


class GenerationKind(StrEnum):
    """Какую работу просили сделать."""

    CHAT = "chat"
    IMAGE = "image"
    PRESET = "preset"


class GenerationStatus(StrEnum):
    """Чем кончилось обращение к провайдеру."""

    SUCCESS = "success"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Generation:
    """Одно обращение к провайдеру.

    ``tokens_in``/``tokens_out`` пусты там, где провайдер их не называет:
    у картинок счёт идёт за саму отрисовку, а не за токены, и выдумывать
    нули вместо «неизвестно» значило бы испортить любую будущую сумму.
    """

    user_id: UserId
    kind: GenerationKind
    model: str
    status: GenerationStatus
    duration_ms: int
    #: Идентификатор прикола из реестра. Пусто у чата и картинки по описанию.
    preset_id: str | None = None
    #: Почему не вышло. Пусто при успехе.
    error_code: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None

    @property
    def succeeded(self) -> bool:
        return self.status is GenerationStatus.SUCCESS


def error_code(error: BaseException) -> str:
    """Короткий код ошибки для записи.

    Имя класса, а не текст: текст исключения у провайдеров часто содержит
    присланный запрос целиком, а содержимого сообщений мы не храним (§3.5).
    По имени класса при этом видно главное — таймаут это, отказ по
    содержанию или неожиданный ответ.
    """
    return type(error).__name__[:MAX_ERROR_CODE] or "Error"


def elapsed_ms(started: datetime, finished: datetime) -> int:
    """Сколько длилось обращение, в миллисекундах.

    Отрицательным не бывает: часы могут прыгнуть назад, а отрицательная
    длительность в отчёте выглядит как ошибка выгрузки, а не как перевод
    времени.
    """
    return max(0, int((finished - started).total_seconds() * 1000))
