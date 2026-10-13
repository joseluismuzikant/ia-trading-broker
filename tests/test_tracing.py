"""Tests for LangSmith tracing: optional, and never able to leak a secret.

Tracing is off by default; the application runs exactly the same without it.
These tests pin that no configuration means no tracing, that only safe values
reach a trace, and that enabling it never puts a credential in metadata. They
make no LangSmith network call: with tracing off the SDK is never even asked.
"""

from __future__ import annotations

import os

from app.config import get_settings
from app.tracing import (
    configure_tracing,
    is_enabled,
    run_metadata,
    safe_metadata,
    span,
)

# --- Off by default -------------------------------------------------------


def test_tracing_is_off_and_needs_no_key() -> None:
    settings = get_settings()
    assert settings.langsmith_tracing is False
    assert is_enabled() is False
    assert configure_tracing() is False


def test_disabling_tracing_publishes_no_environment() -> None:
    os.environ.pop("LANGSMITH_TRACING", None)
    os.environ.pop("LANGSMITH_API_KEY", None)

    assert configure_tracing() is False

    assert "LANGSMITH_TRACING" not in os.environ
    assert "LANGSMITH_API_KEY" not in os.environ


def test_a_span_is_a_no_op_when_tracing_is_off(monkeypatch) -> None:
    import langsmith.run_helpers as run_helpers

    def explode(*_args, **_kwargs):  # pragma: no cover - must never run
        raise AssertionError("the LangSmith SDK must not be called")

    monkeypatch.setattr(run_helpers, "trace", explode)

    with span("test_step", inputs={"a": 1}, metadata={"b": 2}) as run:
        run.end(outputs={"done": True})
        assert run.inputs == {}


# --- Only safe values are ever traced ------------------------------------


def test_secret_looking_keys_are_dropped() -> None:
    clean = safe_metadata(
        {
            "connection_id": 7,
            "country": "argentina",
            "OPENAI_API_KEY": "sk-secret",
            "api_key": "sk-secret",
            "access_token": "bearer",
            "refresh_token": "refresh",
            "password": "hunter2",
            "authorization": "Bearer x",
            "session_cookie": "cookie",
            "csrf_token": "token",
            "credential_encryption_key": "fernet",
            "LANGSMITH_API_KEY": "ls-secret",
        }
    )

    assert clean == {"connection_id": 7, "country": "argentina"}


def test_objects_are_never_serialised_into_metadata() -> None:
    class HoldsASecret:
        api_key = "sk-secret"

    clean = safe_metadata({"client": HoldsASecret(), "note": "safe", "count": 3})

    assert clean == {"note": "safe", "count": 3}


def test_run_metadata_carries_only_the_documented_fields() -> None:
    metadata = run_metadata(
        connection_id=4,
        country="argentina",
        model_provider="openai",
        model_name="gpt-6.1-sol",
        proposal_id=12,
        execution_mode="LIVE",
    )

    assert metadata == {
        "connection_id": 4,
        "country": "argentina",
        "model_provider": "openai",
        "model_name": "gpt-6.1-sol",
        "proposal_id": 12,
        "execution_mode": "LIVE",
    }


# --- Enabling it ----------------------------------------------------------


def test_enabling_tracing_publishes_the_expected_environment(monkeypatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "test")
    for name in (
        "LANGSMITH_TRACING",
        "LANGSMITH_API_KEY",
        "LANGSMITH_PROJECT",
        "LANGSMITH_ENDPOINT",
        "LANGSMITH_WORKSPACE_ID",
    ):
        os.environ.pop(name, None)
    settings = get_settings()
    monkeypatch.setattr(settings, "langsmith_tracing", True)
    monkeypatch.setattr(settings, "langsmith_project", "ia-test-project")

    try:
        assert configure_tracing(settings) is True
        assert is_enabled(settings) is True
        assert os.environ["LANGSMITH_TRACING"] == "true"
        assert os.environ["LANGSMITH_PROJECT"] == "ia-test-project"
    finally:
        for name in ("LANGSMITH_TRACING", "LANGSMITH_PROJECT"):
            os.environ.pop(name, None)


def test_the_model_call_trace_never_holds_the_client(monkeypatch) -> None:
    """The advisor's span receives dictionaries only, never OpenAIClient."""
    import asyncio

    import app.infrastructure.openai.portfolio_advisor as advisor_module
    from app.infrastructure.openai import OpenAIPortfolioAdvisor
    from tests.test_openai_portfolio_advisor import (
        FakeOpenAIClient,
        build_context,
        canned_payload,
        catalog_entry,
    )

    seen: list[dict] = []

    class RecordingRun:
        def end(self, *, outputs=None, error=None):
            seen.append({"outputs": outputs})

    class RecordingSpan:
        def __init__(self, name, **kwargs):
            self.name = name
            self.kwargs = kwargs

        def __enter__(self):
            seen.append({"name": self.name, "inputs": self.kwargs.get("inputs")})
            return RecordingRun()

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(
        advisor_module,
        "span",
        lambda name, **kwargs: RecordingSpan(name, **kwargs),
    )
    advisor = OpenAIPortfolioAdvisor(
        model=catalog_entry(), client=FakeOpenAIClient(canned_payload())
    )

    result = asyncio.run(advisor.analyze(build_context()))

    assert result.model_name == "gemini-2.5-pro"
    assert seen[0]["name"] == "openai_portfolio_analysis"
    inputs = seen[0]["inputs"]
    assert inputs["model_provider"] == "google"
    assert inputs["model_name"] == "gemini-2.5-pro"
    assert inputs["number_of_positions"] == 3
    assert "api_key" not in inputs
    assert "client" not in inputs
