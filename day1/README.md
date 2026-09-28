# Day 1: Run the application

Day 1 provides FastAPI, PostgreSQL-backed users and sessions, an Argon2id login, logout, an empty dashboard, and health checks. **No broker connection or trading is available.** Run commands below from the repository root.

## Docker Compose (recommended)

Prerequisites: Docker with Compose. For local development only; this setup exposes plain HTTP on port 8000 and is not suitable for an internet-facing deployment.

1. Copy `.env.example` to `.env`. Replace `SECRET_KEY` with a random value (generate with `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`) and replace `POSTGRES_PASSWORD` with a strong password. Keep `.env` out of Git. The Compose service uses the PostgreSQL values to build its private database URL; `DATABASE_URL` in `.env` is for host-side Python commands only.
2. Start PostgreSQL and the application:

   ```bash day1/README.md
   docker compose up --build -d
   docker compose ps
   ```

3. Create the initial administrator. The command prompts twice for a password (do not put it in shell history):

   ```bash day1/README.md
   docker compose exec app python -m app.cli create-admin --username admin
   ```

4. Visit `http://localhost:8000/login`, sign in, and open the dashboard. Sign out using the header button. Check `http://localhost:8000/healthz` for liveness and `http://localhost:8000/readyz` for database and configuration readiness.

To create another account, use `docker compose exec app python -m app.cli create-user --username second --allow-existing`. To stop containers without deleting users or sessions:

```bash day1/README.md
docker compose down
```

Do not use `docker compose down -v` unless you intend to delete the database volume.

## Run Python locally (optional)

Prerequisites: Python 3.12 and a running PostgreSQL database. Set `DATABASE_URL` in `.env` to a reachable `postgresql+asyncpg://user:password@host:port/database` URL (for example, a separate local PostgreSQL installation). The Compose database does **not** publish a host port, so host-side Python cannot connect to it without changing your local setup.

```bash day1/README.md
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m app.cli create-admin --username admin
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/login`. The application and CLI create missing Day 1 tables automatically; they do not alter existing tables. Back up the database before future schema changes.

## Tests

Install development dependencies in the virtual environment, then run existing tests:

```bash day1/README.md
python -m pip install -r requirements-dev.txt
python -m pytest
```

Tests use an isolated in-memory database and do not require a broker account. Never put real credentials in fixtures.

## Security notes

- Day 1 uses HTTP only for local testing. Before HTTPS deployment, set `ENVIRONMENT=production`, set a unique random `SECRET_KEY`, set `COOKIE_SECURE=true`, and put the app behind a TLS reverse proxy. Do not expose this Compose setup directly to the internet.
- Session tokens are random and only their hashes are saved in PostgreSQL. Session cookies are HttpOnly and SameSite=Lax; login and logout forms use CSRF tokens.
- Login and dashboard contain no trading functionality. Both live-trading switches must remain off.
- `.env` is ignored by Git. Do not commit it or any real passwords, tokens, or account data.
