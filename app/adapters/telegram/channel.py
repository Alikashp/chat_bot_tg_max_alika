"""Проверка подписки на канал через Telegram.

Единственный вызов — getChatMember. Работает он только тогда, когда бот
добавлен в канал администратором: иначе Telegram отвечает отказом, и это не
ошибка в коде, а незаконченная настройка канала.

Отсюда главное решение этого файла: отказ Telegram — это «проверить не
удалось», а не «человек не подписан». Подписанного участника Telegram
описывает статусом, а не ошибкой; ошибка почти всегда означает, что спросить
нам не дали. Свалив одно в другое, бот сообщал бы подписавшимся людям, что
подписки не видит, — и делал бы это тем убедительнее, чем хуже настроен канал.
"""

from __future__ import annotations

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest

from app.ports.observability import Logger

#: Статусы, при которых человек считается подписанным.
#:
#: RESTRICTED сюда не входит по значению статуса: у ограниченного участника
#: есть отдельный признак is_member, и он же решает, в канале человек или его
#: уже выгнали. Разбирается ниже отдельно.
_SUBSCRIBED = frozenset(
    {
        ChatMemberStatus.CREATOR,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.MEMBER,
    }
)

#: Единственный отказ, который значит «такого человека нет», а не «нам не
#: дали посмотреть». Всё остальное — «chat not found», «member list is
#: inaccessible», «not enough rights» — говорит о правах бота в канале.
_NO_SUCH_USER = "user not found"


class TelegramChannel:
    """Порт Channel поверх getChatMember."""

    def __init__(self, bot: Bot, channel: str, logger: Logger) -> None:
        self._bot = bot
        self._channel = channel
        self._logger = logger

    async def has_member(self, external_user_id: str) -> bool:
        """Подписан ли человек на канал.

        Бросает, когда проверить не удалось: сценарий отличает этот случай от
        отказа и предлагает попробовать ещё раз, а в логе остаётся причина —
        по ней видно, что боту не хватает прав администратора канала.
        """
        try:
            member = await self._bot.get_chat_member(
                chat_id=self._channel, user_id=int(external_user_id)
            )
        except TelegramBadRequest as error:
            if _NO_SUCH_USER in str(error).lower():
                return False
            self._logger.warning(
                "channel_check_refused",
                channel=self._channel,
                error=repr(error),
            )
            raise

        if member.status == ChatMemberStatus.RESTRICTED:
            # У ограниченного участника членство в канале — отдельное поле:
            # он может быть и в канале, и уже выгнанным из него.
            return bool(getattr(member, "is_member", False))
        return member.status in _SUBSCRIBED
