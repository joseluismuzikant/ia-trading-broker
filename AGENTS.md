# AGENTS.md

Instructions for AI coding assistants working on this repository. This file is
the single source of truth. It is read natively by OpenCode (and other tools
that follow the `AGENTS.md` convention); `CLAUDE.md` imports it for Claude
Code. Do not duplicate these rules elsewhere: edit them here.

## What this project is

A portfolio assistant for broker accounts. It reads the portfolio, asks one
model for a target allocation of the whole account, turns that allocation into
exact trades in Python, and stops for a human to approve. Approval submits the
orders to the broker. Read `README.md` for how it works today and
`docs/roadmap.md` for where it is going.

## Commands

```bash
# Tests: offline, SQLite in memory, fake IOL transport, fake advisor
python -m pytest -q
python -m pytest -q tests/test_rebalance.py   # one file

# Run the stack
docker compose up --build -d
docker compose exec app python -m app.cli create-admin --username admin
docker compose logs -f app

# Lint and format (available from roadmap phase 0)
ruff check . && ruff format --check .
```

Python 3.12. Install with `python -m pip install -r requirements-dev.txt`.

## Invariants — never break these

1. **One model call per run.** `analyse_portfolio` in
   `app/workflows/trading_flow.py` is the only node that calls a model.
2. **The model advises, Python decides.** A model returns target weights,
   reasons and risks (`PortfolioAdvice`). It never supplies quantities, prices
   or broker requests. `app/services/rebalance.py` is the only place that turns
   weights into trades.
3. **No order without a recorded human approval.** There is no code path from
   a model answer to a broker call that skips `approvals`.
4. **`app/services/execution.py` is the only caller of an `OrderExecutor`.**
   It saves the order row (idempotency key) **before** calling the broker,
   submits sells before buys, treats a timeout or 5xx as `unknown`, and never
   resends an existing order.
5. **Owner scoping.** Every query filters by the user from the session
   (`app/deps.py`). Never take a user id from a form, a query string or a path.
   Another user's resource is "not found".
6. **Every state change writes an event** through `app/services/events.py`.
7. **Secrets never leave their box:** no passwords, tokens, API keys or
   encrypted credentials in events, logs, traces, proposals or order payloads.
8. **Tests never touch the network**: use `tests/fakes.py` (fake IOL), the
   `FakePortfolioAdvisor`, and recorded fixtures in `tests/fixtures/`.

## Architecture

| Layer | Path | Rule |
|---|---|---|
| Domain | `app/domain/` | Pure: Pydantic models, `Money`, indicators, contracts. No I/O |
| Ports | `app/ports/` | Protocols (`PortfolioAdvisor`, `OrderExecutor`; `BrokerReader` from phase 1) |
| Infrastructure | `app/infrastructure/` | One package per external system (`iol/`, `openai/`, `simulation/`). The only code that knows a vendor's field names, endpoints or SDK |
| Services | `app/services/` | Use cases, transactions, events |
| Workflow | `app/workflows/trading_flow.py` | The LangGraph `Run`. Nodes call services; nodes do not talk to vendors directly |
| Web | `app/api/`, `templates/` | Pages and forms. Call services only. Every write form has a CSRF token |
| Config | `config/*.toml` | Universe, limits, model catalog. Nothing in code names a symbol or a model |

## Conventions

- Money is `Money`/`Decimal`, never `float`, except when serializing to a
  broker API. Bonds are priced per 100 nominal (see roadmap phase 0).
- Async SQLAlchemy 2.x. No migrations: tables are created at startup. A new
  column must come with the `ALTER TABLE` statement in the PR description.
- Docstrings explain **why** and the rule being enforced, in the style already
  used in the codebase. Code, comments and docs are in English.
- Small modules with one responsibility. Prefer a new adapter behind an
  existing port over a conditional on the vendor.
- Errors are explicit and fail closed: when a trading parameter is missing,
  refuse instead of guessing.

## How to work here

1. Read the relevant phase in `docs/roadmap.md` and the files it names.
2. Make the smallest change that completes one checklist item; add or update
   tests in the same change.
3. Run `python -m pytest -q` (and `ruff` once it exists) before saying a task is
   done. Never mark a test as skipped to make the suite pass.
4. Tick the item in `docs/roadmap.md` and keep `README.md` accurate: if
   behavior, configuration, endpoints or known issues change, update the
   README in the same change.
5. Before touching execution, approvals, credentials or auth, re-read the
   invariants above and run the trading safety review (see below).

## Shared workflows (skills)

Step-by-step recipes live in `.claude/skills/<name>/SKILL.md`. Both Claude
Code and OpenCode load that directory. Read the matching one before starting:

| Skill | When |
|---|---|
| `implement-phase` | Working through a phase of `docs/roadmap.md` |
| `trading-safety-review` | Reviewing a diff that touches orders, approvals, credentials, auth or events |
| `add-broker-adapter` | Adding or changing a broker (PPI, IBKR, paper) |
| `add-llm-provider` | Adding a model provider or fixing how one is called |
| `strategy-profile` | Adding or tuning a simulation type |
| `web-page` | Adding a page or a form |

## Never

- Commit `.env`, keys, real account data or real API responses with personal
  data (anonymize fixtures).
- Read `.env` or print environment variables that hold secrets.
- Send a real order, call a real broker, or call a paid model API from a test
  or a script without the user asking for it explicitly in the conversation.
