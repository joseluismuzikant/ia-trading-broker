# ia-trading-broker

A human-approved trading assistant for InvertirOnline (IOL).

The application will read a user's portfolio and market data, create weekly BUY, SELL, or HOLD recommendations, apply fixed risk rules, and wait for the user to approve or reject the complete proposal. The first release focuses on safe paper trading. Real IOL order submission stays disabled until the integration and recovery rules are proven.

> This project is in the planning and initial implementation stage. It is not ready for live trading.

## Main goals

- Secure application login for multiple users.
- One encrypted IOL connection per user.
- Live and paper portfolio views.
- Weekly portfolio analysis with a rule-based strategy and an optional LLM.
- Clear reasons, evidence, and risk results for every recommendation.
- Mandatory human approval before orders.
- Persistent paper trading with cash, positions, orders, and fills.
- A private trading-history log for each user.
- Safe recovery after restarts and duplicate requests.
- Deployment on one Oracle Cloud Infrastructure node.

## Safety rules

- The LLM cannot call IOL or create broker payloads.
- Normal Python code calculates order quantities and checks risk.
- Approval is all-or-nothing and expires after 24 hours.
- Changed prices, cash, or positions can invalidate an approval.
- Real trading is off by default.
- Order submission is idempotent: retries must not create duplicate orders.
- An unclear broker response is never retried automatically.
- PostgreSQL is the source of truth for approvals, orders, history, and recovery.
- Secrets and full account snapshots must not appear in logs or LangSmith traces.

## Planned user flow

1. Log in to the application.
2. Connect and test an IOL account.
3. View the live or paper portfolio.
4. Start a weekly analysis.
5. Review BUY, SELL, and HOLD recommendations.
6. Review quantities, risk checks, and the expected portfolio.
7. Approve or reject the complete proposal.
8. Follow order status and fills.
9. Review all activity in the personal trading-history page.

## Architecture

The MVP uses one repository, one FastAPI application, and one PostgreSQL database.

```text
Browser
   -> HTTPS proxy
   -> FastAPI
      |-- server-rendered web pages
      |-- REST endpoints
      |-- short LangGraph analysis flow
      |-- paper/live order executor
      |-- order reconciler
      -> PostgreSQL
      -> IOL API
      -> LLM provider / LangSmith
      -> webhook notifications
```

LangGraph is used only to create a proposal:

```text
load run
  -> load portfolio and market data
  -> calculate features
  -> create recommendations
  -> validate recommendations
  -> calculate proposed orders
  -> check risk
  -> save proposal
```

Human approval, order submission, cancellation, and reconciliation are separate application services. They do not wait inside a long-running LangGraph workflow.

## Planned technology

- Python 3.12
- FastAPI
- Server-rendered HTML templates and simple CSS
- LangGraph and LangSmith
- PostgreSQL
- SQLAlchemy 2 and Alembic
- Pydantic Settings
- `httpx`
- `pytest`
- Docker Compose
- Caddy or Nginx for HTTPS
- Oracle Cloud Infrastructure for deployment

## Frontend pages

The first frontend will include:

- Login
- Dashboard
- IOL connection setup
- Live and paper portfolios
- New weekly analysis
- Analysis status
- Proposal review and approval
- Trading history with filters
- Trade and order details
- Operations view for failed runs and unknown orders

A separate React application is not required for the one-week MVP. The backend exposes clear service and API boundaries so React can replace the template frontend later without changing trading logic.

## Trading history

Each user will have a private, chronological log containing:

- Analysis started, completed, or failed.
- Proposal created, approved, rejected, or expired.
- Recommendations and risk results.
- Order prepared, submitted, accepted, unknown, filled, rejected, or cancelled.
- Paper fills and paper-portfolio changes.

History records are append-only during normal operation. Every history query is restricted by `user_id`, and sensitive credentials or raw tokens are never stored in the log.

## Broker boundaries

IOL-specific details stay inside the IOL adapter. The rest of the application uses neutral models for:

- Instruments
- Account and market snapshots
- Recommendations
- Proposal versions
- Order intents
- Broker order IDs
- Order events

There is no IBKR implementation in the MVP. A future broker should implement the existing portfolio, market-data, and order-execution ports without changing strategy, risk, or approval logic.

## Repository status

The repository currently contains architecture and implementation plans under `docs/`. Application code, Docker configuration, migrations, and setup commands have not been added yet.

Planned structure:

```text
app/
  api/
  domain/
  workflows/
  services/
  ports/
  infrastructure/
    iol/
    simulation/
    persistence/
    notifications/
templates/
static/
tests/
alembic/
docs/
docker-compose.yml
```

## Documentation

- [Final implementation plan](docs/final_plan.md)
- [Original plan](docs/plan.md)
- [Architecture review](docs/PLAN_REVIEW.md)

The final plan is the active implementation reference. The original plan and review are kept to explain the architectural decisions.

## Development roadmap

### Day 1

Create the application, secure login, basic frontend layout, IOL connection page, and read-only portfolio.

### Day 2

Add normalized portfolio and market data, the paper ledger, and portfolio pages.

### Day 3

Add the rule-based strategy, sizing, risk checks, LangGraph analysis, and proposal pages.

### Day 4

Add human approval, paper execution, reconciliation, notifications, and trading history.

### Day 5

Add the optional LLM, deploy to OCI, complete the browser workflow, and investigate IOL order behavior. Live order submission remains disabled if any behavior is unclear.

## Running the project

There are no working installation or startup commands yet because the application has not been bootstrapped. Once the initial project files exist, this section should document:

- Local prerequisites.
- Environment variables.
- Database migrations.
- Admin-user creation.
- Docker Compose startup.
- Test commands.
- Paper-trading setup.
- OCI deployment.

Do not add real credentials to source control. Future local configuration must use an ignored `.env` file based on a committed `.env.example`.

## Security

Do not report security problems in a public issue. Contact the repository owner privately.

Never commit:

- IOL usernames or passwords.
- IOL bearer tokens.
- LLM API keys.
- Session secrets.
- Credential-encryption keys.
- Database backups containing user data.
- Postman collections modified with real credentials.

## Disclaimer

This software is for research and decision support. It does not guarantee returns and is not financial advice. Trading can result in loss. Keep live trading disabled until the safety gates in the final plan have passed and an authorized person has reviewed the deployment.
