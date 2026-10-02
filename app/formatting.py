"""Display formatting for the pages.

Money and plain numbers are shown the way a local reader expects them: the
thousands separator is a dot and the decimal separator is a comma
(``99.895,16``), with the currency code after the amount when it is known
(``99.895,16 ARS``). Timestamps are shown in the application's local time with
its offset (``2026-10-02 11:42:06 UTC-3``) instead of the UTC the database
stores.

Nothing here makes a trading decision and nothing here rounds a value that is
used for one: these helpers only shape values for the screen and for printing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

from app.domain.money import Money

#: How money is shown: two decimals, dot for thousands, comma for decimals.
MONEY_PLACES = Decimal("0.01")

#: The display timezone. Argentina has kept a fixed UTC-3 offset for years; the
#: name is resolved when the platform has a timezone database so the printed
#: label always matches the real offset.
try:  # pragma: no cover - depends on the host's tz database
    from zoneinfo import ZoneInfo

    LOCAL_TIMEZONE = ZoneInfo("America/Argentina/Buenos_Aires")
except Exception:  # pragma: no cover - fallback for stripped-down systems
    LOCAL_TIMEZONE = timezone(timedelta(hours=-3))

#: Currency values reach here from different sources (the broker, the ledger,
#: saved payloads), so they are mapped to the short code a reader expects.
_CURRENCY_LABELS = {
    "peso_argentino": "ARS",
    "pesos": "ARS",
    "ars": "ARS",
    "ar$": "ARS",
    "dolar_estadounidense": "USD",
    "dolar": "USD",
    "usd": "USD",
    "us$": "USD",
}


def currency_label(value: object) -> str:
    """The short currency code for a stored currency value, or its own text.

    ``Peso_Argentino`` and ``AR$`` both become ``ARS``; an unknown currency is
    shown upper-cased rather than hidden.
    """
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    return _CURRENCY_LABELS.get(text.lower(), text.upper())


def format_money(value: object, currency: object = None) -> str:
    """One amount the way it is read locally: ``99.895,16 ARS``.

    ``currency`` is optional; without it the amount is shown without a code.
    A missing amount renders as an em dash, never as ``0``.
    """
    amount = _decimal(value)
    if amount is None:
        return "—"
    text = _localise(amount, MONEY_PLACES)
    label = currency_label(currency)
    return f"{text} {label}" if label else text


def format_number(value: object, places: int = 2) -> str:
    """A plain number with the same separators: ``12.345,678``."""
    amount = _decimal(value)
    if amount is None:
        return "—"
    return _localise(amount, Decimal(1).scaleb(-places) if places else Decimal(1))


def format_percent(value: object, places: int = 2) -> str:
    """A fraction as a percentage: ``15,00%``."""
    amount = _decimal(value)
    if amount is None:
        return "—"
    return _localise(amount * 100, Decimal(1).scaleb(-places) if places else Decimal(1)) + "%"


def format_datetime(value: object) -> str:
    """A timestamp in local time with its offset: ``2026-10-02 11:42:06 UTC-3``.

    Stored values are UTC; a value without a timezone is read as UTC, which is
    what the application writes. A missing timestamp renders as an em dash.
    """
    if value is None:
        return "—"
    if not isinstance(value, datetime):
        return "—"
    moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    local = moment.astimezone(LOCAL_TIMEZONE)
    return f"{local:%Y-%m-%d %H:%M:%S} {_offset_label(local)}"


def _offset_label(moment: datetime) -> str:
    """The UTC offset of one moment, written as a reader expects it."""
    offset = moment.utcoffset() or timedelta(0)
    total = int(offset.total_seconds())
    sign = "-" if total < 0 else "+"
    hours, remainder = divmod(abs(total), 3600)
    label = f"UTC{sign}{hours}"
    if remainder:
        label += f":{remainder // 60:02d}"
    return label


def _localise(amount: Decimal, quantum: Decimal) -> str:
    """Render a decimal with dot thousands and a comma decimal separator."""
    rounded = amount.quantize(quantum, rounding=ROUND_HALF_UP)
    sign = "-" if rounded < 0 else ""
    integer, _, fraction = format(abs(rounded), "f").partition(".")
    groups: list[str] = []
    while integer:
        groups.append(integer[-3:])
        integer = integer[:-3]
    text = ".".join(reversed(groups))
    if fraction:
        text = f"{text},{fraction}"
    return f"{sign}{text}"


def _decimal(value: object) -> Decimal | None:
    """A Decimal for anything the pages may hold, or None when there is none."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Money):
        return value.amount
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return Decimal(text)
        except Exception:  # noqa: BLE001 - unparseable text is simply absent
            return None
    return None
