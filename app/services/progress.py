"""What step a proposal, or a whole account, is on, and what to do next.

Pure presentation logic with no I/O, so it can be tested without a page. The
review page draws :func:`proposal_steps` as a progress bar, and the dashboard
asks :func:`next_action` for the one thing worth doing now. Nothing here
decides whether an order may be sent; the execution service owns that.
"""

from __future__ import annotations

from dataclasses import dataclass

DONE = "done"
CURRENT = "current"
TODO = "todo"
SKIPPED = "skipped"
STOPPED = "stopped"


@dataclass(frozen=True)
class Step:
    """One stage of a proposal's life, as the progress bar draws it."""

    label: str
    state: str
    hint: str = ""


def proposal_steps(
    *,
    approval_status: str | None,
    has_sells: bool,
    has_buys: bool,
    sells_submitted: bool,
    buys_submitted: bool,
    any_submitted: bool,
    pending_orders: int,
) -> list[Step]:
    """Review, sales, purchases, settlement: where this proposal stands."""
    decided = approval_status in ("approved", "consumed")
    stopped = approval_status in ("rejected", "expired")

    if decided:
        review = Step("Approve", DONE, "Decision recorded")
    elif stopped:
        review = Step("Approve", STOPPED, f"Plan {approval_status}; nothing was sent")
    else:
        review = Step("Approve", CURRENT, "Review the plan, then approve or reject it")

    if not has_sells:
        sales = Step("Sales", SKIPPED, "No sales in this plan")
    elif sells_submitted:
        sales = Step("Sales", DONE, "All sales submitted")
    elif decided:
        sales = Step("Sales", CURRENT, "Execute each sale")
    else:
        sales = Step("Sales", TODO)

    sales_clear = not has_sells or sells_submitted
    if not has_buys:
        purchases = Step("Purchases", SKIPPED, "No purchases in this plan")
    elif buys_submitted:
        purchases = Step("Purchases", DONE, "All purchases submitted")
    elif decided and sales_clear:
        purchases = Step("Purchases", CURRENT, "Execute the purchases the cash covers")
    else:
        purchases = Step("Purchases", TODO)

    submitted_all = (not has_sells or sells_submitted) and (not has_buys or buys_submitted)
    if any_submitted and pending_orders > 0:
        settle = Step(
            "Settle",
            CURRENT,
            f"{pending_orders} order(s) waiting at the broker; refresh their status",
        )
    elif any_submitted and submitted_all:
        settle = Step("Settle", DONE, "No order is waiting at the broker")
    else:
        settle = Step("Settle", TODO)

    return [review, sales, purchases, settle]


def next_action(
    *,
    connections: int,
    proposals: int,
    awaiting_decision: int,
    orders_waiting: int,
) -> tuple[str, str, str] | None:
    """The single most useful next move for an account: ``(text, label, href)``."""
    if connections == 0:
        return ("Add your broker login to start.", "Add a connection", "/connections/new")
    if orders_waiting:
        return (
            f"{orders_waiting} order(s) are waiting at the broker. Refresh their status.",
            "Open proposals",
            "/proposals",
        )
    if awaiting_decision:
        return (
            f"{awaiting_decision} proposal(s) are waiting for your decision.",
            "Review proposals",
            "/proposals",
        )
    if proposals == 0:
        return ("No analysis yet. Run one to get a first plan.", "Run an analysis", "/analysis")
    return None
