"""The simulated portfolio advisor: a valid answer with no network.

A catalog entry with ``kind = "fake"`` builds this instead of the OpenAI one.
It answers with the portfolio it is shown — every held symbol at its current
weight, all actions ``HOLD``, cash where it already is — which is always a
complete, self-financing allocation. A smoke test can therefore run the whole
analysis flow without any provider key, and a proposal saved from it records
plainly that a simulator, not a model, produced it.

Provenance is stamped exactly like the OpenAI advisor: from the catalog entry,
never invented here. Handed a specific ``payload`` it returns that answer, and
handed an ``error`` it fails; those knobs exist so tests can script it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from app.domain.analysis import (
    AssetAdvice,
    PortfolioAdvice,
    PortfolioAnalysisContext,
    expected_action,
)
from app.domain.analysis_models import AnalysisModel

#: Version recorded with every allocation this simulator builds.
SIMULATOR_VERSION = "simulator-1"


@dataclass
class FakePortfolioAdvisor:
    """A portfolio advisor that answers without reaching a network.

    By default it keeps the portfolio it is shown: every held symbol at its
    current weight, all actions ``HOLD``, cash where it already is. That is
    always a valid, self-financing allocation. Tests that want a different
    answer hand one in, and tests that want a failure set ``error``.
    """

    model: AnalysisModel | None = None
    payload: PortfolioAdvice | None = None
    error: Exception | None = None
    contexts: list = field(default_factory=list)

    async def analyze(self, context: PortfolioAnalysisContext) -> PortfolioAdvice:
        self.contexts.append(context)
        if self.error is not None:
            raise self.error
        answer = self.payload if self.payload is not None else keep_portfolio(context)
        if self.model is None:
            return answer
        return answer.model_copy(
            update={
                "model_provider": self.model.provider,
                "model_name": self.model.model,
                "analysis_version": SIMULATOR_VERSION,
            }
        )


def keep_portfolio(context: PortfolioAnalysisContext) -> PortfolioAdvice:
    """A target allocation that changes as little as possible.

    Keeping an account exactly as it stands is only a valid answer while the
    account satisfies its own constraints, so cash is raised to the floor when
    it sits below it and the positions are scaled down together to pay for it.
    """
    floor = float(context.constraints.min_cash_pct)
    cash_weight = context.cash_weight or 0.0
    target_cash = max(cash_weight, floor)
    scale = 1.0
    if cash_weight < 1.0:
        scale = (1.0 - target_cash) / (1.0 - cash_weight)
    targets = {symbol: context.weight_of(symbol) * scale for symbol in sorted(context.held_symbols)}
    moved = abs(target_cash - cash_weight) > 1e-9
    return advice_from_targets(
        targets,
        cash=target_cash,
        current=context.current_weights,
        summary=(
            "Kept as it is (test advice)."
            if not moved
            else "Kept, with cash raised to the floor (test advice)."
        ),
    )


def advice_from_targets(
    targets: Mapping[str, float],
    *,
    cash: float,
    current: Mapping[str, float] | None = None,
    summary: str = "A test allocation.",
    confidence: float = 0.9,
) -> PortfolioAdvice:
    """Build advice for target weights, with actions that match the moves.

    ``targets`` maps a symbol to the weight it should hold and ``current`` to
    the weight it holds today; a symbol missing from ``current`` starts at
    zero. The action is derived from the move, so the answer is consistent by
    construction and the validator accepts it.
    """
    today = dict(current or {})
    decisions = [
        AssetAdvice(
            symbol=symbol,
            action=expected_action(target, today.get(symbol, 0.0)),
            target_weight=target,
            confidence=confidence,
            rationale="A test allocation.",
            risks=[],
        )
        for symbol, target in targets.items()
    ]
    return PortfolioAdvice(
        summary=summary,
        target_cash_weight=cash,
        decisions=decisions,
        model_provider="fake",
        model_name="test",
        analysis_version="test-1",
    )
