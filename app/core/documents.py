"""Проверка присланных файлов до разбора (§3.5).

Устроено как проверка фото и по той же причине: заявленный тип приходит от
клиента и ничего не гарантирует. Но у файлов есть своя особенность — docx и
pptx внутри обычный zip, и по сигнатуре они неразличимы. Поэтому здесь два
шага: сигнатура отсекает чужие форматы, расширение выбирает между двумя
своими, а окончательно формат подтверждает разборщик, когда файл открывается
его библиотекой. Соврать расширением можно, но дальше разбора это не уедет.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.core.models import Document


class DocumentFormat(StrEnum):
    """Форматы, которые умеем читать."""

    DOCX = "docx"
    PDF = "pdf"
    PPTX = "pptx"


class DocumentProblem(StrEnum):
    """Почему файл не подходит."""

    TOO_BIG = "too_big"
    UNSUPPORTED_FORMAT = "unsupported_format"


class DocumentTooLargeError(Exception):
    """Мессенджер сообщил, что файл больше допустимого.

    Отдельное исключение, как и у фото: размер известен до загрузки байтов, и
    человеку надо сказать «пришли поменьше», а не «что-то пошло не так».
    """


@dataclass(frozen=True, slots=True)
class DocumentCheck:
    """Результат проверки."""

    problem: DocumentProblem | None
    document_format: DocumentFormat | None = None

    @property
    def ok(self) -> bool:
        return self.problem is None


#: Начало zip-архива. Внутри него лежат и docx, и pptx: оба — OOXML.
_ZIP = b"PK\x03\x04"

#: Начало PDF. Версия дальше бывает разная, поэтому сравниваем только это.
_PDF = b"%PDF-"

#: Расширение → формат. Только им и различаются два zip-овых формата.
_BY_EXTENSION: dict[str, DocumentFormat] = {
    ".docx": DocumentFormat.DOCX,
    ".pptx": DocumentFormat.PPTX,
    ".pdf": DocumentFormat.PDF,
}


def detect_format(document: Document) -> DocumentFormat | None:
    """Определяет формат; None — читать это мы не умеем.

    Расширение и сигнатура должны сойтись. Одного расширения мало: под именем
    ``доклад.pdf`` может приехать что угодно, и отдавать это чужой библиотеке
    мы не будем. Одной сигнатуры тоже мало: docx и pptx под ней одинаковы.
    """
    extension = _extension(document.filename)
    expected = _BY_EXTENSION.get(extension)
    if expected is None:
        return None

    if expected is DocumentFormat.PDF:
        return expected if document.data.startswith(_PDF) else None
    return expected if document.data.startswith(_ZIP) else None


def check_document(document: Document, *, max_bytes: int) -> DocumentCheck:
    """Можно ли этот файл разбирать.

    Размер проверяется первым: слишком большой файл не стоит и открывать,
    а человеку важнее узнать про размер, чем про формат.
    """
    if len(document.data) > max_bytes:
        return DocumentCheck(problem=DocumentProblem.TOO_BIG)

    found = detect_format(document)
    if found is None:
        return DocumentCheck(problem=DocumentProblem.UNSUPPORTED_FORMAT)
    return DocumentCheck(problem=None, document_format=found)


def _extension(filename: str) -> str:
    """Расширение в нижнем регистре, вместе с точкой. Нет — пустая строка."""
    _, dot, tail = filename.rpartition(".")
    return f".{tail.lower()}" if dot else ""
