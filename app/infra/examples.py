"""Примеры к приколам — картинки, по которым видно, что получится.

Кнопка «🧸 Фигурка в коробке» ничего не объясняет тому, кто такой фигурки
не видел. Поэтому меню приколов открывается альбомом примеров, и человек
выбирает по картинке, а не по догадке.

Файлы лежат в репозитории и читаются один раз на старте: их пять, они не
меняются между выкатками, и ходить за ними на диск при каждом открытии меню
незачем. Отсутствие файла — не ошибка: прикол просто останется без примера,
и меню покажет его как раньше. Иначе добавление шестого прикола требовало бы
сперва нарисовать к нему картинку, а это ровно та связанность, которой в
реестре пресетов быть не должно (критерий приёмки A1).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from app.core.models import Photo
from app.core.photos import detect_mime
from app.ports.observability import Logger

#: Где лежат примеры. Имя файла — идентификатор прикола из реестра.
EXAMPLES_DIR = Path("assets/presets")

#: Какие расширения ищем. Порядок важен: JPEG для фотографий, PNG — если
#: картинку сделали со скриншота.
_SUFFIXES = (".jpg", ".jpeg", ".png")


def load_examples(
    preset_ids: tuple[str, ...],
    *,
    logger: Logger,
    directory: Path = EXAMPLES_DIR,
) -> Mapping[str, Photo]:
    """Читает примеры к приколам. Каких нет — тех нет, и это не ошибка.

    Формат проверяется по сигнатуре, а не по расширению: файл с расширением
    .jpg внутри может оказаться чем угодно, а уехать он должен в мессенджер
    как картинка.
    """
    found: dict[str, Photo] = {}
    for preset_id in preset_ids:
        path = _find(directory, preset_id)
        if path is None:
            continue

        data = path.read_bytes()
        mime = detect_mime(data)
        if mime is None:
            # Молчать нельзя: картинка есть, но в мессенджер она не уедет, и
            # человек увидит меню без примера, не понимая почему.
            logger.error("preset_example_not_an_image", preset=preset_id)
            continue

        found[preset_id] = Photo(data=data, mime_type=mime, filename=path.name)

    logger.info("preset_examples_loaded", count=len(found))
    return found


def _find(directory: Path, preset_id: str) -> Path | None:
    for suffix in _SUFFIXES:
        path = directory / f"{preset_id}{suffix}"
        if path.is_file():
            return path
    return None
