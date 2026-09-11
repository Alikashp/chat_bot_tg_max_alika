"""Запись обращений к провайдерам — сбоку от продукта.

Отдельный модуль ровно ради одного правила: **учёт не смеет ломать ответ
человеку**. Провайдер отработал, картинка нарисована, а база в этот момент
может быть недоступна — и человек всё равно обязан получить результат. Поэтому
запись здесь обёрнута в перехват: падение уходит в лог и на этом кончается.

Перехват широкий намеренно. Учёт — не бизнес-правило, и никакой его сбой не
стоит того, чтобы пользователь увидел «что-то пошло не так» вместо готовой
картинки, за которую уже заплачено провайдеру.
"""

from __future__ import annotations

from datetime import datetime

from app.core.generations import (
    Generation,
    GenerationKind,
    GenerationStatus,
    elapsed_ms,
    error_code,
)
from app.core.scenarios.deps import Deps, Session


async def record_success(
    deps: Deps,
    session: Session,
    kind: GenerationKind,
    *,
    started: datetime,
    model: str,
    preset_id: str | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
) -> None:
    """Записывает удавшееся обращение."""
    await _write(
        deps,
        Generation(
            user_id=session.user.id,
            kind=kind,
            model=model,
            status=GenerationStatus.SUCCESS,
            duration_ms=elapsed_ms(started, deps.now()),
            preset_id=preset_id,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        ),
    )


async def record_failure(
    deps: Deps,
    session: Session,
    kind: GenerationKind,
    *,
    started: datetime,
    model: str,
    error: BaseException,
    preset_id: str | None = None,
) -> None:
    """Записывает упавшее обращение.

    Упавшие пишутся наравне с удачными: провайдер берёт деньги за попытку,
    и без этих строк доля брака видна только по счёту в конце месяца.
    """
    await _write(
        deps,
        Generation(
            user_id=session.user.id,
            kind=kind,
            model=model,
            status=GenerationStatus.FAILED,
            duration_ms=elapsed_ms(started, deps.now()),
            preset_id=preset_id,
            error_code=error_code(error),
        ),
    )


async def _write(deps: Deps, generation: Generation) -> None:
    try:
        await deps.storage.record_generation(generation)
    except Exception as error:
        deps.logger.warning(
            "generation_not_recorded",
            user_id=int(generation.user_id),
            kind=generation.kind.value,
            error=repr(error),
        )
