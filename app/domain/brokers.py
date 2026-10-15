"""What each broker needs to connect, and what it can do.

One table, read by the connection form (which fields to ask for), by the
connection service (what to validate and encrypt) and by the workflow (whether a
broker has an account-status page or can send a real order). Adding a broker
starts here.

Nothing in this module is secret: it names fields, never holds a value.
"""

from __future__ import annotations

from dataclasses import dataclass

IOL = "iol"
PPI = "ppi"
PAPER = "paper"


@dataclass(frozen=True)
class CredentialField:
    """One value a broker needs to log in."""

    name: str
    label: str
    #: Secret fields are rendered as password inputs and never shown again.
    secret: bool = False
    hint: str = ""
    max_length: int = 200


@dataclass(frozen=True)
class BrokerKind:
    """A broker the application can hold a connection for."""

    key: str
    label: str
    fields: tuple[CredentialField, ...]
    #: The field whose value is shown as the connection's login name.
    display_field: str = ""
    #: True when the broker has IOL-style account-status and profile pages.
    has_account_status: bool = False
    #: True when a real order can be sent through this broker.
    can_trade: bool = False
    #: True when the connection is simulated and sends nothing anywhere.
    simulated: bool = False
    description: str = ""


BROKERS: dict[str, BrokerKind] = {
    IOL: BrokerKind(
        key=IOL,
        label="InvertirOnline (IOL)",
        fields=(
            CredentialField("username", "IOL username", max_length=120),
            CredentialField("password", "IOL password", secret=True),
        ),
        display_field="username",
        has_account_status=True,
        can_trade=True,
        description="Read your IOL account and, once you approve, send orders to it.",
    ),
    PPI: BrokerKind(
        key=PPI,
        label="PPI (experimental)",
        fields=(
            CredentialField("account_number", "Account number", max_length=60),
            CredentialField("api_key", "Public API key", secret=True),
            CredentialField("api_secret", "Private API key", secret=True),
        ),
        display_field="account_number",
        description=(
            "Experimental. Can log in and read cash and prices. It cannot read "
            "positions or send orders yet."
        ),
    ),
    PAPER: BrokerKind(
        key=PAPER,
        label="Paper account (simulated)",
        fields=(),
        simulated=True,
        description=(
            "A simulated account: same review and approval, no real money, no broker "
            "orders. It can copy another account's holdings."
        ),
    ),
}


def kind_of(broker: str) -> BrokerKind | None:
    """The definition of a broker, or None for one the application does not know."""
    return BROKERS.get(broker)


def is_simulated(broker: str) -> bool:
    kind = kind_of(broker)
    return bool(kind and kind.simulated)
