"""Реферальные коды и ссылки.

Код должен быть коротким (его пересылают в сообщении и читают глазами) и при
этом неугадываемым: подобранный чужой код позволил бы накручивать награды.
Поэтому secrets, а не random.
"""

from __future__ import annotations

import secrets
import string

#: Алфавит без похожих друг на друга символов: 0/O и 1/l/I в пересланной
#: ссылке путаются, а ссылку иногда набирают руками.
_ALPHABET = "".join(
    ch for ch in string.ascii_lowercase + string.digits if ch not in "01lio"
)

#: Восемь символов из 31 — это около 10^12 вариантов. Перебором не берётся,
#: в ссылке выглядит коротко.
CODE_LENGTH = 8

#: Префиксы deeplink (§2.1, §2.7).
REFERRAL_PREFIX = "ref_"
PRESENTATION_PREFIX = "pres_"

#: Где живёт бот. Форма ссылки у Telegram и MAX совпадает до буквы —
#: «<хост>/<имя бота>?start=<payload>», — различается только хост
#: (docs/research.md §1.5). Поэтому хост приходит параметром, а не зашит.
TELEGRAM_HOST = "https://t.me"
MAX_HOST = "https://max.ru"


def generate_code() -> str:
    """Новый реферальный код."""
    return "".join(secrets.choice(_ALPHABET) for _ in range(CODE_LENGTH))


def referral_payload(code: str) -> str:
    """Что подставляется в ссылку после ?start=."""
    return f"{REFERRAL_PREFIX}{code}"


def referral_url(host: str, bot_username: str, code: str) -> str:
    """Персональная ссылка пользователя.

    Хост приходит снаружи: у пользователя MAX ссылка обязана вести в MAX, а
    не в Telegram, иначе приглашение просто не откроется.
    """
    return f"{host.rstrip('/')}/{bot_username}?start={referral_payload(code)}"


def parse_referral_payload(payload: str) -> str | None:
    """Достаёт код из deeplink вида ref_XXXX; None — если это не он."""
    if not payload.startswith(REFERRAL_PREFIX):
        return None
    return payload.removeprefix(REFERRAL_PREFIX) or None


def is_from_presentations(payload: str) -> bool:
    """Пришёл ли пользователь из бота презентаций (§2.1)."""
    return payload.startswith(PRESENTATION_PREFIX)
