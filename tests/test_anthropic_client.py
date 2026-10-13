"""Tests for the Claude client: the request it builds and how it reads an answer.

The SDK client is replaced by a fake, so no call is made and no key is needed.
The request shape follows the Anthropic documentation for Claude Opus 5.5:
structured output through ``output_config.format``, no thinking or sampling
parameters, and no forced ``tool_choice``.
"""

from __future__ import annotations

from types import SimpleNamespace

import anthropic
import httpx
import pytest

from app.config import Settings
from app.domain.analysis import PortfolioAdvice
from app.domain.analysis_models import AnalysisModel
from app.infrastructure.anthropic.client import (
    FALLBACK_BETA,
    AnthropicClient,
    AnthropicClientError,
)
from app.infrastructure.openai.portfolio_advisor import build_client

ANSWER = '{"summary": "ok"}'


class FakeMessages:
    def __init__(self, response=None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


def fake_sdk(response=None, error=None) -> tuple[SimpleNamespace, FakeMessages]:
    messages = FakeMessages(response, error)
    return SimpleNamespace(beta=SimpleNamespace(messages=messages)), messages


def reply(text=ANSWER, stop_reason="end_turn", **extra):
    block = SimpleNamespace(type="text", text=text)
    thinking = SimpleNamespace(type="thinking", thinking="")
    return SimpleNamespace(
        content=[thinking, block], stop_reason=stop_reason, stop_details=None, **extra
    )


def client_with(response=None, error=None):
    sdk, messages = fake_sdk(response, error)
    return AnthropicClient(api_key="sk-ant-test", model="claude-opus-5-5", transport=sdk), messages


async def ask(client):
    return await client.complete_json(
        instructions="be brief",
        input_text="the account",
        schema=PortfolioAdvice.model_json_schema(),
    )


async def test_the_request_uses_structured_output_and_no_forbidden_parameters() -> None:
    client, messages = client_with(reply())

    assert await ask(client) == {"summary": "ok"}

    call = messages.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["system"] == "be brief"
    assert call["messages"] == [{"role": "user", "content": "the account"}]
    assert call["betas"] == [FALLBACK_BETA]
    assert call["fallbacks"] == "default"
    assert call["output_config"]["effort"] == "high"
    assert call["output_config"]["format"]["type"] == "json_schema"
    schema = call["output_config"]["format"]["schema"]
    assert schema["additionalProperties"] is False
    # Forbidden on this model: a forced tool, thinking config, sampling.
    for forbidden in ("tool_choice", "thinking", "temperature", "top_p", "top_k"):
        assert forbidden not in call


async def test_a_refusal_is_an_error_not_an_answer() -> None:
    refused = reply(text="", stop_reason="refusal")
    refused.stop_details = SimpleNamespace(category="cyber")
    client, _ = client_with(refused)

    with pytest.raises(AnthropicClientError, match=r"declined.*cyber"):
        await ask(client)


async def test_a_truncated_answer_is_an_error() -> None:
    client, _ = client_with(reply(stop_reason="max_tokens"))
    with pytest.raises(AnthropicClientError, match="cut off"):
        await ask(client)


@pytest.mark.parametrize(
    ("text", "message"),
    [("", "no text"), ("not json", "requested JSON"), ("[1, 2]", "not a JSON object")],
)
async def test_an_unreadable_answer_is_an_error(text, message) -> None:
    client, _ = client_with(reply(text=text))
    with pytest.raises(AnthropicClientError, match=message):
        await ask(client)


def status_error(cls, status):
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(status, request=request, json={})
    return cls("boom sk-ant-test", response=response, body=None)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (status_error(anthropic.AuthenticationError, 401), "rejected ANTHROPIC_API_KEY"),
        (status_error(anthropic.RateLimitError, 429), "rate-limited"),
        (status_error(anthropic.NotFoundError, 404), "does not know the model"),
        (
            anthropic.APITimeoutError(request=httpx.Request("POST", "https://x")),
            "timed out",
        ),
    ],
)
async def test_sdk_failures_become_one_safe_message(error, expected) -> None:
    client, _ = client_with(error=error)

    with pytest.raises(AnthropicClientError, match=expected) as caught:
        await ask(client)

    assert "sk-ant-test" not in str(caught.value)


def test_the_client_refuses_to_build_without_a_key() -> None:
    with pytest.raises(AnthropicClientError, match="ANTHROPIC_API_KEY is not configured"):
        AnthropicClient(api_key="", model="claude-opus-5-5")


def claude_entry(**overrides) -> AnalysisModel:
    fields = {
        "key": "claude",
        "label": "Claude Opus 5.5",
        "model": "claude-opus-5-5",
        "provider": "anthropic",
        "base_url": "https://api.anthropic.com",
        "api_key_env": "ANTHROPIC_API_KEY",
        "api": "anthropic",
        "timeout_seconds": 180.0,
    }
    fields.update(overrides)
    return AnalysisModel(**fields)


def settings_with(**values) -> Settings:
    return Settings(
        _env_file=None,
        secret_key="a-development-secret-of-sufficient-length",
        database_url="sqlite+aiosqlite:///:memory:",
        **values,
    )


def test_from_model_reads_the_key_and_the_catalog_timeout() -> None:
    client = AnthropicClient.from_model(
        claude_entry(), settings_with(anthropic_api_key="sk-ant-test")
    )

    assert client.model == "claude-opus-5-5"
    assert client._timeout_seconds == 180.0  # noqa: SLF001
    assert "sk-ant-test" not in repr(client)


def test_from_model_without_the_key_names_the_variable() -> None:
    with pytest.raises(AnthropicClientError, match="ANTHROPIC_API_KEY is not configured"):
        AnthropicClient.from_model(claude_entry(), settings_with())


def test_the_advisor_picks_the_claude_client_for_the_anthropic_api() -> None:
    client = build_client(claude_entry(), settings_with(anthropic_api_key="sk-ant-test"))
    assert isinstance(client, AnthropicClient)
