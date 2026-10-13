"""Typed models for the IOL API responses used by the read-only pages.

Covers the profile and account status (Day 2) and the country portfolio and
quotes (Day 3). Each model mirrors the documented response shape and tolerates
unknown fields, so a broker-side addition never breaks a page.

IOL is inconsistent about scalar types: the same field can arrive as a number
in one account and as a string in another (``numero`` is documented as ``2``
but is sometimes ``"2"``). Text fields therefore use :data:`DisplayStr`, which
normalises numbers to strings in one place instead of at every call site.

Amounts are plain floats for now. Day 4 replaces them with exact money types
before any sizing or risk decision uses them.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


def _to_display_str(value: Any) -> Any:
    """Turn a scalar into text, leaving everything else for Pydantic to reject."""
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        # ``2`` and ``2.0`` both become "2" rather than "2.0".
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value)
    return value


#: A text field that also accepts numbers, normalised to a string.
DisplayStr = Annotated[str | None, BeforeValidator(_to_display_str)]


class IOLModel(BaseModel):
    """Base model that ignores extra fields and allows population by name."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)


class Token(IOLModel):
    """Response of ``POST /token``.

    The access token is held in memory only. It is never written to the
    database, the logs, or a template.
    """

    access_token: str = Field(repr=False)
    token_type: str = "bearer"
    expires_in: int = 0
    refresh_token: str | None = Field(default=None, repr=False)


class Profile(IOLModel):
    """Response of ``GET /api/v2/datos-perfil``."""

    nombre: DisplayStr = None
    apellido: DisplayStr = None
    numero_cuenta: DisplayStr = Field(default=None, alias="numeroCuenta")
    dni: DisplayStr = None
    sexo: DisplayStr = None
    perfil_inversor: DisplayStr = Field(default=None, alias="perfilInversor")

    @property
    def full_name(self) -> str:
        """First and last name joined, falling back to a neutral label."""
        parts = [part for part in (self.nombre, self.apellido) if part]
        return " ".join(parts) if parts else "Unknown"

    @property
    def masked_dni(self) -> str:
        """The national ID with everything but the last three digits hidden.

        The profile page shows this instead of the raw value so a screenshot or
        a shared screen does not leak the full document number.
        """
        if not self.dni:
            return "—"
        digits = self.dni.strip()
        if not digits:
            return "—"
        if len(digits) <= 3:
            return "•" * len(digits)
        return "•" * (len(digits) - 3) + digits[-3:]


class AccountBalance(IOLModel):
    """One settlement bucket inside an account."""

    liquidacion: DisplayStr = None
    saldo: float | None = None
    comprometido: float | None = None
    disponible: float | None = None
    disponible_operar: float | None = Field(default=None, alias="disponibleOperar")


class Account(IOLModel):
    """One account inside the account-status response."""

    numero: DisplayStr = None
    tipo: DisplayStr = None
    moneda: DisplayStr = None
    disponible: float | None = None
    comprometido: float | None = None
    saldo: float | None = None
    titulos_valorizados: float | None = Field(default=None, alias="titulosValorizados")
    total: float | None = None
    margen_descubierto: float | None = Field(default=None, alias="margenDescubierto")
    saldos: list[AccountBalance] = Field(default_factory=list)
    estado: DisplayStr = None


class AccountStatistic(IOLModel):
    """One row of the account-status statistics block."""

    descripcion: DisplayStr = None
    cantidad: float | None = None
    volumen: float | None = None


class AccountStatus(IOLModel):
    """Response of ``GET /api/v2/estadocuenta``."""

    cuentas: list[Account] = Field(default_factory=list)
    estadisticas: list[AccountStatistic] = Field(default_factory=list)
    total_en_pesos: float | None = Field(default=None, alias="totalEnPesos")


class IOLInstrument(IOLModel):
    """The ``titulo`` block inside one portfolio asset."""

    simbolo: DisplayStr = None
    descripcion: DisplayStr = None
    pais: DisplayStr = None
    mercado: DisplayStr = None
    tipo: DisplayStr = None
    plazo: DisplayStr = None
    moneda: DisplayStr = None


class Parking(IOLModel):
    """Settlement detail attached to a portfolio asset."""

    disponible_inmediato: float | None = Field(default=None, alias="disponibleInmediato")


class PortfolioAsset(IOLModel):
    """One holding inside ``GET /api/v2/portafolio/{pais}``."""

    cantidad: float | None = None
    comprometido: float | None = None
    puntos_variacion: float | None = Field(default=None, alias="puntosVariacion")
    variacion_diaria: float | None = Field(default=None, alias="variacionDiaria")
    ultimo_precio: float | None = Field(default=None, alias="ultimoPrecio")
    ppc: float | None = None
    ganancia_porcentaje: float | None = Field(default=None, alias="gananciaPorcentaje")
    ganancia_dinero: float | None = Field(default=None, alias="gananciaDinero")
    valorizado: float | None = None
    titulo: IOLInstrument | None = None
    parking: Parking | None = None


class IOLPortfolio(IOLModel):
    """Response of ``GET /api/v2/portafolio/{pais}``."""

    pais: DisplayStr = None
    activos: list[PortfolioAsset] = Field(default_factory=list)


class Quote(IOLModel):
    """One quote, from ``Cotizacion`` or one row of ``seriehistorica``."""

    ultimo_precio: float | None = Field(default=None, alias="ultimoPrecio")
    variacion: float | None = None
    apertura: float | None = None
    maximo: float | None = None
    minimo: float | None = None
    fecha_hora: DisplayStr = Field(default=None, alias="fechaHora")
    tendencia: DisplayStr = None
    cierre_anterior: float | None = Field(default=None, alias="cierreAnterior")
    monto_operado: float | None = Field(default=None, alias="montoOperado")
    volumen_nominal: float | None = Field(default=None, alias="volumenNominal")
    precio_promedio: float | None = Field(default=None, alias="precioPromedio")
    moneda: DisplayStr = None
