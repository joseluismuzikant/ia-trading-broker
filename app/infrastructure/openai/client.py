"""A thin OpenAI client for one structured, portfolio-level answer.

It wraps the official OpenAI SDK and its Responses API, which is the API the
platform expects for new integrations. One call asks for one JSON document that
parses into :class:`~app.domain.analysis.PortfolioAdvice`; the request is made
for the **whole portfolio**, never once per instrument.

The client is the only place that knows about keys, endpoints, or the SDK. It
is also the only place that can reach the network, which is why the tests
replace it with a fake and never spend an API call.

Constructing it is the moment an API key becomes necessary; the application
itself starts and runs without one.
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any
from urllib.parse import urlparse

from app.config import Settings, get_settings
from app.domain.analysis_models import AnalysisModel
from app.redaction import redact


class OpenAIClientError(RuntimeError):
    """The OpenAI call could not be made or its answer could not be read."""


class OpenAIClient:
    """One configured OpenAI endpoint that returns structured JSON."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str | None = None,
        timeout_seconds: float = 30.0,
        transport: Any = None,
        api: str = "responses",
        api_key_env: str = "",
        response_format: str = "json_schema",
    ) -> None:
        if not api_key:
            raise OpenAIClientError(
                "OPENAI_API_KEY is not configured; the OpenAI advisor cannot run"
            )
        if not model:
            raise OpenAIClientError("the model catalog entry has no model id")
        if api not in ("responses", "chat"):
            raise OpenAIClientError(f"unknown model api {api!r}; use 'responses' or 'chat'")
        if response_format not in ("json_schema", "json_object"):
            raise OpenAIClientError(
                f"unknown response format {response_format!r}; use 'json_schema' or 'json_object'"
            )
        self.model = model
        self.api = api
        self.response_format = response_format
        self._api_key = api_key
        self._api_key_env = api_key_env
        self._timeout_seconds = timeout_seconds
        self._base_url = base_url
        self._transport = transport
        self._client: Any = transport

    @classmethod
    def from_model(cls, model: AnalysisModel, settings: Settings | None = None) -> OpenAIClient:
        """Build a client for one catalog entry.

        The model id and endpoint come from the entry; the key comes from the
        environment variable it names. Both are required to ask a model, and
        asking one is the only thing in the application that needs a key.
        """
        resolved = settings if settings is not None else get_settings()
        secret = getattr(resolved, model.api_key_env.lower(), None)
        api_key = secret.get_secret_value() if secret is not None else ""
        if not api_key:
            raise OpenAIClientError(
                f"{model.api_key_env} is not configured; the {model.label} analyst cannot run"
            )
        return cls(
            api_key=api_key,
            model=model.model,
            base_url=model.base_url,
            timeout_seconds=model.timeout_seconds or resolved.openai_timeout_seconds,
            api=model.api,
            api_key_env=model.api_key_env,
            response_format=model.response_format,
        )

    async def complete_json(
        self,
        *,
        instructions: str,
        input_text: str,
        schema: dict,
        name: str = "portfolio_advice",
    ) -> dict:
        """One structured-output call, answered with the requested JSON document.

        ``api = "responses"`` uses the OpenAI Responses API; ``api = "chat"``
        uses chat completions, which is what most OpenAI-compatible endpoints
        (Gemini, DeepSeek, ...) implement. Both demand the same strict JSON
        schema and both answer with one JSON document.
        """
        client = self._client if self._client is not None else self._build()
        strict_schema = strict_json_schema(schema)
        try:
            if self.api == "chat":
                if self.response_format == "json_object":
                    # JSON mode only guarantees *some* JSON, so the schema is
                    # spelled out in the instructions and checked on arrival.
                    system_text = json_mode_instructions(instructions, strict_schema)
                    response_format = {"type": "json_object"}
                else:
                    system_text = instructions
                    response_format = {
                        "type": "json_schema",
                        "json_schema": {
                            "name": name,
                            "strict": True,
                            "schema": strict_schema,
                        },
                    }
                response = await client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system_text},
                        {"role": "user", "content": input_text},
                    ],
                    response_format=response_format,
                )
                text = _chat_text(response)
            else:
                response = await client.responses.create(
                    model=self.model,
                    instructions=instructions,
                    input=input_text,
                    text={
                        "format": {
                            "type": "json_schema",
                            "name": name,
                            "strict": True,
                            "schema": strict_schema,
                        }
                    },
                )
                text = getattr(response, "output_text", None)
        except OpenAIClientError:
            raise
        except Exception as exc:  # noqa: BLE001 - any transport failure is one error
            raise OpenAIClientError(
                failure_message(
                    exc,
                    self._base_url,
                    self._timeout_seconds,
                    key_env=self._api_key_env,
                )
            ) from exc

        if not text or not str(text).strip():
            raise OpenAIClientError("the model returned no text")
        try:
            payload = json.loads(str(text))
        except json.JSONDecodeError as exc:
            raise OpenAIClientError("the model did not answer with the requested JSON") from exc
        if not isinstance(payload, dict):
            raise OpenAIClientError("the model answer is not a JSON object")
        return payload

    def _build(self) -> Any:
        """Create the SDK client lazily, so importing this module needs no key."""
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:  # pragma: no cover - declared in requirements
            raise OpenAIClientError(
                "the openai package is not installed; run pip install -r requirements.txt"
            ) from exc
        self._client = AsyncOpenAI(
            api_key=self._api_key,
            base_url=self._base_url or None,
            timeout=self._timeout_seconds,
        )
        return self._client

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<OpenAIClient model={self.model!r}>"


def json_mode_instructions(instructions: str, schema: dict) -> str:
    """Instructions for an endpoint that only has plain JSON mode.

    JSON mode requires the word "json" in the prompt and does not know the
    schema, so both are supplied here. The answer is still validated against
    the real contract by the advisor.
    """
    return (
        f"{instructions}\n\n"
        "Answer with a single JSON object and nothing else: no prose, no code "
        "fences. It must match this JSON schema exactly, with every listed "
        "property present:\n"
        f"{json.dumps(schema, indent=2)}"
    )


def _chat_text(response: Any) -> str | None:
    """The message text of a chat-completions answer, or None."""
    choices = getattr(response, "choices", None) or []
    if not choices:
        return None
    message = getattr(choices[0], "message", None)
    return getattr(message, "content", None)


#: Failure signatures that mean the endpoint's hostname could not be resolved.
_DNS_SIGNATURES = (
    "name or service not known",
    "nodename nor servname provided",
    "no address associated with hostname",
    "temporary failure in name resolution",
    "getaddrinfo failed",
)


def failure_message(
    exc: BaseException,
    base_url: str | None,
    timeout_seconds: float,
    key_env: str = "",
) -> str:
    """One safe, actionable line for a failed model request.

    The message names the endpoint that was being called, the deepest real
    reason found in the exception chain, and the provider's own explanation
    when it sent one. A wrong ``base_url`` surfaces as "the hostname does not
    resolve", a rejected credential as "the API key was rejected", and a chat
    /responses mismatch as the switch to make. No key, token, or request body
    is ever included.
    """
    host = urlparse(base_url or "").hostname or "the model endpoint"
    reason = redact(_deepest_reason(exc))
    lowered = reason.lower()
    detail = redact(_provider_detail(exc))
    suffix = f" The provider says: {detail}." if detail and detail != reason else ""
    key_name = f" ({key_env})" if key_env else ""

    if any(signature in lowered for signature in _DNS_SIGNATURES):
        return (
            f"the model request to {host} could not be sent: the hostname does "
            f"not resolve ({reason}). Check this model's base_url in "
            "config/models.toml and the DNS/network access of the service."
        )
    if "timed out" in lowered or "timeout" in lowered:
        return f"the model request to {host} timed out after {timeout_seconds:g}s ({reason})."
    if "401" in lowered or "403" in lowered or "unauthorized" in lowered:
        return (
            f"the model request to {host} was refused: the API key this model "
            f"uses{key_name} was rejected. Check that this is the key for "
            f"{host} and not another provider's.{suffix}"
        )
    if "404" in lowered and "/responses" in lowered:
        return (
            f"the model request to {host} failed: {reason}. This endpoint "
            "looks like it only implements chat completions; set "
            'api = "chat" for this model in config/models.toml.'
        )
    return f"the model request to {host} failed: {reason}{suffix}"


def _provider_detail(exc: BaseException) -> str:
    """A short, safe explanation from the provider's error response body.

    Providers put the useful text ("Invalid API key", "model not found") in
    the JSON body of a failed response. Only that one message is returned;
    no credential or request content is ever in it.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        detail = _message_from_payload(getattr(current, "body", None))
        if detail:
            return detail
        response = getattr(current, "response", None)
        if response is not None:
            try:
                detail = _message_from_payload(response.json())
            except Exception:  # noqa: BLE001 - an unparseable body is ignored
                detail = ""
            if detail:
                return detail
        current = current.__cause__ or current.__context__
    return ""


def _message_from_payload(payload: object) -> str:
    """The one-line message from a provider error payload, or an empty string."""
    if isinstance(payload, str):
        text = payload.strip()
        return text[:200] if text else ""
    if isinstance(payload, list):
        # Some providers (Gemini) wrap the error object in a one-item array.
        return next((text for item in payload if (text := _message_from_payload(item))), "")
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    if isinstance(error, dict):
        for key in ("message", "description", "detail"):
            value = error.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:200]
        messages = error.get("messages")
        if isinstance(messages, list) and messages:
            first = messages[0]
            if isinstance(first, dict):
                value = first.get("description") or first.get("title")
                if isinstance(value, str) and value.strip():
                    return value.strip()[:200]
    for key in ("message", "error_description", "detail", "error"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:200]
    return ""


def _deepest_reason(exc: BaseException) -> str:
    """The last non-empty message in an exception chain, or the first one."""
    reason = str(exc).strip() or type(exc).__name__
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        text = str(current).strip()
        if text:
            reason = text
        current = current.__cause__ or current.__context__
    return reason


def strict_json_schema(schema: dict) -> dict:
    """Make a JSON schema acceptable to strict structured output.

    Strict mode wants every property required and no extra ones, which is also
    exactly the contract this project wants from a model.
    """

    def walk(node: object) -> None:
        if isinstance(node, dict):
            properties = node.get("properties")
            if isinstance(properties, dict):
                node["required"] = list(properties)
                node["additionalProperties"] = False
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    tightened = deepcopy(schema)
    walk(tightened)
    return tightened
