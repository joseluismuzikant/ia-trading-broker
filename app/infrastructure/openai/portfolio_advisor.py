"""The OpenAI implementation of the portfolio-advice port.

One call, one portfolio. The advisor sends the whole account — holdings,
weights, indicators, watchlist, limits, warnings — and asks for one complete
target allocation back. It never asks per instrument, and it never lets the
model decide a quantity: the answer is weights and words, and deterministic
Python turns those into orders later.

Which model answers is configuration, not code: the caller passes one entry
from ``config/models.toml``. Provenance is stamped from that entry, not read
back from the model, so a saved proposal always says which provider, model,
and prompt version produced it.

When LangSmith tracing is enabled, one nested span wraps the model call. It
carries only normalised values (provider, model, version, counts) and the
already-sanitised request text; the client object and its API key are never
part of the traced call.
"""

from __future__ import annotations

from typing import Any

from app.config import Settings
from app.domain.analysis import PortfolioAdvice, PortfolioAnalysisContext
from app.domain.analysis_models import AnalysisModel
from app.infrastructure.openai.client import OpenAIClient
from app.infrastructure.openai.prompts import (
    ANALYSIS_PROMPT_VERSION,
    SYSTEM_PROMPT,
    user_message,
)
from app.tracing import span


def build_client(model: AnalysisModel, settings: Settings | None = None) -> Any:
    """The model client for one catalog entry: Claude natively, the rest by OpenAI protocol."""
    if model.api == "anthropic":
        from app.infrastructure.anthropic.client import AnthropicClient

        return AnthropicClient.from_model(model, settings)
    return OpenAIClient.from_model(model, settings)


class OpenAIPortfolioAdvisor:
    """Ask one configured model for a whole-portfolio target allocation."""

    def __init__(
        self,
        *,
        model: AnalysisModel,
        client: Any = None,
        settings: Settings | None = None,
    ) -> None:
        self.model = model
        self._client = client if client is not None else build_client(model, settings)

    async def analyze(self, context: PortfolioAnalysisContext) -> PortfolioAdvice:
        """Return one target allocation for the whole portfolio in ``context``."""
        request_text = user_message(context)
        trace_inputs = {
            "model_provider": self.model.provider,
            "model_name": self.model.model,
            "analysis_version": ANALYSIS_PROMPT_VERSION,
            "country": context.country,
            "number_of_positions": len(context.portfolio.positions),
            "number_of_watchlist_instruments": len(list(context.watchlist)),
            "request": request_text,
        }
        with span(
            "openai_portfolio_analysis",
            inputs=trace_inputs,
            metadata={
                "model_provider": self.model.provider,
                "model_name": self.model.model,
                "analysis_version": ANALYSIS_PROMPT_VERSION,
            },
            tags=["portfolio-analysis", self.model.provider],
        ) as run:
            payload = await self._client.complete_json(
                instructions=SYSTEM_PROMPT,
                input_text=request_text,
                schema=PortfolioAdvice.model_json_schema(),
                name="portfolio_advice",
            )
            advice = PortfolioAdvice.model_validate(payload)
            answer = advice.model_copy(
                update={
                    "model_provider": self.model.provider,
                    "model_name": self.model.model,
                    "analysis_version": ANALYSIS_PROMPT_VERSION,
                }
            )
            run.end(
                outputs={
                    "model_provider": self.model.provider,
                    "model_name": self.model.model,
                    "analysis_version": ANALYSIS_PROMPT_VERSION,
                    "number_of_decisions": len(answer.decisions),
                    "target_cash_weight": answer.target_cash_weight,
                    "response": payload,
                }
            )
            return answer
