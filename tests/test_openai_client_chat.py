"""Tests for the chat-completions path of the OpenAI-protocol client.

The SDK is replaced by a fake, so no request is made. They pin what is sent to
a chat endpoint in each response format, and that a provider error body shaped
as a JSON array (Gemini's) still yields the provider's own message.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.infrastructure.openai.client import (
    OpenAIClient,
    OpenAIClientError,
    _message_from_payload,
    json_mode_instructions,
)

SCHEMA = {"type": "object", "properties": {"summary": {"type": "string"}}}


class FakeChat:
    def __init__(self, text='{"summary": "ok"}') -> None:
        self.text = text
        self.calls: list[dict] = []
        message = SimpleNamespace(content=self.text)
        self.response = SimpleNamespace(choices=[SimpleNamespace(message=message)])
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def client_for(sdk: FakeChat, **options) -> OpenAIClient:
    return OpenAIClient(
        api_key="sk-test",
        model="some-model",
        base_url="https://api.example.test/v1",
        api="chat",
        transport=sdk,
        **options,
    )


async def ask(client: OpenAIClient) -> dict:
    return await client.complete_json(instructions="Be brief.", input_text="go", schema=SCHEMA)


async def test_a_chat_endpoint_gets_a_strict_schema_by_default() -> None:
    sdk = FakeChat()

    assert await ask(client_for(sdk)) == {"summary": "ok"}

    call = sdk.calls[0]
    assert call["response_format"]["type"] == "json_schema"
    assert call["response_format"]["json_schema"]["strict"] is True
    assert call["messages"][0]["content"] == "Be brief."


async def test_json_mode_sends_the_schema_in_the_instructions() -> None:
    sdk = FakeChat()

    assert await ask(client_for(sdk, response_format="json_object")) == {"summary": "ok"}

    call = sdk.calls[0]
    assert call["response_format"] == {"type": "json_object"}
    system = call["messages"][0]["content"]
    assert system.startswith("Be brief.")
    assert "JSON" in system  # JSON mode requires the word in the prompt
    assert '"summary"' in system  # the schema itself is spelled out
    assert json.loads(system[system.index("{") :])["additionalProperties"] is False


def test_json_mode_instructions_state_the_contract() -> None:
    text = json_mode_instructions("Hello.", {"type": "object"})
    assert text.startswith("Hello.")
    assert "no code fences" in text


def test_an_unknown_response_format_is_refused() -> None:
    with pytest.raises(OpenAIClientError, match="response format"):
        OpenAIClient(api_key="k", model="m", api="chat", response_format="xml")


def test_an_error_body_wrapped_in_an_array_still_gives_the_message() -> None:
    body = [{"error": {"code": 404, "message": "models/x is not found for API version v1beta"}}]
    assert "is not found" in _message_from_payload(body)
    assert _message_from_payload([]) == ""
