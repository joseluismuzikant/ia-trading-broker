---
name: trading-safety-review
description: Read-only review of a diff for trading-safety, data-isolation and secret-handling mistakes. Use before merging any change to orders, approvals, execution, broker adapters, credentials, auth, sessions or events.
---

# Trading safety review

Review the current diff (`git diff main...HEAD`, plus uncommitted changes).
Do not edit files. Report findings ordered by severity, each with file, line,
the concrete failure scenario, and the fix.

## Checklist

**Orders and execution**
- Is any broker write reachable without a recorded, pending, unexpired
  approval?
- Is the order row saved (with its idempotency key) before the broker call?
- Can any path resend an order that already has a row, including `prepared`
  and `unknown`?
- Are timeouts, transport errors and 5xx recorded as `unknown`, not as
  rejected or accepted?
- Are sells still submitted before buys?
- Does anything other than `services/execution.py` call an `OrderExecutor`?

**Model boundary**
- Can a model answer influence a quantity, price, symbol outside the
  validated set, or broker request without passing `advice_validation` and
  `rebalance.py`?
- Is there still exactly one model-calling node, with retries bounded?

**Money**
- Any `float` in money math? Any value computed without `quote_unit`?
- Any mixing of ARS and USD amounts without an explicit conversion?

**Isolation and secrets**
- Does every new query filter by the session user? Any user id taken from
  input?
- Can a password, token, API key or encrypted credential reach an event,
  log line, trace, template, proposal payload or order payload?
- Do new POST forms validate CSRF?

**Tests**
- Does every new branch above have a test? Do tests stay offline?

Answer "no findings" only after going through every item.
