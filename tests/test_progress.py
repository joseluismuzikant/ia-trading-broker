"""Tests for the progress helpers behind the proposal bar and the dashboard."""

from __future__ import annotations

from app.services.progress import CURRENT, DONE, SKIPPED, STOPPED, TODO, next_action, proposal_steps


def steps(**overrides):
    base = {
        "approval_status": "pending",
        "has_sells": True,
        "has_buys": True,
        "sells_submitted": False,
        "buys_submitted": False,
        "any_submitted": False,
        "pending_orders": 0,
    }
    base.update(overrides)
    return {step.label: step.state for step in proposal_steps(**base)}


def test_a_fresh_proposal_waits_for_the_decision() -> None:
    assert steps() == {"Approve": CURRENT, "Sales": TODO, "Purchases": TODO, "Settle": TODO}


def test_an_approved_proposal_starts_with_the_sales() -> None:
    state = steps(approval_status="approved")
    assert state["Approve"] == DONE
    assert state["Sales"] == CURRENT
    assert state["Purchases"] == TODO


def test_purchases_open_after_the_sales_are_submitted() -> None:
    state = steps(
        approval_status="approved", sells_submitted=True, any_submitted=True, pending_orders=1
    )
    assert state["Sales"] == DONE
    assert state["Purchases"] == CURRENT
    assert state["Settle"] == CURRENT


def test_a_plan_without_sales_goes_straight_to_the_purchases() -> None:
    state = steps(approval_status="approved", has_sells=False)
    assert state["Sales"] == SKIPPED
    assert state["Purchases"] == CURRENT


def test_a_finished_plan_is_settled_when_nothing_waits() -> None:
    state = steps(
        approval_status="consumed",
        sells_submitted=True,
        buys_submitted=True,
        any_submitted=True,
        pending_orders=0,
    )
    assert state == {"Approve": DONE, "Sales": DONE, "Purchases": DONE, "Settle": DONE}


def test_a_rejected_plan_stops_at_the_decision() -> None:
    state = steps(approval_status="rejected")
    assert state["Approve"] == STOPPED
    assert state["Sales"] == TODO


def test_the_next_action_follows_the_account_state() -> None:
    assert next_action(connections=0, proposals=0, awaiting_decision=0, orders_waiting=0)[2] == (
        "/connections/new"
    )
    assert next_action(connections=1, proposals=0, awaiting_decision=0, orders_waiting=0)[2] == (
        "/analysis"
    )
    assert next_action(connections=1, proposals=2, awaiting_decision=1, orders_waiting=0)[2] == (
        "/proposals"
    )
    # An order still at the broker comes before a pending decision.
    waiting = next_action(connections=1, proposals=2, awaiting_decision=1, orders_waiting=2)
    assert "order" in waiting[0]
    assert next_action(connections=1, proposals=2, awaiting_decision=0, orders_waiting=0) is None
