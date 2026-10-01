# Day 4: Indicators, paper ledger, and proposal

Day 4 turns saved market data into a decision. It calculates indicators in
Python, copies a portfolio snapshot into a paper ledger, runs a rule-based
strategy, sizes each order, applies fixed risk checks, and saves the result as a
proposal. It does not place an order. Approval and paper execution are Day 5.

## What Day 4 adds

- Python indicators: EMA, RSI, MACD, ATR, volume average, returns, and exposure.
- An exact money type, so sizing and risk never use binary floats.
- A paper ledger created from a country portfolio snapshot.
- A rule-based buy, sell, and hold strategy.
- Fixed sizing and fixed risk checks, together called the DecisionPolicy.
- An immutable proposal, saved and shown on a review page.
- Safe price-history reads for every symbol the analysis judges.

## Requirements

- The Day 1 to Day 3 setup, with a saved connection and at least one portfolio
  refresh.
- No new environment variables.

## 1. Install and start

The setup is the same as Day 1 to Day 3.

```bash
docker compose up --build
```

Or run it locally:

```bash
python -m pip install -r requirements-dev.txt
uvicorn app.main:app --reload
```

Then open `http://localhost:8000` and log in.

## 2. Create the paper ledger

Open **Analysis** in the navigation. If no ledger exists yet, the page says so.

1. Refresh the portfolio once, so a portfolio snapshot exists for the country.
2. Click **Create paper ledger**.

The ledger copies the portfolio's cash and positions, priced to the cent. It
never contacts IOL, so creating it costs no API calls. A position without a
price or a quantity is left out, because sizing against an unknown value would
be a guess.

## 3. Run an analysis

Click **Run analysis**. The page then shows:

- the paper ledger: cash, positions value, total, and the positions table;
- the new proposal: summary, status, approval mode, execution mode, turnover,
  and the plan risk checks;
- one row per instrument: action, quantity, limit price, notional, weight, and
  the reason;
- an expandable block per instrument with the indicator evidence and every
  risk check.

Running an analysis refreshes price history for the instruments it judges. Those
are safe reads, counted against the IOL monthly call budget. A symbol whose read
fails is judged on the history already saved.

## Indicators

All indicators are calculated in `app/domain/indicators.py`, in Python, with
tests. A value is empty until its window has enough observations.

| Indicator | Meaning |
| --- | --- |
| EMA | Exponential moving average, seeded on the first full window. |
| RSI | Relative Strength Index, Wilder smoothed, 0 to 100. |
| MACD | Fast EMA minus slow EMA, with a signal line and a histogram. |
| ATR | Average True Range, Wilder smoothed: how much price moves. |
| Volume average | Simple average of volume, skipping bars with no volume. |
| Returns | Fractional return over a fixed number of periods. |
| Exposure | A position's share of the portfolio, or empty when the total is zero. |

## Strategy rules

The strategy in `app/domain/strategy.py` is fixed Python and fully determined by
its inputs.

- **Buy** when the fast EMA is above the slow EMA, the MACD histogram is
  positive, and RSI is between 45 and 70.
- **Sell** when the fast EMA is below the slow EMA and RSI is below 50.
- **Hold** in every other case, including when there is not enough history.

A missing indicator never produces a trade.

## Risk limits

Sizing and the risk checks live in `app/domain/policy.py`. The constants are in
`app/domain/trading.py`.

| Limit | Value | Applies to |
| --- | --- | --- |
| Max order weight | 10% of the portfolio | Buys |
| Max position weight | 25% of the portfolio | Buys and sells |
| Max turnover | 20% of the portfolio | The whole plan |
| Cash buffer | 1% of cash kept back | Buys |
| Allowlist | `GGAL`, `YPFD` | Buys and sells |

Every check must pass before a recommendation counts as an order. A failed check
zeroes the quantity, and the recommendation is still saved with the failing
check visible, so the review page shows what was blocked and why.

## The proposal

A proposal is a saved plan, not an action. It records the approval mode
(`HUMAN_IN_THE_LOOP`), the execution mode (`PAPER`), the portfolio value, the
cash, every recommendation, the plan turnover, and the risk results.

A saved proposal is never edited. A later analysis inserts a new one, so an
approval in Day 5 always points at exactly the plan that was reviewed.

## Tests

```bash
python -m pytest tests/test_money.py tests/test_indicators.py \
  tests/test_strategy.py tests/test_policy.py tests/test_ledger.py \
  tests/test_analysis.py tests/test_analysis_pages.py -q
```

Or run everything:

```bash
python -m pytest -q
```

All tests run offline against a scripted fake broker. No test reaches IOL.

## Troubleshooting

- **"No paper ledger yet"** — refresh the portfolio for the country, then click
  **Create paper ledger**.
- **"Create a paper ledger first"** — run an analysis only after the ledger
  exists.
- **Every instrument holds** — the saved price history is too short for the slow
  EMA, or the trend is flat. Refresh the portfolio and run again.
- **A buy is blocked by `cash`** — the ledger's cash comes from the newest
  account-status snapshot. Refresh the account status first.

## Money and security notes

- Sizing and risk use the exact `Money` type, not binary floats. A rounding
  error cannot change an order quantity.
- The proposal is created and shown only. There is no approval step and no
  order submission on Day 4.
- Passwords and tokens never reach the analysis. Logs contain no credentials.
- The analysis reads price history from saved snapshots, so it is repeatable and
  free of charge.
