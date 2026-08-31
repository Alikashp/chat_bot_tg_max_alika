#!/usr/bin/env python
"""Готовит юридические документы к публикации на telegra.ph.

Задача не в красоте, а в том, чтобы опубликованная страница совпадала с
файлом из docs/legal. Вставить туда markdown нельзя: редактор telegra.ph
покажет решётки и звёздочки как есть. Зато он понимает форматированный текст
из буфера обмена — значит документ надо один раз показать браузеру, а дальше
copy-paste донесёт заголовки, списки и полужирный.

Поддерживается ровно та разметка, которая есть в наших документах: заголовки,
абзацы, списки, полужирный, разделители. Таблиц в них нет намеренно —
telegra.ph их не умеет.

Запуск: python scripts/legal_to_html.py
Результат: docs/legal/html/*.html — открыть в браузере, скопировать, вставить.
"""

from __future__ import annotations

import html
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "docs" / "legal"
TARGET = SOURCE / "html"

#: Полужирный. Внутри документов встречается только он.
_BOLD = re.compile(r"\*\*(.+?)\*\*")

_PAGE = """<!doctype html>
<meta charset="utf-8">
<title>{title}</title>
<style>
  body {{ font: 18px/1.6 Georgia, serif; max-width: 40em; margin: 2em auto;
          padding: 0 1em; }}
  .hint {{ background: #fffbe6; border: 1px solid #f0e0a0; padding: 1em;
           font-family: system-ui, sans-serif; font-size: 15px; }}
</style>
<div class="hint">
  Выделите всё ниже этой рамки (мышью или Ctrl+A), скопируйте и вставьте в
  telegra.ph. Заголовки, списки и выделения перенесутся сами. Саму рамку
  вставлять не нужно — если попала, удалите её в редакторе.
</div>
<hr>
{body}
"""


def to_html(markdown: str) -> tuple[str, str]:
    """Возвращает заголовок документа и его тело в HTML."""
    title = ""
    blocks: list[str] = []
    paragraph: list[str] = []
    items: list[str] = []

    def flush() -> None:
        if paragraph:
            blocks.append(f"<p>{' '.join(paragraph)}</p>")
            paragraph.clear()
        if items:
            blocks.append(
                "<ul>" + "".join(f"<li>{item}</li>" for item in items) + "</ul>"
            )
            items.clear()

    for raw in markdown.splitlines():
        line = raw.rstrip()
        if not line.strip():
            flush()
            continue
        if line.startswith("---"):
            flush()
            continue

        text = _inline(line.lstrip("#- ").strip())
        if line.startswith("# "):
            flush()
            title = text
            blocks.append(f"<h1>{text}</h1>")
        elif line.startswith("## "):
            flush()
            blocks.append(f"<h3>{text}</h3>")
        elif line.startswith("### "):
            flush()
            blocks.append(f"<h4>{text}</h4>")
        elif line.startswith("- "):
            if paragraph:
                blocks.append(f"<p>{' '.join(paragraph)}</p>")
                paragraph.clear()
            items.append(text)
        else:
            if items:
                blocks.append(
                    "<ul>" + "".join(f"<li>{item}</li>" for item in items) + "</ul>"
                )
                items.clear()
            paragraph.append(_inline(line.strip()))

    flush()
    return title or "Документ", "\n".join(blocks)


def _inline(text: str) -> str:
    """Экранирует HTML и разворачивает полужирный."""
    return _BOLD.sub(r"<b>\1</b>", html.escape(text))


def main() -> int:
    TARGET.mkdir(exist_ok=True)
    made = 0
    for source in sorted(SOURCE.glob("*.md")):
        if source.name == "README.md":
            continue
        title, body = to_html(source.read_text(encoding="utf-8"))
        page = TARGET / f"{source.stem}.html"
        page.write_text(_PAGE.format(title=title, body=body), encoding="utf-8")
        print(f"{source.name} → {page.relative_to(ROOT)}")
        made += 1
    if not made:
        print("нечего готовить: в docs/legal нет документов")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
