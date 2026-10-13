# Operations

Configuration, day-to-day commands, the smoke test, and tracing. For the short
version see the [README](../README.md).

## Configuration

- [`config/universe.toml`](../config/universe.toml) — instruments, paper names,
  quotation factors, minimum quantities, and limits.
- [`config/models.toml`](../config/models.toml) — label, model id, provider,
  endpoint, the env var that holds the key, the wire protocol (`api =
  "responses"`, `"chat"`, or `"anthropic"` for Claude through its own SDK), for
  chat endpoints how JSON is requested (`response_format = "json_schema"`, or
  `"json_object"` for providers with only JSON mode, like DeepSeek), and an
  optional `timeout_seconds` for slow reasoning models. An entry with `kind =
  "fake"` builds the offline simulator instead: no endpoint, no key. The
  shipped catalog lists GPT-6.1 Sol, Gemini 3.8 Flash, MiMo V2.6 Pro, DeepSeek
  R1, Grok 4, Claude Opus 5.5, and the Fake simulator (smoke test).

Copy `.env.example` to `.env`. Keys stay in the environment, never in TOML or Git.

| Setting | Purpose |
|---|---|
| `SECRET_KEY` | Application secret |
| `CREDENTIAL_ENCRYPTION_KEY` | Fernet key for stored broker passwords; keep it with database backups |
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | Compose database settings |
| `DATABASE_URL` | Database URL for local Python |
| `OPENAI_API_KEY`, `GEMINI_API_KEY`, `MIMO_API_KEY`, `DEEPSEEK_API_KEY`, `XAI_API_KEY`, `ANTHROPIC_API_KEY` | Optional; a run needs the key named by its catalog entry. The fake simulator needs none |
| `OPENAI_TIMEOUT_SECONDS` | Default wait for one model call (30 s); a catalog entry's `timeout_seconds` overrides it |
| `ANALYSIS_MODEL_DEFAULT` | Default catalog key; blank uses `[defaults].model` |
| `IOL_LIVE_PLAZO` | Settlement term for live orders (e.g. `t2`). Live execution fails closed while empty, which is how it stays off |
| `IOL_LIVE_VALIDITY_DAYS` | Days an order stays valid (`validez`). `0` fails closed |
| `APPROVAL_EXECUTION_WINDOW_HOURS` | Hours an approved plan may still be executed (default 24, max 168). Add `APPROVAL_EXECUTION_WINDOW_HOURS=24` to `.env` to change it |
| `PPI_BASE_URL` | PPI REST origin. Defaults to PPI's **sandbox** |
| `PPI_AUTHORIZED_CLIENT`, `PPI_CLIENT_KEY` | Integration headers PPI issues; required to build a PPI client |
| `PPI_LIVE_SETTLEMENT`, `PPI_LIVE_OPERATION_TERM` | Settlement and order term for live PPI orders (e.g. `INMEDIATA`, `HASTA-SU-EJECUCIÓN`). Live PPI execution fails closed while empty |
| `LANGSMITH_TRACING` | Off by default; `true` enables LangSmith tracing |
| `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT` | LangSmith credentials and project |
| `LANGSMITH_ENDPOINT`, `LANGSMITH_WORKSPACE_ID` | Optional LangSmith endpoint/workspace |

## LangSmith tracing

Tracing is optional and off by default; the application runs the same without
it, and the tests make no LangSmith network call. To inspect a run:

```bash
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=<key>
LANGSMITH_PROJECT=ia-trading-broker
```

then `docker compose up --build -d app` and run one analysis. The run appears
in the configured LangSmith project as one trace with the LangGraph nodes
(`load_broker_state`, `create_paper_ledger`, `build_analysis_context`,
`analyse_portfolio`, `validate_advice`, `apply_to_paper_ledger`,
`request_approval`, `await_approval`, `execute_live`) and a nested span around
the portfolio model call.

What a trace shows: connection id, country, model provider and name, proposal
id, execution mode, and the normalised model request/response. What it never
shows: broker usernames or passwords, encrypted credentials, bearer or refresh
tokens, provider API keys, session cookies, or CSRF tokens. Those keys are
dropped from every span, and no traced object can hold them.

## Restarting after changes

| What changed | What to run |
|---|---|
| Python code, templates, static files | `docker compose up --build -d app` — the source is baked into the image, so a rebuild is required |
| `.env` values (keys, plazo, ...) | `docker compose up -d app` — the container is recreated with the new environment |
| `config/universe.toml` or `config/models.toml` | rebuild, same as code: `docker compose up --build -d app` |
| Dependencies in `requirements*.txt` | `docker compose up --build -d app` |
| Local Python variant | stop `uvicorn` (Ctrl-C) and start it again; no rebuild |

Useful companions:

```bash
docker compose logs -f app     # follow application logs
docker compose ps              # what is running
docker compose restart app    # plain restart (no rebuild, keeps old env/code)
```

## Database: schema and data

- **Tables** are created automatically at startup when they are missing. This
  is not a migration system: a column added to an *existing* table needs a
  manual `ALTER TABLE` (or a fresh database).
- **Reset the analysis data** — every proposal, approval, order, and trading
  event — while keeping users, connections, snapshots, and the paper ledger
  (Compose default credentials):

```bash
docker compose exec db psql -U trading -d trading -c "BEGIN;
DELETE FROM trading_events;
DELETE FROM orders;
DELETE FROM approvals;
DELETE FROM proposals;
COMMIT;
"
```

- **Start the database from zero** — deletes users, connections, and
  everything else (back up first if you care about the data):

```bash
docker compose down -v
docker compose up --build -d
docker compose exec app python -m app.cli create-admin --username admin
```

## Smoke test

Three levels, in order: offline tests, a safe browser pass, and — only when
you are ready — a live pass with real money. Each level says exactly what to
run and what you must see.

### Level 0 — Automated tests (offline, no broker, no money)

```bash
python -m pytest -q        # on the host; needs requirements-dev installed
ruff check . && ruff format --check .
```

Expected: every test passes (635 at the time of writing). The suite uses an
isolated database and a fake IOL transport, so it can never send a real order.
GitHub Actions (`.github/workflows/ci.yml`) runs the same three commands on
every push to `main` and every pull request. The Docker image ships the
application only; run the tests on your machine.

### Level 1 — Safe development smoke (browser, no real orders)

Preconditions: the app is running, `IOL_LIVE_PLAZO` empty in `.env`.

1. `curl -fsS http://localhost:8000/healthz && curl -fsS http://localhost:8000/readyz`
   → both return success.
2. Log in at <http://localhost:8000/login>. **Profile** → add your IOL
   connection → **Test** → the broker accepts the login.
3. **Portfolio** → **Refresh** → your real holdings appear.
4. **Analysis** → choose a model (the **Fake simulator (smoke test)** needs no
   provider key) → **Run analysis**.
   Expected: a success message, a *fresh* simulated portfolio created
   automatically, and a proposal with the current-vs-target allocation, the
   simulated portfolio after the recommendation, the AI summary, and risks.
5. Open **Proposals** → the plan is listed with its status and trades → open
   it. Reject it. Expected: *"Rejected. Nothing was sent to the broker."*
6. Run a second analysis, press **Approve plan**, then try to execute a trade.
   Expected: a clear **"IOL_LIVE_PLAZO is not configured"** block and History
   shows no `order.prepared`/`order.submitted` events.

This level sends **zero** broker trade requests.

### Level 2 — Live smoke — REAL MONEY, REAL BROKER EXECUTION

Only after Levels 0 and 1 pass. Nothing is submitted without your clicks.

**Enable LIVE mode** in `.env`:

```bash
IOL_LIVE_PLAZO=t2          # the settlement term your account trades on
IOL_LIVE_VALIDITY_DAYS=1
```

Apply it: `docker compose up -d app` (env only) or
`docker compose up --build -d app` if you also changed code. Proposals now
execute for real; their page shows a **LIVE TRADING** badge.

1. **Analysis** → **Run analysis** → open the saved proposal.
2. Read **Current vs target allocation**, the **sales** and **purchases**
   tables (each showing the suggested quantity), the **Not executable** table
   if present, the cash summary, and the risks.
3. **Human review** → **Approve plan**. Expected: the decision is recorded and
   *nothing is sent*.
4. For each trade you want, adjust the quantity if you wish and press its
   **Execute** button; do the sales first so their cash is available. Expected:
   an IOL operation number and an **ACCEPTED** status. *ACCEPTED means IOL took the
   order; only FILLED means it executed.* A trade the broker rejects or cancels
   offers **Retry**; one still at the broker cannot be resent.
5. **Refresh Order Status**, then execute the purchases the available cash
   covers (a purchase the cash cannot fund is refused). Finally **Portfolio** → **Refresh** to see your updated IOL
   holdings.

If the app restarts at any point, reopen the proposal: every order keeps its
state and operation number, and pressing Execute again never resends one.

### What is verified where

- **Automated tests (635, offline):** the full flow against a fake IOL
  transport: stock and bond sells/buys, phase routes and per-trade execution, form
  fields and endpoints, accepted / rejected / partial / timeout handling, no
  duplicate submissions, cash and holdings guards, restart safety, and the PPI
  client and executor against a mock transport. No real order is ever sent.
- **Real IOL read endpoints:** login, portfolio, account status, and price
  history work against IOL in your deployment (Level 1 exercises them).
- **Real trade submission:** requires your confirmation in the UI. It has
  **not** been verified with a real order during implementation.
- **PPI:** not exercised against PPI at all. The OpenAPI file's examples are
  illustrative, not recorded responses.

Dates use Argentina time (`UTC-3`); money uses local separators, such as
`99.895,16 ARS`.

### Failure checks

- Start without provider keys: the application boots. A run needing a missing
  key shows an error and saves no proposal; the Fake simulator needs none.
- An unknown model key is refused.
- A failed IOL refresh keeps the last snapshot, marks it stale, and records a
  warning on the proposal.
- Another user's proposal URL is refused.
- A model endpoint that only supports chat completions must be configured with
  `api = "chat"` in `config/models.toml`; a wrong `base_url` fails with a
  message naming the host and the file to fix.

## Working with AI coding assistants

The repository is set up to be implemented phase by phase with an AI coding
assistant, and it is not tied to one. The same instructions work in
**Claude Code** and in **OpenCode** (where you can pick any provider: DeepSeek,
Gemini, OpenAI, Grok, local models…). If you run out of tokens with one tool or
model, switch to the other and continue on the same branch.

| File | Claude Code | OpenCode | Purpose |
|---|---|---|---|
| [`AGENTS.md`](../AGENTS.md) | via `CLAUDE.md` | native | **Single source of truth**: commands, invariants, conventions, workflow |
| [`CLAUDE.md`](../CLAUDE.md) | native | ignored when `AGENTS.md` exists | Imports `AGENTS.md`, plus Claude-only notes |
| `.claude/skills/*/SKILL.md` | native | native | Shared recipes: `implement-phase`, `trading-safety-review`, `add-broker-adapter`, `add-llm-provider`, `strategy-profile`, `web-page` |
| `.claude/agents/` · `.opencode/agents/` | ✓ | ✓ | Thin subagent wrappers (`trading-safety-reviewer`, `broker-integrator`, `test-writer`) that point to the skills |
| `.claude/commands/` · `.opencode/commands/` | ✓ | ✓ | `/implement-phase <n>` and `/review`, same text in both |
| `.claude/settings.json` · `opencode.json` | ✓ | ✓ | Allowed commands; `.env` is never readable |

Rules for keeping the two in sync:

- Put every rule that applies to all tools in `AGENTS.md` or in a skill. The
  per-tool files only wrap them.
- A new subagent or command gets one file in each tool folder, with the same
  body.

Typical loop, in either tool:

```text
/implement-phase 1      # works through a docs/roadmap.md phase, with tests
/review                 # read-only trading safety review of the diff
```

Then open a PR. For changes to orders, approvals, credentials or auth, run
`/review` with your strongest available model.
