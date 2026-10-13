"""Optional LangSmith tracing: safe spans, never a secret.

Tracing is off by default and the application runs the same with or without
it. When ``LANGSMITH_TRACING=true``, :func:`configure_tracing` publishes the
settings into the standard environment variables, which is what the LangGraph
runtime and the LangSmith SDK read: node-level runs then appear in the
configured project automatically.

Everything this module sends is explicit and sanitised. It is deliberately a
separate module so that nothing around a model call or a graph run has to
reason about secrets: :func:`safe_metadata` drops any key that looks like a
credential, and no span ever receives a client object, a token, or a settings
instance — only plain dictionaries of already-safe values.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from app.config import Settings, get_settings
from app.redaction import redact

logger = logging.getLogger("ia_trading_broker.tracing")

#: Metadata keys that are safe to show in a trace, whatever the caller sent.
_SECRET_MARKERS = (
    "key",
    "token",
    "secret",
    "password",
    "passwd",
    "authorization",
    "cookie",
    "csrf",
    "credential",
)


class _NullRun:
    """The run object used when tracing is off. Records nothing."""

    def __init__(self) -> None:
        self.inputs: dict[str, Any] = {}
        self.outputs: dict[str, Any] = {}
        self.metadata: dict[str, Any] = {}
        self.tags: list[str] = []

    def end(self, *, outputs: Any = None, error: str | None = None) -> None:
        return None


def is_enabled(settings: Settings | None = None) -> bool:
    """True when LangSmith tracing is explicitly switched on."""
    resolved = settings if settings is not None else get_settings()
    return bool(resolved.langsmith_tracing)


def configure_tracing(settings: Settings | None = None) -> bool:
    """Publish the LangSmith environment when tracing is enabled.

    Returns True when tracing is active. With tracing off nothing is set and
    nothing is imported, so an offline test can never make a network call.
    """
    resolved = settings if settings is not None else get_settings()
    if not resolved.langsmith_tracing:
        return False
    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGSMITH_PROJECT"] = resolved.langsmith_project
    if resolved.langsmith_api_key is not None:
        os.environ.setdefault("LANGSMITH_API_KEY", resolved.langsmith_api_key.get_secret_value())
    if resolved.langsmith_endpoint:
        os.environ.setdefault("LANGSMITH_ENDPOINT", resolved.langsmith_endpoint)
    if resolved.langsmith_workspace_id:
        os.environ.setdefault("LANGSMITH_WORKSPACE_ID", resolved.langsmith_workspace_id)
    logger.info("LangSmith tracing enabled for project %s", resolved.langsmith_project)
    return True


def safe_metadata(values: Mapping[str, Any] | None) -> dict[str, Any]:
    """The metadata worth sending: everything secret-looking is dropped."""
    if not values:
        return {}
    clean: dict[str, Any] = {}
    for key, value in values.items():
        lowered = key.lower()
        if any(marker in lowered for marker in _SECRET_MARKERS):
            continue
        if isinstance(value, str):
            # A key can hide in a value too (an echoed error, a pasted header).
            clean[key] = redact(value)
        elif value is None or isinstance(value, (int, float, bool)):
            clean[key] = value
        elif isinstance(value, (list, tuple)):
            clean[key] = [
                redact(item) if isinstance(item, str) else item
                for item in value
                if isinstance(item, (str, int, float, bool))
            ]
        # Anything else (objects, models, clients) is never serialised.
    return clean


def run_metadata(
    *,
    connection_id: int | None = None,
    country: str | None = None,
    model_provider: str | None = None,
    model_name: str | None = None,
    proposal_id: int | None = None,
    execution_mode: str | None = None,
) -> dict[str, Any]:
    """The graph-run metadata a trace may show. Safe values only."""
    return safe_metadata(
        {
            "connection_id": connection_id,
            "country": country,
            "model_provider": model_provider,
            "model_name": model_name,
            "proposal_id": proposal_id,
            "execution_mode": execution_mode,
        }
    )


@contextmanager
def span(
    name: str,
    *,
    inputs: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
    tags: list[str] | None = None,
) -> Iterator[Any]:
    """One LangSmith span around a step, or a silent no-op when disabled.

    ``inputs`` and ``metadata`` must already be plain, safe values. The run
    object the caller gets is never the OpenAI client and never anything that
    holds a credential, so nothing here can serialise one.
    """
    if not is_enabled():
        yield _NullRun()
        return

    try:
        from langsmith.run_helpers import trace
    except ImportError:  # pragma: no cover - declared in requirements
        logger.warning("LangSmith tracing is on but langsmith is not installed")
        yield _NullRun()
        return

    with trace(
        name,
        inputs=dict(safe_metadata(inputs)),
        metadata=dict(safe_metadata(metadata)),
        tags=tags or [],
    ) as run:
        yield run
