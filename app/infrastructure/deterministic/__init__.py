"""Deterministic analysis adapters.

These need no credentials and make no network call, so the funnel runs and can
be tested offline. They are documented placeholders, not a second opinion: a
news or language-model adapter replaces them behind the same port.
"""

from app.infrastructure.deterministic.fundamentals import DeterministicFundamentals

__all__ = ["DeterministicFundamentals"]
