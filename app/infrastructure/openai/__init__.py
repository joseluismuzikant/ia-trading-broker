"""The OpenAI adapter for portfolio advice.

It is the only place in the application that talks to a model. Everything else
goes through :class:`~app.ports.portfolio_advisor.PortfolioAdvisor`, so the
advisor can be replaced without touching the workflow, the validation, or the
risk rules.
"""

from app.infrastructure.openai.client import OpenAIClient, OpenAIClientError
from app.infrastructure.openai.portfolio_advisor import OpenAIPortfolioAdvisor

__all__ = ["OpenAIClient", "OpenAIClientError", "OpenAIPortfolioAdvisor"]
