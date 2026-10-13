"""A thin Claude client for one structured, portfolio-level answer.

It wraps the official Anthropic SDK and answers the same question the OpenAI
client does: the whole portfolio in, one JSON document out. The advisor calls
``complete_json`` and cannot tell the two apart.

Claude-specific choices, each one deliberate:

* **Structured output through ``output_config.format``.** Opus 5.5 rejects a
  forced ``tool_choice`` with a 400, so the schema is sent as a JSON schema
  output format instead, after the SDK's ``transform_schema`` has folded the
  constraints Claude's subset does not support into descriptions.
* **Thinking is never configured.** It is always on for this model and any
  explicit setting is a 400; ``effort`` is the only depth control.
* **A refusal is an error, never an answer.** ``stop_reason == "refusal"`` is
  checked before the content is read, and the server-side ``default`` fallback
  is requested so a classifier decline can be retried on another model.
* **No sampling parameters,** which the model also rejects.

The client is the only place in this adapter that knows about the key or the
SDK, and the tests replace it with a fake so no call is ever spent.
"""

from __future__ import annotations

import json
from typing import Any

from app.config import Settings, get_settings
from app.domain.analysis_models import AnalysisModel
from app.infrastructure.openai.client import strict_json_schema
from app.redaction import redact

#: Beta that enables ``fallbacks: "default"`` on the Messages API.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
#: A portfolio answer is small; this leaves room for the thinking Claude does.
MAX_TOKENS = 16000


class AnthropicClientError(RuntimeError):
    """The Claude call could not be made or its answer could not be read."""


class AnthropicClient:
    """One configured Claude model that returns structured JSON."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_seconds: float = 120.0,
        effort: str = "high",
        transport: Any = None,
        api_key_env: str = "ANTHROPIC_API_KEY",
    ) -> None:
        if not api_key:
            raise AnthropicClientError(
                f"{api_key_env} is not configured; the Claude advisor cannot run"
            )
        if not model:
            raise AnthropicClientError("the model catalog entry has no model id")
        self.model = model
        self._api_key = api_key
        self._api_key_env = api_key_env
        self._timeout_seconds = timeout_seconds
        self._effort = effort
        self._client: Any = transport

    @classmethod
    def from_model(cls, model: AnalysisModel, settings: Settings | None = None) -> AnthropicClient:
        """Build a client for one catalog entry; the key comes from the environment."""
        resolved = settings if settings is not None else get_settings()
        secret = getattr(resolved, model.api_key_env.lower(), None)
        api_key = secret.get_secret_value() if secret is not None else ""
        return cls(
            api_key=api_key,
            model=model.model,
            timeout_seconds=model.timeout_seconds or resolved.openai_timeout_seconds,
            api_key_env=model.api_key_env,
        )

    async def complete_json(
        self,
        *,
        instructions: str,
        input_text: str,
        schema: dict,
        name: str = "portfolio_advice",
    ) -> dict:
        """One structured-output call, answered with the requested JSON document."""
        del name  # Claude's output format has no schema name
        client = self._client if self._client is not None else self._build()
        try:
            from anthropic import transform_schema

            output_schema = transform_schema(strict_json_schema(schema))
            response = await client.beta.messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                betas=[FALLBACK_BETA],
                fallbacks="default",
                system=instructions,
                messages=[{"role": "user", "content": input_text}],
                output_config={
                    "effort": self._effort,
                    "format": {"type": "json_schema", "schema": output_schema},
                },
            )
        except AnthropicClientError:
            raise
        except Exception as exc:  # noqa: BLE001 - any SDK failure is one clear error
            raise AnthropicClientError(self._failure_message(exc)) from exc

        return _answer_from(response)

    def _failure_message(self, exc: BaseException) -> str:
        """A safe one-line reason: never the key, never the request body."""
        import anthropic

        if isinstance(exc, anthropic.AuthenticationError):
            return f"Anthropic rejected {self._api_key_env}; check the key."
        if isinstance(exc, anthropic.PermissionDeniedError):
            return f"{self._api_key_env} is not allowed to use {self.model}."
        if isinstance(exc, anthropic.NotFoundError):
            return f"Anthropic does not know the model {self.model!r}; check config/models.toml."
        if isinstance(exc, anthropic.RateLimitError):
            return "Anthropic rate-limited the request; try again shortly."
        if isinstance(exc, anthropic.APITimeoutError):
            return (
                f"the Claude request timed out after {self._timeout_seconds:g}s; "
                "raise timeout_seconds for this model in config/models.toml."
            )
        if isinstance(exc, anthropic.APIConnectionError):
            return "Anthropic could not be reached; check the network."
        if isinstance(exc, anthropic.BadRequestError):
            return f"Anthropic refused the request as invalid: {_short(exc)}"
        if isinstance(exc, anthropic.APIStatusError):
            return f"Anthropic answered HTTP {exc.status_code}."
        return f"the Claude call failed ({type(exc).__name__})."

    def _build(self) -> Any:
        """Create the SDK client lazily, so importing this module needs no key."""
        try:
            from anthropic import AsyncAnthropic
        except ImportError as exc:  # pragma: no cover - declared in requirements
            raise AnthropicClientError(
                "the anthropic package is not installed; run pip install -r requirements.txt"
            ) from exc
        self._client = AsyncAnthropic(api_key=self._api_key, timeout=self._timeout_seconds)
        return self._client

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<AnthropicClient model={self.model!r}>"


def _short(exc: BaseException) -> str:
    message = redact(str(getattr(exc, "message", "") or exc)).strip()
    return message[:200] or "no detail"


def _answer_from(response: Any) -> dict:
    """The JSON object in a Claude answer, after the stop reason is checked."""
    reason = getattr(response, "stop_reason", None)
    if reason == "refusal":
        details = getattr(response, "stop_details", None)
        category = getattr(details, "category", None)
        suffix = f" ({category})" if category else ""
        raise AnthropicClientError(f"Claude declined to answer this request{suffix}.")
    if reason == "max_tokens":
        raise AnthropicClientError("the Claude answer was cut off before it finished")

    text = next(
        (
            block.text
            for block in getattr(response, "content", None) or []
            if getattr(block, "type", None) == "text" and getattr(block, "text", "").strip()
        ),
        "",
    )
    if not text:
        raise AnthropicClientError("the model returned no text")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AnthropicClientError("the model did not answer with the requested JSON") from exc
    if not isinstance(payload, dict):
        raise AnthropicClientError("the model answer is not a JSON object")
    return payload
