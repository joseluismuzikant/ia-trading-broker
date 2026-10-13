---
name: add-broker-adapter
description: Add or change a broker integration (PPI, IBKR, paper accounts, IOL fixes) behind the BrokerReader and OrderExecutor ports. Use when a task mentions a broker API, SDK, credentials fields, or order submission for a venue.
---

# Add a broker adapter

Read `docs/roadmap.md` section 4.1 (ports) and the IOL package
`app/infrastructure/iol/` first; it is the reference implementation.

1. **Package.** `app/infrastructure/<broker>/` with `client.py` (transport or
   SDK wrapper), `mapping.py` (vendor payload → domain models), `errors.py`,
   and `order_executor.py`. Vendor field names never leave this package.
2. **Credentials.** Declare the fields the connection form must ask for
   (for PPI: account number, public API key, private API key). Store them
   encrypted as one JSON blob. Never log them.
3. **Reads (`BrokerReader`).** Portfolio, balances per currency, quotes,
   daily history, operation status, and cash movements if the broker offers
   them. Map every instrument to a catalog symbol and set `quote_unit` (100 for
   bonds quoted per 100 nominal). Unknown instruments become `other`, never a
   guess.
4. **Writes (`OrderExecutor`).** Limit orders only. Map responses to
   `accepted`, `rejected` or `unknown` exactly as `IOLOrderExecutor` does: a
   timeout, transport failure or 5xx is `unknown` and is never retried.
   Missing trading parameters (settlement term, validity) fail closed.
5. **Registry.** Register the reader and the executor in the broker registry
   keyed by `broker_type`.
6. **Tests.** Add a fake transport (or a fake SDK object) under `tests/` and
   anonymized recorded responses under `tests/fixtures/<broker>/`. Cover:
   mapping, auth failure, each order outcome, idempotent resubmission refusal.
7. **Docs.** Add the broker to the README (Status, Configuration, smoke test)
   and record anything unverified in `docs/roadmap.md` section 7.
