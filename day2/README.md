# Day 2: Connect IOL and read profile and account status

Day 2 adds encrypted IOL logins plus two read-only pages: **IOL profile** and **Account status**. A user saves an IOL username and password, tests the login, and refreshes broker data on demand. **No trading, no portfolio, and no order placement is available.** Run commands below from the repository root.

## What Day 2 adds

- A saved `broker_connections` row per user, with the IOL password stored only as Fernet ciphertext.
- A token per connection with a single retry after an authentication error.
- Timestamped `snapshots` rows for the profile and account-status responses.
- Two pages that render the newest saved snapshot immediately and refresh only when asked.
- Stale-data handling: a failed refresh keeps the previous snapshot on screen and marks it stale.

## Requirements

- The Day 1 setup working (see [day1/README.md](../day1/README.md)).
- Python 3.12.
- An IOL username and password (read-only is enough; nothing here places an order).

## 1. Configure the credential key

Add a credential-encryption key to `.env`. Generate one and paste it into `CREDENTIAL_ENCRYPTION_KEY`:

```bash day2/README.md
python3 -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
```

`.env.example` documents the key and the two IOL settings.

> **This key is the only way to read saved IOL passwords.** Losing it, or changing it, makes every saved connection unusable; the user has to delete the connection and add it again. Back it up with the same care as the database. Do not commit it.

If the key is missing, the application starts and falls back to the shared development key from `.env.example` with a warning. In that case the encryption is effectively public and every saved password must be treated as exposed. Set a real key before saving any connection.

## 2. Install and start

```bash day2/README.md
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Docker Compose works as on Day 1. Rebuild so the new dependency is installed in the image:

```bash day2/README.md
docker compose up --build -d
docker compose exec app python -m app.cli create-admin --username admin
```

The application and CLI create the new `broker_connections` and `snapshots` tables automatically when they start. Existing Day 1 tables are not altered. **Day 2 needs no migration, but this automatic creation is not a migration tool.** A future column change needs an explicit upgrade plan.

## 3. Use the pages

1. Sign in at `http://localhost:8000/login`.
2. Open **Connections** in the header and choose **Add a connection**.
3. Enter a label, the IOL username, the IOL password, and the country. Save.
4. On the connection page, choose **Test connection**. A token is requested but no account data is read.
5. Open **IOL profile** and choose **Refresh from IOL**. The page shows the name, account number, investor profile, and a document number with only the last three digits visible.
6. Open **Account status** and choose **Refresh from IOL**. The page shows the total in pesos and one row per account.
7. Refresh again while IOL is unreachable (or with a deliberately wrong password). The previously saved data stays on screen and is marked **stale** with the refresh error next to it.

The saved password is never shown again, not even on the connection page. To use a different password, delete the connection and add it again.

## One real read

Steps 4 to 6 above are the real read: they call the live IOL API with your saved login and save what comes back. Keep the following in mind:

- The test request asks IOL only for a token. The refresh requests then read `GET /api/v2/datos-perfil` and `GET /api/v2/estadocuenta`.
- Both are read-only endpoints.
- Prefer a read-only IOL user while the trading days are still being built.
- After the first successful refresh, run `docker compose logs app` and confirm that no password and no bearer token appear in the output.

To work without a real account, run the test suite instead: it answers the same endpoints from saved fixtures with no network access.

## Credentials, tokens, and what is stored

| Item | Where it lives | Lifetime |
|---|---|---|
| IOL password | `broker_connections.password_encrypted`, Fernet ciphertext | Until the connection is deleted |
| IOL bearer token | Process memory only | About 15 minutes, then renewed |
| Profile and account snapshots | `snapshots.payload`, JSON | Kept; the newest per kind is replaced only by a newer one |

A token is never written to the database, the logs, or a page. A password is decrypted in memory for the duration of one broker call.

## Tests

```bash day2/README.md
python -m pytest
```

The suite runs fully offline. Day 2 covers the client against a scripted fake transport, token reuse, the single retry after an authentication error, fallback to a fresh login when a refresh is refused, snapshot and stale handling, tenant isolation, form validation, CSRF protection on every write, and a check that no password or token reaches the logs.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `CredentialEncryptionError` while saving | `CREDENTIAL_ENCRYPTION_KEY` is not a valid Fernet key. Regenerate it as shown in step 1. |
| Test connection fails with an invalid-grant message | Wrong IOL username or password, or the IOL account is blocked. Re-add the connection with the correct password. |
| The page says the refresh failed and the data is stale | IOL was unreachable, slow, or returned an error. The saved data is still correct as of its fetch time. Try again later. |
| `Readyz` reports a configuration problem | Set `SECRET_KEY` and `CREDENTIAL_ENCRYPTION_KEY` in `.env` and restart. |
| Snapshots look empty after a restart | Expected. Snapshots are in PostgreSQL, not in memory; check that the app uses the same `DATABASE_URL`. |

## Security notes

- Day 2 is read-only against IOL. It cannot place, cancel, or modify anything.
- Both live-trading switches must remain off. They are still unused by the code.
- The profile page masks the document number so a screenshot does not leak it in full.
- Full broker responses are stored in `snapshots.payload` and are never rendered raw. Keep database backups private: they contain account data and encrypted passwords.
- Do not paste real IOL credentials into fixtures, logs, issues, or chat. Use the test fakes.
