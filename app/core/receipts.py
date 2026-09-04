"""Фискальный чек — то, чего требует 54-ФЗ, а не наш продукт.

Чек уходит вместе с платежом: ЮKassa передаёт его онлайн-кассе, касса
регистрирует и шлёт человеку на почту. Из всего, что мы знаем о покупателе,
доставить чек можно только на почту, поэтому она и спрашивается — один раз,
перед первой оплатой картой.

Про фискальные параметры. Ставка НДС, признак предмета расчёта и признак
способа расчёта зависят от системы налогообложения продавца, а не от кода.
Угадывать их нельзя: неверный чек — это не косметика, а неверный фискальный
документ, и отвечает за него продавец. Поэтому они приходят из настроек, где
их ставит тот, кто знает ответ, и меняются без выкладки — вместе со сменой
режима налогообложения, а не вместе с релизом.

Сумма чека обязана совпадать с суммой платежа. Здесь это выполняется само:
позиция ровно одна, количество единица, и сумма берётся из того же числа,
что уходит в amount.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Сколько позиций в нашем чеке. Одна: подписка на тариф — это одна услуга.
#: Число вынесено сюда не для настройки, а чтобы рядом стояло объяснение,
#: почему проверка «сумма чека равна сумме платежа» выполняется сама собой.
ITEMS_IN_RECEIPT = 1


@dataclass(frozen=True, slots=True)
class ReceiptItem:
    """Одна позиция чека."""

    description: str
    amount_rub: int
    currency: str
    vat_code: int
    payment_subject: str
    payment_mode: str


@dataclass(frozen=True, slots=True)
class Receipt:
    """Данные для формирования чека.

    Живёт в ядре и ничего не знает про ЮKassa: как это превратится в поля
    запроса — забота адаптера. Ядро отвечает только за то, чтобы чек был
    полным и совпадал с платежом.
    """

    email: str
    items: tuple[ReceiptItem, ...]

    def __post_init__(self) -> None:
        if not self.email:
            raise ValueError("чек некуда отправить: нет почты покупателя")
        if not self.items:
            raise ValueError("в чеке нет ни одной позиции")

    @property
    def total_rub(self) -> int:
        """Сумма чека. Обязана совпадать с суммой платежа."""
        return sum(item.amount_rub for item in self.items)


@dataclass(frozen=True, slots=True)
class FiscalSettings:
    """Фискальные параметры продавца.

    Не наш выбор и не выбор пользователя: их называет бухгалтер по системе
    налогообложения. Здесь они собраны в одном месте, чтобы сценарии не
    таскали четыре числа по отдельности.
    """

    vat_code: int
    payment_subject: str
    payment_mode: str
    #: Система налогообложения. Нужна только тем, у кого их несколько;
    #: у остальных пусто, и в чек поле не попадает вовсе.
    tax_system_code: int | None = None


def receipt_for(
    *,
    email: str,
    description: str,
    amount_rub: int,
    currency: str,
    fiscal: FiscalSettings,
) -> Receipt:
    """Чек на одну услугу — подписку на тариф."""
    return Receipt(
        email=email,
        items=(
            ReceiptItem(
                description=description,
                amount_rub=amount_rub,
                currency=currency,
                vat_code=fiscal.vat_code,
                payment_subject=fiscal.payment_subject,
                payment_mode=fiscal.payment_mode,
            ),
        ),
    )
