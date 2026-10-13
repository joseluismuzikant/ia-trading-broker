"""Tests for live execution: the real IOL path, and every guard around it.

These tests run the execution service against the fake IOL transport. They pin
the safety rules: the fail-closed preflight,
sells before buys, accepted-but-not-filled semantics, no paper-ledger mutation
from live results, and never resending an order that may already have been
sent. No test can reach a real broker.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.domain.money import money
from app.domain.trading import Order, OrderResult
from app.infrastructure.simulation import advice_from_targets
from app.services import analysis as analysis_service
from app.services import events as events_service
from app.services import execution as execution_service
from app.services import ledger as ledger_service
from app.services import orders as orders_service
from app.services import rebalance as rebalance_service
from tests.day5_helpers import make_connection

PRICES = {"GGAL": money("60.00"), "VIST": money("100.00"), "YPFD": money("1200.00")}


def _enable_live(monkeypatch, **overrides) -> None:
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "iol_live_plazo", overrides.get("plazo", "t2"))
    monkeypatch.setattr(settings, "iol_live_validity_days", overrides.get("validity_days", 1))


async def _live_proposal(db, user, connection):
    """A saved LIVE proposal: exit GGAL and buy VIST, sells first."""
    from app.domain.trading import PaperLedger, PaperPosition

    account = PaperLedger(
        country="argentina",
        currency="Peso_Argentino",
        cash=money("10000.00"),
        positions=[
            PaperPosition(
                symbol="GGAL",
                quantity=Decimal("100"),
                average_price=money("60.00"),
                last_price=money("60.00"),
                market="BCBA",
            )
        ],
    )
    await ledger_service.save_ledger(db, user_id=user.id, connection=connection, ledger=account)
    advice = advice_from_targets(
        {"GGAL": 0.0, "VIST": 0.20},
        cash=0.80,
        current={"GGAL": 0.375},
    )
    plan = rebalance_service.plan_rebalance(
        advice=advice,
        ledger=account,
        universe=analysis_service.load_config(),
        prices=PRICES,
    )
    proposal = analysis_service.build_rebalance_proposal(
        plan=plan,
        advice_view=_advice_view(advice),
        evidence={},
    )
    return await analysis_service.save_proposal(
        db, user_id=user.id, connection_id=connection.id, proposal=proposal
    )


async def _record_sale(db, user, connection, record, *, status="accepted") -> None:
    """Save the plan's GGAL sale as already submitted. No broker is involved."""
    sale = Order(
        symbol="GGAL",
        side="SELL",
        quantity=Decimal("100"),
        limit_price=money("60.00"),
        notional=money("6000.00"),
        market="BCBA",
        status=status,
        execution_mode="LIVE",
        broker_order_id="900001" if status == "accepted" else None,
    )
    await orders_service.prepare_order(
        db,
        user_id=user.id,
        connection_id=connection.id,
        proposal_id=record.id,
        country="argentina",
        order=sale,
    )


def _advice_view(advice):
    """A flat advice view for the proposal, bypassing the model context."""
    from app.domain.trading import AssetAdviceView, PortfolioAdviceView

    return PortfolioAdviceView(
        summary=advice.summary,
        target_cash_weight=advice.target_cash_weight,
        current_cash_weight=0.03,
        model_label="Fake simulator (smoke test)",
        model_provider=advice.model_provider,
        model_name=advice.model_name,
        analysis_version=advice.analysis_version,
        decisions=[
            AssetAdviceView(
                symbol=item.symbol,
                action=item.action,
                target_weight=item.target_weight,
                current_weight=0.375 if item.symbol == "GGAL" else 0.0,
                confidence=item.confidence,
                rationale=item.rationale,
                risks=list(item.risks),
            )
            for item in advice.decisions
        ],
    )


class RecordingExecutor:
    """An executor that accepts everything and remembers the calls."""

    def __init__(self) -> None:
        self.calls: list = []

    async def submit(self, order) -> OrderResult:
        self.calls.append(order)
        return OrderResult(
            status="accepted",
            accepted=True,
            broker_order_id="9001",
            message="accepted by the test executor",
        )


# --- The preflight -------------------------------------------


async def test_live_without_trading_terms_refuses_the_whole_submission(
    db_session, make_user, fake_iol
) -> None:
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)
    record = await _live_proposal(db_session, user, connection)

    with pytest.raises(execution_service.ExecutionError, match="IOL_LIVE_PLAZO is not configured"):
        await execution_service.submit_proposal_orders(
            db_session, user_id=user.id, connection=connection, proposal_record=record
        )
    assert fake_iol.trade_posts == []


async def test_the_preflight_refuses_a_missing_settlement_term(
    db_session, make_user, fake_iol, monkeypatch
) -> None:
    _enable_live(monkeypatch, plazo="")
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)
    record = await _live_proposal(db_session, user, connection)

    with pytest.raises(execution_service.ExecutionError, match="IOL_LIVE_PLAZO"):
        await execution_service.submit_proposal_orders(
            db_session, user_id=user.id, connection=connection, proposal_record=record
        )
    assert fake_iol.trade_posts == []


async def test_the_preflight_refuses_selling_more_than_was_reviewed(
    db_session, make_user, fake_iol, monkeypatch
) -> None:
    _enable_live(monkeypatch)
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)
    record = await _live_proposal(db_session, user, connection)
    payload = dict(record.payload)
    payload["recommendations"][0]["quantity"] = "9999"
    payload["recommendations"][0]["notional"] = "599940.00"
    record.payload = payload
    await db_session.commit()

    with pytest.raises(execution_service.ExecutionError, match="cannot sell"):
        await execution_service.submit_proposal_orders(
            db_session, user_id=user.id, connection=connection, proposal_record=record
        )
    assert fake_iol.trade_posts == []


async def test_the_preflight_refuses_a_buy_the_reviewed_cash_cannot_fund(
    db_session, make_user, fake_iol, monkeypatch
) -> None:
    _enable_live(monkeypatch)
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)
    record = await _live_proposal(db_session, user, connection)
    payload = dict(record.payload)
    for item in payload["recommendations"]:
        if item["action"] == "BUY":
            item["quantity"] = "100000"
            item["notional"] = "10000000.00"
    record.payload = payload
    await db_session.commit()

    with pytest.raises(execution_service.ExecutionError, match="fund"):
        await execution_service.submit_proposal_orders(
            db_session, user_id=user.id, connection=connection, proposal_record=record
        )
    assert fake_iol.trade_posts == []


# --- What a live submission does ------------------------------------------


async def test_a_live_order_is_accepted_not_filled_and_keeps_the_broker_id(
    db_session, make_user, fake_iol, monkeypatch
) -> None:
    _enable_live(monkeypatch)
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)
    record = await _live_proposal(db_session, user, connection)

    report = await execution_service.submit_proposal_orders(
        db_session, user_id=user.id, connection=connection, proposal_record=record
    )

    assert report.accepted >= 1
    orders = await orders_service.list_orders_for_proposal(
        db_session, user_id=user.id, proposal_id=record.id
    )
    for order in orders:
        assert order.status == "accepted"
        assert order.broker_order_id
        assert order.payload.get("filled_quantity") is None
        assert order.payload.get("status") == "accepted"


async def test_live_execution_never_mutates_the_paper_ledger(
    db_session, make_user, fake_iol, monkeypatch
) -> None:
    _enable_live(monkeypatch)
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)
    record = await _live_proposal(db_session, user, connection)
    before = await ledger_service.load_ledger(
        db_session, user_id=user.id, connection=connection, country="argentina"
    )

    await execution_service.submit_proposal_orders(
        db_session, user_id=user.id, connection=connection, proposal_record=record
    )

    after = await ledger_service.load_ledger(
        db_session, user_id=user.id, connection=connection, country="argentina"
    )
    assert before is not None and after is not None
    assert after.cash == before.cash
    assert {p.symbol: p.quantity for p in after.positions} == {
        p.symbol: p.quantity for p in before.positions
    }


async def test_sells_are_submitted_before_buys(
    db_session, make_user, fake_iol, monkeypatch
) -> None:
    _enable_live(monkeypatch)
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)
    record = await _live_proposal(db_session, user, connection)

    await execution_service.submit_proposal_orders(
        db_session, user_id=user.id, connection=connection, proposal_record=record
    )

    paths = [path for path, _ in fake_iol.trade_posts]
    assert paths == ["/api/v2/operar/Vender", "/api/v2/operar/Comprar"]


def test_execution_order_puts_sells_first() -> None:
    orders = [
        Order(
            symbol="YPFD",
            side="BUY",
            quantity=Decimal("1"),
            limit_price=money("1200.00"),
            notional=money("1200.00"),
            market="BCBA",
        ),
        Order(
            symbol="GGAL",
            side="SELL",
            quantity=Decimal("1"),
            limit_price=money("60.00"),
            notional=money("60.00"),
            market="BCBA",
        ),
        Order(
            symbol="AL30",
            side="BUY",
            quantity=Decimal("1"),
            limit_price=money("100.00"),
            notional=money("100.00"),
            market="BCBA",
        ),
    ]

    ordered = execution_service.execution_order(orders)

    assert [(o.side, o.symbol) for o in ordered] == [
        ("SELL", "GGAL"),
        ("BUY", "AL30"),
        ("BUY", "YPFD"),
    ]


# --- Exactly-once behaviour ----------------------------------------------


async def test_orders_are_saved_before_the_broker_sees_them(
    db_session, make_user, fake_iol, monkeypatch
) -> None:
    _enable_live(monkeypatch)
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)
    record = await _live_proposal(db_session, user, connection)
    # The first POST times out; the row must already exist as unknown.
    fake_iol.trade_responses.append({"raise": "timeout"})

    await execution_service.submit_proposal_orders(
        db_session, user_id=user.id, connection=connection, proposal_record=record
    )

    orders = await orders_service.list_orders_for_proposal(
        db_session, user_id=user.id, proposal_id=record.id
    )
    assert orders and orders[0].status == "unknown"
    assert orders[0].idempotency_key.startswith(f"proposal:{record.id}:")


async def test_an_existing_order_row_is_never_resent(
    db_session, make_user, fake_iol, monkeypatch
) -> None:
    _enable_live(monkeypatch)
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)
    record = await _live_proposal(db_session, user, connection)
    # An earlier attempt left an unknown row behind.
    await orders_service.prepare_order(
        db_session,
        user_id=user.id,
        connection_id=connection.id,
        proposal_id=record.id,
        country="argentina",
        order=Order(
            symbol="GGAL",
            side="SELL",
            quantity=Decimal("100"),
            limit_price=money("60.00"),
            notional=money("6000.00"),
            market="BCBA",
            status="unknown",
            execution_mode="LIVE",
        ),
    )

    report = await execution_service.submit_proposal_orders(
        db_session, user_id=user.id, connection=connection, proposal_record=record
    )

    assert report.skipped >= 1
    assert report.unknown >= 1
    assert fake_iol.trade_posts == []


async def test_the_event_log_records_the_live_order_state(
    db_session, make_user, fake_iol, monkeypatch
) -> None:
    _enable_live(monkeypatch)
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)
    record = await _live_proposal(db_session, user, connection)

    await execution_service.submit_proposal_orders(
        db_session, user_id=user.id, connection=connection, proposal_record=record
    )

    events = await events_service.list_events_for_proposal(
        db_session, user_id=user.id, proposal_id=record.id
    )
    types = {event.event_type for event in events}
    assert "order.prepared" in types
    assert "order.accepted" in types


# --- Reconciliation -------------------------------------------------------


async def test_a_submitted_order_can_be_reconciled_with_the_broker(
    db_session, make_user, fake_iol, monkeypatch
) -> None:
    _enable_live(monkeypatch)
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)
    record = await _live_proposal(db_session, user, connection)
    fake_iol.trade_responses.append({"status": 201, "json": {"numeroOperacion": 777}})
    await execution_service.submit_proposal_orders(
        db_session, user_id=user.id, connection=connection, proposal_record=record
    )
    orders = await orders_service.list_orders_for_proposal(
        db_session, user_id=user.id, proposal_id=record.id
    )
    first = orders[0]
    fake_iol.operations["777"] = {
        "numero": 777,
        "estadoActual": "Operada",
        "fechaOperado": "2026-10-04T15:00:00",
        "cantidadOperada": 100,
        "precioOperado": 60.0,
    }

    updated = await execution_service.reconcile_live_order(
        db_session, user_id=user.id, connection=connection, record=first
    )

    assert updated.status == "filled"
    assert updated.broker_order_id == "777"


async def test_reconciliation_records_the_broker_price_not_the_limit(
    db_session, make_user, fake_iol, monkeypatch
) -> None:
    _enable_live(monkeypatch)
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)
    record = await _live_proposal(db_session, user, connection)
    fake_iol.trade_responses.append({"status": 201, "json": {"numeroOperacion": 501}})
    await execution_service.submit_proposal_orders(
        db_session,
        user_id=user.id,
        connection=connection,
        proposal_record=record,
        side="SELL",
    )
    orders = await orders_service.list_orders_for_proposal(
        db_session, user_id=user.id, proposal_id=record.id
    )
    first = orders[0]
    assert first.broker_order_id == "501"
    fake_iol.operations["501"] = {
        "numero": 501,
        "estadoActual": "Operada",
        "fechaOperado": "2026-10-07T15:00:00",
        "cantidadOperada": 100,
        "precioOperado": 61.75,
    }

    updated = await execution_service.reconcile_live_order(
        db_session, user_id=user.id, connection=connection, record=first
    )

    assert updated.status == "filled"
    assert Decimal(updated.payload["filled_price"]) == Decimal("61.75")
    assert Decimal(updated.payload["filled_quantity"]) == Decimal("100")
    # The requested limit price is never presented as an execution price.
    assert updated.payload["filled_price"] != updated.payload["limit_price"]


async def test_a_partial_fill_stays_partial_and_blocks_deletion(
    db_session, make_user, fake_iol, monkeypatch
) -> None:
    _enable_live(monkeypatch)
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)
    record = await _live_proposal(db_session, user, connection)
    fake_iol.trade_responses.append({"status": 201, "json": {"numeroOperacion": 502}})
    await execution_service.submit_proposal_orders(
        db_session,
        user_id=user.id,
        connection=connection,
        proposal_record=record,
        side="SELL",
    )
    orders = await orders_service.list_orders_for_proposal(
        db_session, user_id=user.id, proposal_id=record.id
    )
    first = orders[0]
    fake_iol.operations["502"] = {
        "numero": 502,
        "estadoActual": "Parcialmente_Operada",
        "fechaOperado": "2026-10-07T15:00:00",
        "cantidadOperada": 40,
        "precioOperado": 61.0,
    }

    counts = await execution_service.reconcile_proposal_orders(
        db_session, user_id=user.id, connection=connection, proposal_record=record
    )
    updated = await orders_service.get_order(db_session, user_id=user.id, order_id=first.id)

    assert counts["partial"] >= 1
    assert updated.status == "partially_filled"
    assert Decimal(updated.payload["filled_quantity"]) == Decimal("40")
    from app.services.analysis import ProposalDeleteError, delete_proposal

    with pytest.raises(ProposalDeleteError):
        await delete_proposal(db_session, user_id=user.id, proposal_id=record.id)


async def test_the_purchases_phase_never_spends_more_than_the_broker_reports(
    db_session, make_user, fake_iol, monkeypatch
) -> None:
    _enable_live(monkeypatch)
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)
    record = await _live_proposal(db_session, user, connection)
    await _record_sale(db_session, user, connection, record)
    from app.domain.money import money as to_money

    report = await execution_service.submit_proposal_orders(
        db_session,
        user_id=user.id,
        connection=connection,
        proposal_record=record,
        side="BUY",
        cash_limit=to_money("100.00"),
    )

    # The VIST buy costs far more than 100 pesos: it is deferred, not sent.
    assert report.deferred == ["VIST"]
    assert report.submitted == 0
    assert fake_iol.trade_posts == []


# --- Quotation factor and unverified bond quantity semantics --------------


def _bond_recommendation(*, factor: int = 100, executable: bool = True):
    from app.domain.trading import IndicatorEvidence, Recommendation

    return Recommendation(
        symbol="AL30",
        name="Bonar 2030",
        action="BUY",
        quantity=Decimal("17"),
        limit_price=money("85800.00"),
        notional=money("14586.00"),
        target_weight=0.15,
        market="BCBA",
        executable=executable,
        quotation_factor=factor,
        rationale="test",
        evidence=IndicatorEvidence(),
    )


async def _save_bond_proposal(db, user, connection, *, factor: int = 100, executable=True):
    from app.domain.trading import PortfolioAdviceView, Proposal

    advice = PortfolioAdviceView(
        summary="test",
        target_cash_weight=0.85,
        current_cash_weight=1.0,
        model_label="test",
        model_provider="test",
        model_name="test",
        analysis_version="test-1",
        decisions=[],
    )
    proposal = Proposal(
        country="argentina",
        execution_mode="LIVE",
        portfolio_value=money("100000.00"),
        cash=money("100000.00"),
        advice=advice,
        recommendations=[_bond_recommendation(factor=factor, executable=executable)],
        summary="test",
    )
    return await analysis_service.save_proposal(
        db, user_id=user.id, connection_id=connection.id, proposal=proposal
    )


async def test_a_bond_order_is_blocked_while_quantity_semantics_are_unverified(
    db_session, make_user, fake_iol, monkeypatch
) -> None:
    from app.domain.universe import Instrument, PortfolioConstraints, UniverseConfig

    _enable_live(monkeypatch)
    unverified = UniverseConfig(
        instruments=(
            Instrument(
                "AL30",
                "Bonar 2030",
                "bonds",
                quotation_factor=100,
                live_quantity_confirmed=False,
            ),
        ),
        constraints=PortfolioConstraints(),
    )
    monkeypatch.setattr("app.services.analysis.load_config", lambda: unverified)
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)
    record = await _save_bond_proposal(db_session, user, connection)

    with pytest.raises(execution_service.ExecutionError, match="unverified"):
        await execution_service.submit_proposal_orders(
            db_session,
            user_id=user.id,
            connection=connection,
            proposal_record=record,
            side="BUY",
            cash_limit=money("20000.00"),
        )
    assert fake_iol.trade_posts == []


async def test_a_quotation_factor_mismatch_is_blocked(
    db_session, make_user, fake_iol, monkeypatch
) -> None:
    _enable_live(monkeypatch)
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)
    # The plan was sized as if a bond quote were per 1, which it is not.
    record = await _save_bond_proposal(db_session, user, connection, factor=1)

    with pytest.raises(execution_service.ExecutionError, match="quotation factor"):
        await execution_service.submit_proposal_orders(
            db_session,
            user_id=user.id,
            connection=connection,
            proposal_record=record,
            side="BUY",
            cash_limit=money("20000.00"),
        )
    assert fake_iol.trade_posts == []


async def test_a_confirmed_bond_category_submits_the_quoted_price_unchanged(
    db_session, make_user, fake_iol, monkeypatch
) -> None:
    from app.domain.universe import Instrument, PortfolioConstraints, UniverseConfig

    _enable_live(monkeypatch)
    confirmed = UniverseConfig(
        instruments=(
            Instrument(
                "AL30",
                "Bonar 2030",
                "bonds",
                quotation_factor=100,
                live_quantity_confirmed=True,
            ),
        ),
        constraints=PortfolioConstraints(),
    )
    monkeypatch.setattr("app.services.analysis.load_config", lambda: confirmed)
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)
    record = await _save_bond_proposal(db_session, user, connection)

    report = await execution_service.submit_proposal_orders(
        db_session,
        user_id=user.id,
        connection=connection,
        proposal_record=record,
        side="BUY",
        cash_limit=money("20000.00"),
    )

    assert report.accepted == 1
    path, body = fake_iol.trade_posts[0]
    assert path == "/api/v2/operar/Comprar"
    # The quoted price is sent unchanged; only the sizing divided by 100.
    assert body["precio"] == "85800.00"
    assert body["cantidad"] == "17"


# --- Regression: purchases wait for sales, quantities stay inside the plan ----


async def test_a_purchase_is_refused_until_the_sales_are_submitted(
    db_session, make_user, fake_iol, monkeypatch
) -> None:
    _enable_live(monkeypatch)
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)
    record = await _live_proposal(db_session, user, connection)

    with pytest.raises(execution_service.ExecutionError, match="sales before any purchase"):
        await execution_service.submit_proposal_orders(
            db_session,
            user_id=user.id,
            connection=connection,
            proposal_record=record,
            side="BUY",
        )

    assert fake_iol.trade_posts == []


async def test_a_single_purchase_is_refused_until_the_sales_are_submitted(
    db_session, make_user, fake_iol, monkeypatch
) -> None:
    _enable_live(monkeypatch)
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)
    record = await _live_proposal(db_session, user, connection)

    with pytest.raises(execution_service.ExecutionError, match="sales before any purchase"):
        await execution_service.execute_single_order(
            db_session,
            user_id=user.id,
            connection=connection,
            proposal_record=record,
            symbol="VIST",
            side="BUY",
            units=Decimal("1"),
        )

    assert fake_iol.trade_posts == []


async def test_a_rejected_sale_does_not_unlock_the_purchases(
    db_session, make_user, fake_iol, monkeypatch
) -> None:
    _enable_live(monkeypatch)
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)
    record = await _live_proposal(db_session, user, connection)
    await _record_sale(db_session, user, connection, record, status="rejected")

    with pytest.raises(execution_service.ExecutionError, match="sales before any purchase"):
        await execution_service.submit_proposal_orders(
            db_session,
            user_id=user.id,
            connection=connection,
            proposal_record=record,
            side="BUY",
        )


async def test_a_single_trade_cannot_exceed_the_reviewed_quantity(
    db_session, make_user, fake_iol, monkeypatch
) -> None:
    _enable_live(monkeypatch)
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)
    record = await _live_proposal(db_session, user, connection)

    # The plan sells all 100 GGAL; 101 is more than the human approved.
    with pytest.raises(execution_service.ExecutionError, match="more than the 100"):
        await execution_service.execute_single_order(
            db_session,
            user_id=user.id,
            connection=connection,
            proposal_record=record,
            symbol="GGAL",
            side="SELL",
            units=Decimal("101"),
        )

    assert fake_iol.trade_posts == []
    assert (
        await orders_service.list_orders_for_proposal(
            db_session, user_id=user.id, proposal_id=record.id
        )
        == []
    )


async def test_a_single_purchase_cannot_exceed_the_reviewed_quantity(
    db_session, make_user, fake_iol, monkeypatch
) -> None:
    from app.domain.trading import Proposal

    _enable_live(monkeypatch)
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)
    record = await _live_proposal(db_session, user, connection)
    await _record_sale(db_session, user, connection, record)
    planned = next(
        item for item in Proposal.model_validate(record.payload).orders if item.symbol == "VIST"
    )

    with pytest.raises(execution_service.ExecutionError, match="more than the"):
        await execution_service.execute_single_order(
            db_session,
            user_id=user.id,
            connection=connection,
            proposal_record=record,
            symbol="VIST",
            side="BUY",
            units=planned.quantity + 1,
        )

    assert fake_iol.trade_posts == []


async def test_reconcile_reports_an_order_that_cannot_be_looked_up(
    db_session, make_user, fake_iol, monkeypatch
) -> None:
    _enable_live(monkeypatch)
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)
    record = await _live_proposal(db_session, user, connection)
    # A timeout left an order with no operation number: IOL cannot be asked.
    await _record_sale(db_session, user, connection, record, status="unknown")

    counts = await execution_service.reconcile_proposal_orders(
        db_session, user_id=user.id, connection=connection, proposal_record=record
    )

    assert counts["unresolved"] == 1
    assert fake_iol.trade_posts == []
    # And it is still blocked from being sent again.
    attempt, allowed, _reason = await orders_service.next_attempt(
        db_session, user_id=user.id, proposal_id=record.id, symbol="GGAL", side="SELL"
    )
    assert allowed is False


async def test_two_requests_racing_for_the_same_order_send_it_once(
    db_session, make_user, fake_iol, monkeypatch
) -> None:
    _enable_live(monkeypatch)
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)
    record = await _live_proposal(db_session, user, connection)
    await _record_sale(db_session, user, connection, record)

    # The second request does not see the first one's row, as in a real race,
    # so it reaches the insert and the unique key must stop it.
    real_get = orders_service.get_by_key
    calls = {"n": 0}

    async def blind_once(db, *, user_id, key):
        calls["n"] += 1
        return None if calls["n"] == 1 else await real_get(db, user_id=user_id, key=key)

    monkeypatch.setattr(orders_service, "get_by_key", blind_once)

    duplicate = Order(
        symbol="GGAL",
        side="SELL",
        quantity=Decimal("100"),
        limit_price=money("60.00"),
        notional=money("6000.00"),
        market="BCBA",
        execution_mode="LIVE",
    )
    saved, created = await orders_service.prepare_order(
        db_session,
        user_id=user.id,
        connection_id=connection.id,
        proposal_id=record.id,
        country="argentina",
        order=duplicate,
    )

    assert created is False
    assert saved.status == "accepted"
