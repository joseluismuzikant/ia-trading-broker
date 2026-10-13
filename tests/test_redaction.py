"""Tests for credential redaction in anything that may be shown or logged.

The samples are invented strings shaped like real credentials. None is a real
key, and none can authenticate anywhere.
"""

from __future__ import annotations

import httpx
import pytest

from app.infrastructure.iol.errors import IOLAPIError, safe_message
from app.infrastructure.openai.client import failure_message
from app.redaction import REDACTED, redact


def _fake(*parts: str) -> str:
    """Join fragments at runtime, so no key-shaped literal sits in the source.

    A secret scanner (or GitHub push protection) would otherwise flag these
    invented strings as leaked keys.
    """
    return "".join(parts)


FAKE_OPENAI = _fake("sk", "-proj-", "AbCdEf123456GhIjKl789012MnOpQr")
FAKE_ANTHROPIC = _fake("sk", "-ant-", "api03-AbCdEf123456GhIjKl789012MnOpQr")
FAKE_GOOGLE = _fake("AI", "za", "SyA-1234567890abcdefghijklmnopqrstuv")
FAKE_JWT = _fake(
    "ey", "JhbGciOiJIUzI1NiJ9.", "eyJzdWIiOiIxMjM0NTY3ODkwIn0.", "dBjftJeZ4CVPmB92K27uhbUJU1p1r"
)


@pytest.mark.parametrize(
    "secret",
    [
        FAKE_OPENAI,
        FAKE_ANTHROPIC,
        FAKE_GOOGLE,
        FAKE_JWT,
        _fake("xai", "-AbCdEf123456GhIjKl789012"),
        _fake("gh", "p_AbCdEf123456GhIjKl789012MnOpQrStUv"),
        _fake("AK", "IAABCDEFGHIJKLMNOP"),
    ],
)
def test_a_credential_shaped_string_is_removed(secret) -> None:
    cleaned = redact(f"the call failed because of {secret} in the request")

    assert secret not in cleaned
    assert REDACTED in cleaned


def test_a_partly_masked_key_echoed_by_a_provider_is_removed() -> None:
    cleaned = redact(
        "Incorrect API key provided: sk-proj-AbCd********wxyz. Find it at the console."
    )

    assert "sk-proj" not in cleaned
    assert "wxyz" not in cleaned
    assert "Incorrect API key provided" in cleaned


def test_an_authorization_header_and_bearer_token_are_removed() -> None:
    cleaned = redact("sent Authorization: Bearer abcDEF1234567890xyz to the broker")

    assert "abcDEF1234567890xyz" not in cleaned


@pytest.mark.parametrize(
    "text",
    [
        "password=hunter2hunter2",
        'api_key: "abcd1234efgh5678"',
        "refresh_token = abcd1234efgh5678",
        "ApiSecret: abcd1234efgh5678",
    ],
)
def test_a_secret_assignment_keeps_the_name_and_drops_the_value(text) -> None:
    cleaned = redact(text)

    assert REDACTED in cleaned
    for fragment in ("hunter2hunter2", "abcd1234efgh5678"):
        assert fragment not in cleaned


def test_a_long_opaque_run_such_as_a_fernet_key_is_removed() -> None:
    key = "V19pwAKBBvM85X-KHh3zYNDq0l6LoROtAT8xGZ_2Jyg="
    assert key not in redact(f"decrypting with {key} failed")


def test_ordinary_text_is_left_alone() -> None:
    text = "The broker did not answer in time (HTTP 502) for GGAL, 100 units at 60.55."
    assert redact(text) == text


def test_the_iol_safe_message_redacts_a_key_that_slipped_in() -> None:
    assert FAKE_OPENAI not in safe_message(f"login failed with {FAKE_OPENAI}")
    assert FAKE_OPENAI not in IOLAPIError(f"boom {FAKE_OPENAI}").message


def test_a_provider_error_that_echoes_a_key_does_not_reach_the_user() -> None:
    request = httpx.Request("POST", "https://api.example.test/v1/chat/completions")
    response = httpx.Response(401, request=request)
    error = httpx.HTTPStatusError(
        f"401 Unauthorized: Incorrect API key provided: {FAKE_OPENAI}",
        request=request,
        response=response,
    )

    message = failure_message(error, "https://api.example.test/v1", 30, key_env="OPENAI_API_KEY")

    assert FAKE_OPENAI not in message
    assert "sk-proj" not in message
    assert "OPENAI_API_KEY" in message


async def test_an_event_message_never_stores_a_credential(db_session, make_user, fake_iol) -> None:
    from app.services import events as events_service
    from tests.day5_helpers import make_connection

    user = await make_user("alice")
    connection = await make_connection(db_session, user, fake_iol)

    event = await events_service.record_event(
        db_session,
        user_id=user.id,
        connection_id=connection.id,
        category="run",
        event_type="run.blocked",
        message=f"The model refused: Incorrect API key provided: {FAKE_OPENAI}",
    )

    assert FAKE_OPENAI not in event.message
    assert "Incorrect API key provided" in event.message


def test_trace_metadata_redacts_a_key_hidden_in_a_value() -> None:
    from app.tracing import safe_metadata

    clean = safe_metadata(
        {
            "error": f"401 from the provider for {FAKE_ANTHROPIC}",
            "model_name": "claude-opus-5-5",
            "api_key": FAKE_OPENAI,
        }
    )

    assert "api_key" not in clean  # dropped by name
    assert FAKE_ANTHROPIC not in clean["error"]
    assert clean["model_name"] == "claude-opus-5-5"


async def test_a_claude_error_never_carries_a_key() -> None:
    from types import SimpleNamespace

    import anthropic

    from app.infrastructure.anthropic.client import AnthropicClient, AnthropicClientError

    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(400, request=request, json={})
    error = anthropic.BadRequestError(
        f"invalid request for key {FAKE_ANTHROPIC}", response=response, body=None
    )

    async def boom(**_kwargs):
        raise error

    sdk = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=boom)))
    client = AnthropicClient(api_key="k", model="claude-opus-5-5", transport=sdk)

    with pytest.raises(AnthropicClientError) as caught:
        await client.complete_json(instructions="x", input_text="y", schema={"type": "object"})

    assert FAKE_ANTHROPIC not in str(caught.value)
