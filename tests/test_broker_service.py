"""Tests for the broker read path: tokens, retries, snapshots, and staleness."""

from __future__ import annotations

from app.models import SNAPSHOT_ACCOUNT_STATUS, SNAPSHOT_PROFILE
from app.services import broker as broker_service
from app.services import connections as connections_service
from app.services import snapshots as snapshots_service

async def make_connection(db_session, user, fake):
    """Save a connection whose credentials match the fake broker."""
    return await connections_service.create_connection(
        db_session,
        user_id=user.id,
        label="My IOL account",
        username=fake.username,
        password=fake.password,
    )


# --- Successful reads -----------------------------------------------------


async def test_profile_read_saves_a_snapshot(db_session, make_user, fake_iol) -> None:
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)

    result = await broker_service.read_profile(
        db_session, user_id=user.id, connection=connection
    )

    assert result.ok is True
    assert result.is_stale is False
    assert result.snapshot is not None
    assert result.snapshot.payload["nombre"] == "Ana"
    assert result.snapshot.kind == SNAPSHOT_PROFILE
    assert connection.last_error is None
    assert connection.last_used_at is not None


async def test_account_status_read_saves_a_snapshot(
    db_session, make_user, fake_iol
) -> None:
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)

    result = await broker_service.read_account_status(
        db_session, user_id=user.id, connection=connection
    )

    assert result.ok is True
    assert result.snapshot.payload["totalEnPesos"] == 200.0
    assert result.snapshot.kind == SNAPSHOT_ACCOUNT_STATUS


async def test_a_second_read_adds_a_new_snapshot(db_session, make_user, fake_iol) -> None:
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)

    first = await broker_service.read_profile(
        db_session, user_id=user.id, connection=connection
    )
    second = await broker_service.read_profile(
        db_session, user_id=user.id, connection=connection
    )

    assert first.snapshot.id != second.snapshot.id
    stored = await snapshots_service.count_snapshots(
        db_session, user_id=user.id, connection_id=connection.id
    )
    assert stored == 2


# --- Token lifecycle ------------------------------------------------------


async def test_token_is_reused_between_reads(db_session, make_user, fake_iol) -> None:
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)

    await broker_service.read_profile(db_session, user_id=user.id, connection=connection)
    await broker_service.read_account_status(
        db_session, user_id=user.id, connection=connection
    )

    # The password grant happens once; the second read reuses the cached token.
    assert fake_iol.token_grants == ["password"]


async def test_connection_test_issues_a_token(db_session, make_user, fake_iol) -> None:
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)

    result = await broker_service.test_connection(db_session, connection=connection)

    assert result.ok is True
    assert result.error is None
    assert fake_iol.token_grants == ["password"]
    # A test must not read account data.
    assert fake_iol.authenticated_paths == []


async def test_a_rejected_token_is_refreshed_once(db_session, make_user, fake_iol) -> None:
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)
    # The first authenticated call is rejected, like an expired token.
    fake_iol.reject_authenticated_requests = 1

    result = await broker_service.read_profile(
        db_session, user_id=user.id, connection=connection
    )

    assert result.ok is True
    assert fake_iol.token_grants == ["password", "refresh_token"]
    assert len(fake_iol.authenticated_paths) == 2


async def test_a_refused_refresh_falls_back_to_a_login(
    db_session, make_user, fake_iol
) -> None:
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)
    fake_iol.reject_authenticated_requests = 1
    fake_iol.accept_refresh_tokens = False

    result = await broker_service.read_profile(
        db_session, user_id=user.id, connection=connection
    )

    assert result.ok is True
    assert fake_iol.token_grants == ["password", "refresh_token", "password"]


async def test_a_second_rejection_gives_up(db_session, make_user, fake_iol) -> None:
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)
    # Two rejections cannot be fixed by a single retry.
    fake_iol.reject_authenticated_requests = 5

    result = await broker_service.read_profile(
        db_session, user_id=user.id, connection=connection
    )

    assert result.ok is False
    assert result.is_stale is False
    assert fake_iol.token_grants == ["password", "refresh_token"]
    # The failure is recorded on the connection for the detail page.
    assert connection.last_error is not None


# --- Failure and staleness ------------------------------------------------


async def test_a_failed_first_read_has_nothing_to_show(
    db_session, make_user, fake_iol
) -> None:
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)
    fake_iol.profile_error_status = 500

    result = await broker_service.read_profile(
        db_session, user_id=user.id, connection=connection
    )

    assert result.ok is False
    assert result.snapshot is None
    assert result.is_stale is False
    assert result.error is not None


async def test_a_failed_refresh_keeps_the_previous_data_and_marks_it_stale(
    db_session, make_user, fake_iol
) -> None:
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)
    good = await broker_service.read_profile(
        db_session, user_id=user.id, connection=connection
    )
    fake_iol.profile_error_status = 503

    failed = await broker_service.read_profile(
        db_session, user_id=user.id, connection=connection
    )

    assert failed.ok is False
    assert failed.is_stale is True
    assert failed.snapshot is not None
    # The same row is reused: the good payload is still there, only flagged.
    assert failed.snapshot.id == good.snapshot.id
    assert failed.snapshot.payload["nombre"] == "Ana"
    assert failed.snapshot.is_stale is True
    assert failed.snapshot.stale_since is not None
    assert failed.snapshot.error is not None


async def test_a_recovered_refresh_clears_the_stale_flag(
    db_session, make_user, fake_iol
) -> None:
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)
    await broker_service.read_profile(db_session, user_id=user.id, connection=connection)
    fake_iol.profile_error_status = 503
    await broker_service.read_profile(db_session, user_id=user.id, connection=connection)

    fake_iol.profile_error_status = None
    recovered = await broker_service.read_profile(
        db_session, user_id=user.id, connection=connection
    )

    assert recovered.ok is True
    assert recovered.snapshot.is_stale is False
    assert recovered.snapshot.stale_since is None
    assert recovered.snapshot.error is None


async def test_a_failure_does_not_delete_older_snapshots(
    db_session, make_user, fake_iol
) -> None:
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)
    await broker_service.read_profile(db_session, user_id=user.id, connection=connection)
    fake_iol.profile_error_status = 500

    await broker_service.read_profile(db_session, user_id=user.id, connection=connection)

    stored = await snapshots_service.count_snapshots(
        db_session, user_id=user.id, connection_id=connection.id
    )
    assert stored == 1


async def test_the_two_kinds_are_stored_separately(
    db_session, make_user, fake_iol
) -> None:
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)
    await broker_service.read_profile(db_session, user_id=user.id, connection=connection)
    fake_iol.profile_error_status = 500

    # A failing profile refresh must not disturb the account-status snapshot.
    result = await broker_service.read_account_status(
        db_session, user_id=user.id, connection=connection
    )

    assert result.ok is True
    assert result.snapshot.kind == SNAPSHOT_ACCOUNT_STATUS


# --- Credentials ----------------------------------------------------------


async def test_a_wrong_saved_password_is_a_no_write_failure(
    db_session, make_user, fake_iol
) -> None:
    user = await make_user("alice")
    connection = await connections_service.create_connection(
        db_session,
        user_id=user.id,
        label="Broken login",
        username="iol-test-user",
        password="not-the-right-password",
    )

    result = await broker_service.read_profile(
        db_session, user_id=user.id, connection=connection
    )

    assert result.ok is False
    assert result.snapshot is None
    # The stored password must not appear in anything the user can see.
    assert "not-the-right-password" not in (result.error or "")
    assert "not-the-right-password" not in (connection.last_error or "")


async def test_an_unsupported_broker_is_refused(db_session, make_user, fake_iol) -> None:
    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)
    connection.broker = "some-other-broker"

    result = await broker_service.read_profile(
        db_session, user_id=user.id, connection=connection
    )

    assert result.ok is False
    assert fake_iol.token_grants == []

