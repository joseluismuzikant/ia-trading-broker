# Day 3: Country portfolio and market data

Day 3 adds the **Country portfolio** page and the market-data reads behind it. The page shows the positions IOL holds for one country, in a format that does not depend on IOL field names. The adapter can also read quotes and price history. **No trading and no order placement is available.** Run commands below from the repository root.

## What Day 3 adds

- A shared portfolio format in `app/domain/portfolio.py`: instruments, positions, cash, and totals, with no IOL field names.
- A mapping layer inside the IOL adapter that translates IOL responses into that format.
- The country-portfolio page, with a validated country selector and one saved snapshot per country.
- Quote and price-history calls, with timeouts, and retries only for safe reads.
- A monthly IOL call budget that stops reads before they cost money.

## Requirements

- Day 1 and Day 2 working (see [day1/README.md](../day1/README.md) and [day2/README.md](../day2/README.md)).
- Python 3.12.
- An IOL username and password (read-only is enough; nothing here places an order).

## 1. Configure the call budget

IOL is free up to a monthly quota of about **25,000 API calls**. Past that quota IOL charges **USD 500 + VAT**, debited from the brokerage account, and credits it back as a bonus against commissions over the next 30 days. Trading regularly may make the net cost zero, but a runaway loop still spends real money first.

Day 3 therefore counts its own calls and refuses to go past the quota. Two settings in `.env` control it:

```bash day3/README.md
IOL_MONTHLY_CALL_LIMIT=25000
IOL_CALL_WARN_RATIO=0.8
```

- `IOL_MONTHLY_CALL_LIMIT` — a positive value is the hard cap. Once reached, the adapter raises before sending the request, so the limit itself never costs money. Set `0` to remove the cap and accept the cost.
- `IOL_CALL_WARN_RATIO` — the fraction of the limit at which one warning is logged. The default warns at 80%.

Leave the default unless you have a reason to change it.

## 2. Install and start

```bash day3/README.md
source .venv/bin/activate
python -m pip install -r requirements.txt
```

```bash day3/README.md
docker compose up --build -d
```

Day 3 adds no database table. Country portfolios are stored in the existing `snapshots` table, with one kind per country (`portfolio:argentina`, `portfolio:estados_unidos`). **Automatic table creation is still not a migration tool:** a future column change needs an explicit upgrade plan.

## 3. Use the page

1. Sign in at `http://localhost:8000/login`.
2. Open **Portfolio** in the header.
3. Choose a **Country** and press **Show**. `Argentina` is used when the connection has no preference.
4. Press **Refresh from IOL**. The page shows cash, the value of the positions, the total, and one row per position: symbol, description, market, total quantity, free quantity, last price, average price, and value.
5. Switch the country. Each country has its own snapshot, so switching never overwrites the other one's data.
6. Refresh while IOL is unreachable. The last good data stays on screen and is marked **stale**.

Below the header the page also shows how many IOL calls this process has made this month, so a surprising count is visible before it becomes a bill.

## Market-data reads

The adapter can also read market data. There is no page for these yet; they exist for Day 4.

| Call | IOL endpoint |
|---|---|
| Country portfolio | `GET /api/v2/portafolio/{pais}` |
| Latest quote | `GET /api/v2/{mercado}/Titulos/{simbolo}/Cotizacion` |
| Price history | `GET /api/v2/{mercado}/Titulos/{simbolo}/Cotizacion/seriehistorica/{desde}/{hasta}/{ajustada}` |

All three are read-only.

## How the call budget is spent

The counter is deliberately conservative:

- **Every HTTP attempt counts**, including the token request and each retry.
- **Retries only apply to safe reads.** A `5xx` answer or a transport failure is retried at most twice, so one failed read can cost three calls. A write is never retried automatically.
- **Pages never poll.** Opening a page reads PostgreSQL only. Calls happen when you press **Refresh from IOL**, and a refresh costs one token call plus one read.
- **Cash is not a second call.** The country-portfolio endpoint does not report cash, so it is read from the newest saved account-status snapshot instead of spending another call.
- **Positions are not enriched with quotes.** Enriching each position would cost one call per position, so the table shows the price IOL already returned with the portfolio.

The counter is process-wide and resets at the start of each month (UTC). It lives in memory, so a restart resets it. It is a safety net against a runaway loop, not an exact invoice; persisting it is a later hardening task.

## One real read

Step 4 above is the real read: it calls the live IOL API with your saved login and saves what comes back. Keep the following in mind:

- Prefer a read-only IOL user while the trading days are still being built.
- After the first successful refresh, run `docker compose logs app` and confirm that no password and no bearer token appear in the output.
- Read the call counter on the page and confirm it matches the reads you asked for.

To work without a real account, run the test suite instead: it answers the same endpoints from saved fixtures with no network access.

## Tests

```bash day3/README.md
python -m pytest
```

The suite runs fully offline. Day 3 covers the portfolio, quote, and price-history calls against a scripted fake transport, the mapping into the shared format, `404` and retry behaviour, one call per refresh, cash taken from the saved account status, per-country snapshots, stale handling, the country selector, CSRF protection on every write, tenant isolation, and the call-budget cap, warning, and roll-over.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `The monthly IOL API call limit is reached. No call was made.` | The cap is reached for this month. Raise `IOL_MONTHLY_CALL_LIMIT`, wait for the next month, or restart the process to reset the in-memory counter. |
| The page says the refresh failed and the data is stale | IOL was unreachable, slow, or returned an error. The saved data is still correct as of its fetch time. |
| The portfolio is empty, but the account holds positions | The country is wrong. Switch to the other country and press **Show**; a country portfolio only lists what IOL returns for that country. |
| The cash figure is empty | No account-status snapshot is saved yet. Open **Account status**, refresh, then refresh the portfolio. |
| The country selector rejects a value | The country must be one of `argentina` or `estados_unidos`. Any other value is refused before a call is made. |
| Snapshots look empty after a restart | Expected. Snapshots are in PostgreSQL, not in memory; check that the app uses the same `DATABASE_URL`. |

## Money and security notes

- Day 3 is read-only against IOL. It cannot place, cancel, or modify anything.
- Both live-trading switches must remain off. They are still unused by the code.
- Amounts are plain floats for now. Day 4 must replace them with exact money types before any sizing or risk decision uses them.
- `snapshots.payload` holds portfolio data in the shared format, which is easier to read than a raw broker response, but it is still account data. Keep database backups private.
- Do not paste real IOL credentials into fixtures, logs, issues, or chat. Use the test fakes.
