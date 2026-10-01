"""Simulated execution adapters.

The paper adapter fills orders locally against the saved paper ledger. It needs
no credentials and makes no network call, so the whole Day 5 flow can run and be
tested offline.
"""

from app.infrastructure.simulation.paper import PaperOrderExecutor

__all__ = ["PaperOrderExecutor"]
