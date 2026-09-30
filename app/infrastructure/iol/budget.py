"""Monthly accounting for IOL API calls.

IOL is free up to a monthly quota and bills for every call past it, so the
adapter counts calls and refuses to send more once the budget is spent. The
cap is checked *before* a request goes out, so reaching the limit never costs
money; it only stops the work.

The counter lives in memory and rolls over at the start of each month (UTC). It
is therefore per process, so it is a safety net against a runaway loop rather
than an exact invoice. Persisting it is a hardening task.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from app.config import get_settings
from app.infrastructure.iol.errors import IOLBudgetExceededError

logger = logging.getLogger("ia_trading_broker.iol.budget")


@dataclass(frozen=True)
class CallUsage:
    """How many IOL calls this process has made in the current month."""

    period: str
    used: int
    limit: int

    @property
    def remaining(self) -> int | None:
        """Calls left before the cap, or None when there is no cap."""
        if self.limit <= 0:
            return None
        return max(self.limit - self.used, 0)

    @property
    def ratio(self) -> float:
        """Used fraction of the cap, or 0.0 when there is no cap."""
        if self.limit <= 0:
            return 0.0
        return self.used / self.limit


class CallBudget:
    """Count IOL calls and refuse them once the monthly cap is reached."""

    def __init__(self) -> None:
        self._period = _current_period()
        self._used = 0
        self._warned = False

    def check(self) -> None:
        """Raise when the cap is already reached, before any call is sent."""
        self._roll_over()
        limit = get_settings().iol_monthly_call_limit
        if limit > 0 and self._used >= limit:
            raise IOLBudgetExceededError(
                f"The monthly IOL API call limit ({limit}) is reached for "
                f"{self._period}. No call was made."
            )

    def record(self) -> None:
        """Count one call that is about to be sent."""
        self._roll_over()
        self._used += 1
        self._warn_if_needed()

    @property
    def usage(self) -> CallUsage:
        """Current usage for this process."""
        self._roll_over()
        return CallUsage(
            period=self._period,
            used=self._used,
            limit=get_settings().iol_monthly_call_limit,
        )

    def reset(self) -> None:
        """Forget all usage."""
        self._period = _current_period()
        self._used = 0
        self._warned = False

    # --- Internals --------------------------------------------------------

    def _roll_over(self) -> None:
        period = _current_period()
        if period != self._period:
            logger.info(
                "IOL call counter rolled over from %s to %s", self._period, period
            )
            self._period = period
            self._used = 0
            self._warned = False

    def _warn_if_needed(self) -> None:
        settings = get_settings()
        limit = settings.iol_monthly_call_limit
        if limit <= 0 or self._warned:
            return
        if self._used >= limit * settings.iol_call_warn_ratio:
            self._warned = True
            logger.warning(
                "IOL API usage has reached %s of the monthly limit (%s/%s). "
                "Past the free quota IOL bills for every call.",
                f"{self._used / limit:.0%}",
                self._used,
                limit,
            )


def _current_period() -> str:
    """The current month as ``YYYY-MM`` in UTC."""
    return datetime.now(UTC).strftime("%Y-%m")


#: Process-wide counter shared by every client instance.
call_budget = CallBudget()
