"""Чтение примеров к приколам с диска.

Проверяется главным образом то, что отсутствие картинки — не поломка:
добавление шестого прикола не должно требовать сперва нарисовать к нему
пример, иначе реестр перестанет быть точкой расширения (критерий A1).
"""

from __future__ import annotations

from pathlib import Path

from app.infra.examples import load_examples
from tests.fakes import PNG_BYTES, FakeLogger

JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 64


def test_examples_are_found_by_the_preset_identifier(tmp_path: Path) -> None:
    (tmp_path / "lego.jpg").write_bytes(JPEG_BYTES)
    (tmp_path / "figurine.png").write_bytes(PNG_BYTES)

    found = load_examples(("lego", "figurine"), logger=FakeLogger(), directory=tmp_path)

    assert set(found) == {"lego", "figurine"}
    assert found["lego"].mime_type == "image/jpeg"
    assert found["figurine"].mime_type == "image/png"


def test_a_missing_picture_is_not_a_failure(tmp_path: Path) -> None:
    """Иначе новый прикол пришлось бы ждать вместе с картинкой к нему."""
    (tmp_path / "lego.jpg").write_bytes(JPEG_BYTES)

    found = load_examples(("lego", "ghost"), logger=FakeLogger(), directory=tmp_path)

    assert set(found) == {"lego"}


def test_an_empty_directory_gives_an_empty_result(tmp_path: Path) -> None:
    assert load_examples(("lego",), logger=FakeLogger(), directory=tmp_path) == {}


def test_a_file_that_is_not_a_picture_is_refused_loudly(tmp_path: Path) -> None:
    """Расширение врёт, сигнатура — нет. А в мессенджер уедет именно картинка."""
    (tmp_path / "lego.jpg").write_text("я не картинка")
    logger = FakeLogger()

    found = load_examples(("lego",), logger=logger, directory=tmp_path)

    assert found == {}
    assert [event.event for event in logger.events if event.level == "error"] == [
        "preset_example_not_an_image"
    ]


def test_the_name_of_the_file_travels_with_the_picture(tmp_path: Path) -> None:
    """По имени адаптер узнаёт уже загруженную картинку и не льёт её заново."""
    (tmp_path / "lego.jpg").write_bytes(JPEG_BYTES)

    found = load_examples(("lego",), logger=FakeLogger(), directory=tmp_path)

    assert found["lego"].filename == "lego.jpg"
